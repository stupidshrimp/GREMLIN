import io
import math
import os
import secrets
import sqlite3
import sys
import tempfile
from datetime import date
from functools import wraps
from pathlib import Path

from flask import Flask, jsonify, make_response, redirect, render_template, request, send_file, session, url_for

from repositories.analysis_repo import AnalysisRepository
from repositories.availability_repo import AvailabilityConfigError, AvailabilityRepository
from repositories.failure_repo import FailureRepository
from repositories.metrics_repo import MetricsRepository
from services.availability_dashboard import (
    build_config,
    build_dashboard,
    build_work_order_detail,
    clamp_window_months,
)
from services.reliability_service import ReliabilityService
from services.life_data_service import (
    DEFAULT_DB_PATH,
    DISPLAY_COLUMNS,
    NARRATIVE_COLUMNS,
    PM_DISPOSITION_CATEGORIES,
    PM_RESET_DECISIONS,
    WO_DISPOSITION_CATEGORIES,
    DatabaseWriteError,
    LifeDataService,
)
from services.bug_reports import (
    DEFAULT_BUG_DB_PATH,
    LIST_LIMIT,
    SEVERITIES,
    STATUSES,
    BugReportConflictError,
    BugReportRateLimitError,
    BugReportStore,
    BugReportStoreError,
    BugReportValidationError,
)
from services.patch_notes_service import build_reader as build_patch_notes_reader
from services.schema_service import SchemaService, SchemaServiceError
from services.access_control import ACTIVITY_DEFAULT_DAYS, ACTIVITY_MAX_DAYS, AccessControl, ROLES
from services.sync_service import (
    APP_ENV_KEYS,
    LimbleSyncRunner,
    SyncAlreadyRunningError,
    SyncOptionError,
    SyncOptions,
    load_dotenv_files,
)

# Read the app's own settings out of .env before anything below looks at them.
# This is the one safe moment to do it: nothing has been built yet, so there is
# no service holding a value that this could contradict. Only the names in
# APP_ENV_KEYS are applied, and a real environment variable still wins over the
# file -- so host-exported access-control settings keep deciding.
load_dotenv_files(only_keys=APP_ENV_KEYS)

app = Flask(__name__)

# Who the login limiter thinks it is talking to. Served directly, that is
# request.remote_addr and nothing needs saying. Behind a reverse proxy or a TLS
# terminator it is the *proxy* -- one address for the whole plant -- so five
# wrong PINs from anybody would lock the shared per-client scope and hand every
# other user a 429 for fifteen minutes, correct credentials included.
#
# Opt-in, because trusting X-Forwarded-For without a proxy in front is worse
# than not reading it: any client could then claim whatever address it liked and
# step around the limiter entirely. Set this to the number of proxies GREMLIN
# actually sits behind (usually 1) and only that many hops are believed.
def _parse_trusted_proxy_hops(raw: str | None) -> int:
    """How many forwarding hops to believe; 0 when the setting is absent."""

    value = (raw or "").strip()
    if not value:
        return 0
    try:
        hops = int(value)
    except ValueError:
        hops = 0
    if hops < 1:
        # Refuse rather than fall back. A deployment that set this believes its
        # per-client throttling works, and silently ignoring the value would
        # leave that belief wrong -- which is the direction that hurts.
        raise RuntimeError(
            "GREMLIN_TRUSTED_PROXY_HOPS must be a positive whole number of proxies "
            f"(got {value!r})."
        )
    return hops


_trusted_proxy_hops = _parse_trusted_proxy_hops(os.environ.get("GREMLIN_TRUSTED_PROXY_HOPS"))
if _trusted_proxy_hops:
    from werkzeug.middleware.proxy_fix import ProxyFix

    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=_trusted_proxy_hops)

_gremlin_db_for_access = Path(os.environ.get("GREMLIN_DB_PATH") or DEFAULT_DB_PATH)
ACCESS_DB_PATH = Path(
    os.environ.get("GREMLIN_ACCESS_DB_PATH") or _gremlin_db_for_access.with_name("accesscontrol.db")
)
access_control = AccessControl(ACCESS_DB_PATH)
access_control.ensure_schema(
    initial_username=os.environ.get("GREMLIN_ADMIN_USERNAME"),
    initial_pin=os.environ.get("GREMLIN_ADMIN_PIN"),
)

# Bug reports live on the shared drive rather than beside GREMLIN.db, so every
# deployment files into and reads from the same list. Constructing the store
# touches nothing -- deliberately, unlike the access database above: the folder,
# the file and the table are created on the first report or the first time the
# dashboard is opened, so a share that is unreachable at startup does not stop
# GREMLIN starting.
BUGS_DB_PATH = Path(os.environ.get("GREMLIN_BUGS_DB_PATH") or DEFAULT_BUG_DB_PATH)
bug_reports = BugReportStore(BUGS_DB_PATH)

# Signs the cookie that says who is logged in, so this is what stands between a
# session and a forged one. Falling back to a key generated per process would
# have been wrong the moment GREMLIN runs as more than one: each worker would
# reject the cookies the others signed. The access database holds one key for
# the whole deployment instead, generated on first start and kept across
# restarts, so no configuration is needed and nobody is signed out by a
# restart. An exported GREMLIN_SECRET_KEY still wins.
app.secret_key = os.environ.get("GREMLIN_SECRET_KEY") or access_control.shared_secret_key()

# Lax is the browsers' own default for a cookie that does not say; setting it
# means the session is not sent with a cross-site form POST regardless of which
# browser is asking. Secure is deliberately left off: GREMLIN is served over
# plain HTTP on the plant network, and a Secure cookie would simply never be
# sent. Set SESSION_COOKIE_SECURE where the deployment is behind TLS.
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_HTTPONLY"] = True

ROLE_LEVEL = {"viewer": 0, "editor": 1, "admin": 2}


def current_user() -> dict | None:
    return session.get("user")


def _authorised_user(role: str) -> tuple[dict | None, tuple | None]:
    """Reload the account so deletion and demotion revoke access immediately."""
    snapshot = current_user()
    user = access_control.get_user(snapshot.get("id")) if snapshot and snapshot.get("id") else None
    if not user or snapshot.get("credential_version") != user.get("credential_version"):
        session.pop("user", None)
        return None, (jsonify({"error": "Log in to make changes; guest access is read-only."}), 401)
    session["user"] = user
    if ROLE_LEVEL.get(user["role"], -1) < ROLE_LEVEL[role]:
        return None, (jsonify({"error": f"The {role} role is required for this action."}), 403)
    return user, None


def requires_role(role: str):
    """Require a signed-in user at or above ``role`` for a modifying action."""
    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            user, error = _authorised_user(role)
            if error:
                return error
            response = view(*args, **kwargs)
            status = response[1] if isinstance(response, tuple) and len(response) > 1 else getattr(response, "status_code", 200)
            if int(status) < 400:
                try:
                    access_control.record_change(user, f"{request.method} {request.path}")
                except (sqlite3.Error, OSError):
                    # The protected view may already have committed its write.
                    # Never turn that success into a 500 that invites a retry;
                    # surface the audit failure separately and log its traceback
                    # for the operator to repair.
                    app.logger.exception("Protected write succeeded, but its audit entry could not be stored")
                    response = make_response(response)
                    response.headers["X-GREMLIN-Audit-Warning"] = "audit-entry-not-stored"
            return response
        return wrapped
    return decorator


def csrf_token() -> str:
    """Return the unpredictable token bound to the current browser session."""
    token = session.get("csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["csrf_token"] = token
    return token


def requires_csrf(view):
    """Reject form writes that were not submitted by a page from this session."""
    @wraps(view)
    def wrapped(*args, **kwargs):
        expected = session.get("csrf_token") or ""
        submitted = request.form.get("csrf_token") or ""
        if not expected or not secrets.compare_digest(expected, submitted):
            return jsonify({"error": "The form expired or was submitted from another site. Reload and try again."}), 403
        return view(*args, **kwargs)
    return wrapped


def _resolved_account() -> dict | None:
    """The signed-in account as the accounts table has it now, or None.

    The session carries a snapshot taken at login. This reloads the account
    behind it and refuses the snapshot when the two disagree, which is what makes
    a changed PIN or a revoked account take effect on the next page load rather
    than whenever the browser is next closed.
    """
    snapshot = current_user()
    if not snapshot or not snapshot.get("id"):
        return None
    user = access_control.get_user(snapshot["id"])
    if not user or snapshot.get("credential_version") != user.get("credential_version"):
        return None
    return user


@app.context_processor
def auth_context():
    user = _resolved_account()
    can_edit = bool(user and ROLE_LEVEL.get(user["role"], -1) >= ROLE_LEVEL["editor"])
    # The account dialog is the only entrance to the developer area, so every
    # page needs to know whether to draw that door. Derived from the reloaded
    # account rather than the session snapshot, so a demoted administrator
    # stops being offered the link on their next page load.
    is_admin = bool(user and ROLE_LEVEL.get(user["role"], -1) >= ROLE_LEVEL["admin"])
    return {
        "auth_user": user,
        "auth_can_edit": can_edit,
        "auth_is_admin": is_admin,
        "auth_csrf_token": csrf_token() if user else None,
        # The topbar's global search is drawn on every page and its catalog is
        # role-filtered, so it is built here rather than in a second context
        # processor: the account has already been reloaded and checked, and
        # doing it again would mean a second lookup on every single render.
        "search_index": _search_index(is_admin=is_admin, can_edit=can_edit, has_account=bool(user)),
    }

ICONS = {
    "home": '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 3.2 3.7 10A1 1 0 0 0 3.3 11.1a1 1 0 0 0 1 .7h1v7.3c0 .5.4.9.9.9h4.8v-5.3c0-.5.4-.9.9-.9h1.8c.5 0 .9.4.9.9V20h4.8c.5 0 .9-.4.9-.9v-7.3h1a1 1 0 0 0 .6-1.8L12 3.2Z"/></svg>',
    "trend": '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4.7 18.9a1 1 0 0 1 0-1.4l5.6-5.6c.4-.4 1-.4 1.4 0l2.7 2.7 4.9-4.9h-2.4a1 1 0 1 1 0-2h4.8c.5 0 .9.4.9.9v4.8a1 1 0 1 1-2 0V11l-5.6 5.6c-.4.4-1 .4-1.4 0l-2.7-2.7-4.9 4.9a1 1 0 0 1-1.4 0Z"/><path d="M3 5.1c0-.5.4-.9.9-.9h16.2c.5 0 .9.4.9.9s-.4.9-.9.9H3.9A.9.9 0 0 1 3 5.1Z"/></svg>',
    "chart": '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4.5 20.8a1 1 0 0 1-1-1V4.2a1 1 0 1 1 2 0v14.6h14.6a1 1 0 1 1 0 2H4.5Z"/><path d="M8.1 16.1a1 1 0 0 1-1-1v-3.4a1 1 0 1 1 2 0v3.4a1 1 0 0 1-1 1Zm4 0a1 1 0 0 1-1-1V8.6a1 1 0 1 1 2 0v6.5a1 1 0 0 1-1 1Zm4 0a1 1 0 0 1-1-1v-5a1 1 0 1 1 2 0v5a1 1 0 0 1-1 1Z"/></svg>',
    "docs": '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 2.8h7.8c.2 0 .5.1.6.3l3.8 3.8c.2.2.3.4.3.6v13.7c0 .5-.4.9-.9.9H6c-.5 0-.9-.4-.9-.9V3.7c0-.5.4-.9.9-.9Zm7.2 1.9v2.6c0 .5.4.9.9.9h2.6L13.2 4.7ZM8.2 11.2c0-.5.4-.9.9-.9h5.8a1 1 0 1 1 0 2H9.1a.9.9 0 0 1-.9-.9Zm0 3.8c0-.5.4-.9.9-.9h5.8a1 1 0 1 1 0 2H9.1a.9.9 0 0 1-.9-.9Z"/></svg>',
    "settings": '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M11 2.9a1 1 0 0 1 2 0v1.3a7.8 7.8 0 0 1 2.1.9l.9-.9a1 1 0 1 1 1.4 1.4l-.9.9c.4.6.7 1.4.9 2.1h1.3a1 1 0 1 1 0 2h-1.3a7.8 7.8 0 0 1-.9 2.1l.9.9a1 1 0 1 1-1.4 1.4l-.9-.9c-.6.4-1.4.7-2.1.9v1.3a1 1 0 1 1-2 0v-1.3a7.8 7.8 0 0 1-2.1-.9l-.9.9a1 1 0 1 1-1.4-1.4l.9-.9a7.8 7.8 0 0 1-.9-2.1H3.6a1 1 0 1 1 0-2h1.3c.2-.8.5-1.5.9-2.1l-.9-.9A1 1 0 0 1 6.3 4l.9.9c.6-.4 1.4-.7 2.1-.9V2.9Zm1 5.1a3.8 3.8 0 1 0 0 7.7 3.8 3.8 0 0 0 0-7.7Z"/></svg>',
    "code": '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M9.4 17.6a1 1 0 0 1-1.4 0l-4.9-4.9a1 1 0 0 1 0-1.4l4.9-4.9a1 1 0 1 1 1.4 1.4L5.2 12l4.2 4.2a1 1 0 0 1 0 1.4Zm5.2 0a1 1 0 0 1 0-1.4l4.2-4.2-4.2-4.2a1 1 0 1 1 1.4-1.4l4.9 4.9a1 1 0 0 1 0 1.4l-4.9 4.9a1 1 0 0 1-1.4 0Z"/></svg>',
}

PAGES = [
    {"route": "/", "template": "home.html", "title": "Home", "icon": ICONS["home"]},
    {
        # The sidebar "Life Data Analysis" link goes straight to the Perform an
        # Analysis workspace. The workflow cards that used to sit on a separate
        # landing page live on Home now, so that is the other way in.
        "route": "/life-data-analysis/perform-analysis",
        "template": "perform_analysis.html",
        "title": "Life Data Analysis",
        "icon": ICONS["trend"],
    },
    {"route": "/metrics", "template": "metrics.html", "title": "Metrics", "icon": ICONS["chart"]},
    {
        "route": "/standards-and-documentation",
        "template": "standards_and_documentation.html",
        "title": "Standards and Documentation",
        "icon": ICONS["docs"],
    },
    {
        "route": "/reliability-links",
        "template": "reliability_links.html",
        "title": "Reliability Links",
        "icon": ICONS["docs"],
    },
    {
        # Named "Configuration" rather than "Settings": most of what is on it is
        # structural plant configuration -- shift schedules, group membership,
        # linked-downtime rules -- rather than the personal preferences
        # "Settings" promises. /settings still resolves, as a redirect.
        "route": "/configuration",
        "template": "configuration.html",
        "title": "Configuration",
        "icon": ICONS["settings"],
    },
    {
        "route": "/developer",
        "template": "developer_home.html",
        "title": "Developer",
        "icon": ICONS["code"],
    }
]



# Placeholder service wiring. Replace with dependency-injected instances and real DB connections.
reliability_service = ReliabilityService(
    metrics_repo=MetricsRepository(),
    failure_repo=FailureRepository(),
    analysis_repo=AnalysisRepository(),
)

# The Life Data Analysis / Weibull backend reuses the desktop GUI's LifeDataService
# unchanged. Both open the single default database location, which can be overridden
# with the GREMLIN_DB_PATH environment variable so the Flask app can run wherever
# GREMLIN.db is reachable. The service is created lazily and any startup failure is
# surfaced to the page instead of crashing the whole app.
MLE_CALCULATION_PASSWORD = "1336"
_life_data_service: LifeDataService | None = None
_life_data_service_error: str | None = None

_schema_service: SchemaService | None = None
_availability_repository: AvailabilityRepository | None = None

def _on_sync_succeeded() -> None:
    """Drop cached state that a completed sync has just made wrong.

    A sync maps through a LifeDataService of its own, so the one this app holds
    never learns that the mapped table grew. Its asset list is cached in memory,
    and stale is the worst outcome available here: the sync would report hundreds
    of records mapped while the Life Data page kept insisting the new assets do
    not exist. Runs before the job reports success, so a page that reacts to the
    finish reads fresh data.
    """

    if _life_data_service is not None:
        _life_data_service.invalidate_caches()


# One shared runner for the whole process, so a sync started by one browser tab
# is visible (and un-duplicatable) from every other. The nightly Task Scheduler
# job is a separate process and is not covered by it; see services/sync_service.py.
sync_runner = LimbleSyncRunner(on_success=_on_sync_succeeded)

# The release history lives in a Word document on the engineering share, not in
# this repository: a team member edits it and the change is live on the next page
# load. One reader for the whole process, because it caches the parse against the
# file's own timestamp -- see services/patch_notes_service.py.
patch_notes_reader = build_patch_notes_reader()


def _configured_db_path() -> Path:
    """Return the database GREMLIN opens: GREMLIN_DB_PATH if set, else the default.

    Error messages report this path, so they always name the file that was
    actually tried rather than the default an override replaced.
    """

    override = os.environ.get("GREMLIN_DB_PATH")
    return Path(override) if override else DEFAULT_DB_PATH


def get_life_data_service() -> LifeDataService:
    """Return a shared LifeDataService, building it on first use."""

    global _life_data_service, _life_data_service_error
    if _life_data_service is not None:
        return _life_data_service
    db_path = _configured_db_path()
    try:
        # With no override, never let SQLite create a brand-new empty database at
        # the default location (on Linux a Windows path is just an ordinary local
        # filename, so the miss would go unnoticed). An explicit GREMLIN_DB_PATH is
        # the operator's own choice and is passed through as given -- including
        # when it names the default path -- so SQLite may create that file.
        if not os.environ.get("GREMLIN_DB_PATH") and not db_path.is_file():
            raise RuntimeError(
                f"GREMLIN.db was not found at {db_path}. "
                "Set the GREMLIN_DB_PATH environment variable to a valid GREMLIN.db file."
            )
        _life_data_service = LifeDataService(db_path=db_path, refresh_on_startup=False)
        _life_data_service_error = None
    except Exception as exc:  # noqa: BLE001 - surfaced to the client as JSON
        _life_data_service_error = str(exc)
        raise
    return _life_data_service


class LifeDataApiError(Exception):
    """Wrap a service failure with an HTTP status for the JSON API."""

    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def life_data_api(view):
    """Translate service/validation errors into consistent JSON error payloads."""

    @wraps(view)
    def wrapped(*args, **kwargs):
        try:
            return view(*args, **kwargs)
        except LifeDataApiError as exc:
            return jsonify({"error": exc.message}), exc.status_code
        except DatabaseWriteError as exc:
            return jsonify({"error": str(exc)}), 503
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        except Exception as exc:  # noqa: BLE001 - last-resort guard for the API
            return jsonify({"error": f"Unexpected error: {exc}"}), 500

    return wrapped


def _service_or_api_error() -> LifeDataService:
    try:
        return get_life_data_service()
    except Exception as exc:  # noqa: BLE001
        raise LifeDataApiError(
            f"GREMLIN could not open the analysis database at {_configured_db_path()}. "
            f"Set GREMLIN_DB_PATH to a reachable GREMLIN.db file. Details: {exc}",
            status_code=503,
        ) from exc


def _required_asset() -> str:
    asset_number = (request.values.get("asset") or "").strip()
    if not asset_number:
        raise LifeDataApiError("Select an Asset Number first.", status_code=400)
    return asset_number


def _disposition_kind() -> str:
    kind = (request.values.get("kind") or "").strip().lower()
    if kind not in {"wo", "pm"}:
        raise LifeDataApiError("Disposition kind must be 'wo' or 'pm'.", status_code=400)
    return kind


# Pages that exist and are routable but are deliberately kept out of the sidebar
# navigation. Standards and Documentation is reached from the Life Data Analysis
# landing page instead; the developer area is reached from the account dialog
# behind the person icon, and only when the signed-in account is an
# administrator. Typing the URL still works, but only for that same account --
# every developer route checks the role for itself.
UNLISTED_ROUTES = {"/standards-and-documentation", "/developer"}

NAV_LINKS = [
    {"label": page["title"], "url": page["route"], "icon": page["icon"]}
    for page in PAGES
    if page["route"] not in UNLISTED_ROUTES
]

# The footer's own navigation. These pages are about GREMLIN rather than part of
# the reliability workflow, so they are deliberately kept out of PAGES and out of
# the sidebar: the footer at the bottom of every page is the only route to them,
# the way About/Contact is on an ordinary website.
FOOTER_LINKS = [
    {"label": "About", "url": "/about"},
    {"label": "Patch Notes", "url": "/patch-notes"},
    {"label": "Report a Bug", "url": "/report-a-bug"},
]


# Who to ask about GREMLIN. The same three people answer both questions the app
# cannot answer for itself -- "how do I get a login?" and "why does this page do
# that?" -- so the list lives here once and is drawn twice: in the login dialog,
# where somebody without an account is already stuck, and in the footer, where
# somebody with one can find it from any page.
SUPPORT_CONTACTS = [
    {"name": "Tim Kim", "email": "tim.kim@sandc.com"},
    {"name": "Sania Sohail", "email": "sania.sohail@sandc.com"},
    {"name": "Billy Trinh", "email": "billy.trinh@sandc.com"},
]


# The topbar's quick links: the systems people leave GREMLIN for, rather than
# anywhere inside it. They are deliberately not in NAV_LINKS or the search
# catalog -- both of those are about pages this app serves, and an entry that
# silently takes you off-site does not belong in either. "host" is what the
# dropdown prints under the title, so the destination is legible before the
# click rather than only in the status bar.
QUICK_LINKS = [
    {
        "label": "S&C SourceOne",
        "url": "https://sandc4.sharepoint.com/sites/SourceOne",
        "host": "sandc4.sharepoint.com",
    },
    {
        "label": "M.O.M",
        "url": "https://mom.sandc.ws/",
        "host": "mom.sandc.ws",
    },
]


@app.context_processor
def footer_context():
    """Give every template the footer's links without threading them through each view.

    base.html draws the footer on every page, so the alternative is adding two more
    keyword arguments to all thirty-odd render_template calls and breaking the footer
    on whichever one gets missed.
    """
    return {
        "footer_links": FOOTER_LINKS,
        "current_year": date.today().year,
        # Same reasoning, for the topbar: it is drawn on every page too.
        "quick_links": QUICK_LINKS,
        # And for the help contacts, which the footer and the login dialog both
        # draw -- the login dialog being part of the sidebar, which is likewise
        # on every page.
        "support_contacts": SUPPORT_CONTACTS,
    }


# The catalog behind the topbar's global search. Two kinds of entry: a "page" is
# somewhere the sidebar or the footer could have taken you, and a "function" is a
# single thing GREMLIN does, deep-linked to the control that does it. Keeping both
# in one list is the point -- somebody who wants to disposition a work order
# should not first have to work out whether that is a page of its own or a button
# on one.
#
# `role` gates an entry exactly the way the route behind it is gated, so the
# dropdown never offers a door that answers with a 403. Entries anyone may open
# leave it out. `keywords` are never displayed: they exist so a search for the
# word somebody actually has in mind ("uptime", "changelog", "sql") reaches the
# entry whose visible label uses a different one.
#
# Every URL here is opened by a click in the dropdown, so a deep link has to
# survive a cold page load: query strings are read by the page's own script, and
# fragments must name an element that is present and visible before any data
# arrives. That rules out the Life Data Analysis panels, which stay hidden until
# an asset is picked -- those entries preselect the analysis type instead.
SEARCH_ENTRIES = [
    # -- Pages ---------------------------------------------------------------
    {
        "label": "Home",
        "url": "/",
        "kind": "page",
        "context": "GREMLIN",
        "keywords": ["dashboard", "start", "overview", "landing", "workflows", "main"],
    },
    {
        "label": "Perform an Analysis",
        "url": "/life-data-analysis/perform-analysis",
        "kind": "page",
        "context": "Life Data Analysis",
        "keywords": ["weibull", "life data", "lda", "curve fitting", "reliability", "run analysis"],
    },
    {
        "label": "Disposition",
        "url": "/life-data-analysis/disposition",
        "kind": "page",
        "context": "Life Data Analysis",
        "role": "editor",
        "keywords": ["work order", "wo", "pm reset", "failure mode", "failure mechanism", "classify"],
    },
    {
        "label": "Metrics",
        "url": "/metrics",
        "kind": "page",
        "context": "Dashboards",
        "keywords": ["kpi", "mtbf", "mttr", "downtime", "availability", "risk", "charts"],
    },
    {
        "label": "Standards and Documentation",
        "url": "/standards-and-documentation",
        "kind": "page",
        "context": "Reference",
        "keywords": ["formula", "calculation", "method", "definition", "how it works", "docs"],
    },
    {
        "label": "Reliability Links",
        "url": "/reliability-links",
        "kind": "page",
        "context": "Reference",
        "keywords": ["limble", "power bi", "sharepoint", "bookmarks", "resources", "cmms"],
    },
    {
        "label": "Failure Classification",
        "url": "/life-data-analysis/failure-classification",
        "kind": "page",
        "context": "Life Data Analysis",
        "keywords": ["mode", "subsystem", "severity", "corrective action", "taxonomy"],
    },
    {
        "label": "PM Calendar",
        "url": "/pm-calendar",
        "kind": "page",
        "context": "Maintenance",
        "keywords": ["preventive maintenance", "schedule", "upcoming", "due date", "frequency"],
    },
    {
        "label": "Configuration",
        "url": "/configuration",
        "kind": "page",
        "context": "GREMLIN",
        # "settings" is kept as a keyword deliberately: the page answered to that
        # name for long enough that it is what people will still type.
        "keywords": ["settings", "preferences", "options", "workspace"],
    },
    {
        "label": "About",
        "url": "/about",
        "kind": "page",
        "context": "About this site",
        "keywords": ["what is gremlin", "information"],
    },
    {
        "label": "Patch Notes",
        "url": "/patch-notes",
        "kind": "page",
        "context": "About this site",
        "keywords": ["changelog", "release", "version", "whats new", "updates"],
    },
    {
        "label": "Report a Bug",
        "url": "/report-a-bug",
        "kind": "page",
        "context": "About this site",
        "keywords": ["issue", "feedback", "support", "broken", "problem"],
    },
    {
        "label": "Developer",
        "url": "/developer",
        "kind": "page",
        "context": "Developer",
        "role": "admin",
        "keywords": ["admin", "internals", "tools"],
    },
    {
        "label": "Bug reports",
        "url": "/developer/bugs",
        "kind": "page",
        "context": "Developer",
        "role": "admin",
        "keywords": ["bugs", "issues", "open", "resolved", "triage", "reports"],
    },
    {
        "label": "Database inspection",
        "url": "/developer/database",
        "kind": "page",
        "context": "Developer",
        "role": "admin",
        "keywords": ["table", "row", "sql", "schema", "drift", "gremlin.db"],
    },
    {
        "label": "Limble sync",
        "url": "/developer/limble-sync",
        "kind": "page",
        "context": "Developer",
        "role": "admin",
        "keywords": ["cmms", "import", "pipeline", "refresh", "api", "pull data"],
    },
    {
        "label": "Access control",
        "url": "/developer/access",
        "kind": "page",
        "context": "Developer",
        "role": "admin",
        "keywords": ["user", "role", "pin", "account", "permission", "editor", "admin"],
    },
    {
        "label": "User activity",
        "url": "/developer/activity",
        "kind": "page",
        "context": "Developer",
        "role": "admin",
        "keywords": ["audit", "log", "history", "who changed what"],
    },

    # -- Functions -----------------------------------------------------------
    {
        "label": "Weibull Analysis",
        "url": "/life-data-analysis/perform-analysis?analysis=Weibull+Analysis",
        "kind": "function",
        "context": "Perform an Analysis",
        "keywords": ["beta", "eta", "b10", "b50", "mttf", "reliability curve", "distribution"],
    },
    {
        "label": "Failure Mode Trend Analysis",
        "url": "/life-data-analysis/perform-analysis?analysis=Failure+Mode+Trend+Analysis",
        "kind": "function",
        "context": "Perform an Analysis",
        "keywords": ["monthly trend", "fastest growing", "recurring", "failure mode"],
    },
    {
        "label": "Downtime Driver Analysis",
        "url": "/life-data-analysis/perform-analysis?analysis=Downtime+Driver+Analysis",
        "kind": "function",
        "context": "Perform an Analysis",
        "keywords": ["downtime hours", "distribution", "top events", "by location"],
    },
    {
        "label": "PM Effectiveness Analysis",
        "url": "/life-data-analysis/perform-analysis?analysis=PM+Effectiveness+Analysis",
        "kind": "function",
        "context": "Perform an Analysis",
        "keywords": ["failures following pm", "preventive maintenance", "pm to failure"],
    },
    {
        "label": "Disposition Work Orders",
        "url": "/life-data-analysis/disposition?kind=wo",
        "kind": "function",
        "context": "Disposition",
        "role": "editor",
        "keywords": ["wo", "corrective", "assign failure mode", "record class"],
    },
    {
        "label": "Disposition PM Resets",
        "url": "/life-data-analysis/disposition?kind=pm",
        "kind": "function",
        "context": "Disposition",
        "role": "editor",
        "keywords": ["pm", "reset decision", "preventive maintenance"],
    },
    {
        "label": "Reliability performance by asset",
        "url": "/metrics#card-kpis",
        "kind": "function",
        "context": "Metrics",
        "keywords": ["mtbf", "mttr", "work order count", "total downtime", "kpi summary"],
    },
    {
        "label": "Reliability risk by asset",
        "url": "/metrics#card-alerts",
        "kind": "function",
        "context": "Metrics",
        "keywords": ["risk score", "downtime spike", "alert", "threshold", "baseline"],
    },
    {
        "label": "Asset availability by month",
        "url": "/metrics#card-availability",
        "kind": "function",
        "context": "Metrics",
        "keywords": ["uptime", "goal", "overtime", "ot", "scheduled hours"],
    },
    # The three editing sections of Configuration. They are drawn for an editor
    # and left out of the page entirely for anyone else, so they are gated here
    # to match: pointing a viewer at a fragment naming an element their copy of
    # the page does not contain would land them at the top of it with no
    # explanation. Each fragment names the collapsed panel rather than a heading
    # inside it, which is what lets the page open the one that was asked for.
    {
        "label": "Asset group schedules",
        "url": "/configuration#config-availability",
        "kind": "function",
        "context": "Configuration",
        "role": "editor",
        "keywords": ["shift", "operating hours", "availability basis", "calendar"],
    },
    {
        "label": "Linked downtime rules",
        "url": "/configuration#config-linked",
        "kind": "function",
        "context": "Configuration",
        "role": "editor",
        "keywords": ["linked work order", "double count", "downtime rule"],
    },
    {
        "label": "Refresh CMMS mapping",
        "url": "/configuration#config-cmms",
        "kind": "function",
        "context": "Configuration",
        "role": "editor",
        "keywords": ["limble", "asset mapping", "remap", "reimport"],
    },
    {
        "label": "Metrics Dashboard formulas",
        "url": "/standards-and-documentation#metrics-panel",
        "kind": "function",
        "context": "Standards and Documentation",
        "keywords": ["mtbf formula", "mttr formula", "risk score", "percent change"],
    },
    {
        "label": "Weibull Analysis formulas",
        "url": "/standards-and-documentation#weibull-panel",
        "kind": "function",
        "context": "Standards and Documentation",
        "keywords": ["beta", "eta", "observation life", "failure indicator", "b10", "b50"],
    },
    {
        "label": "Pareto and Trends formulas",
        "url": "/standards-and-documentation#trends-panel",
        "kind": "function",
        "context": "Standards and Documentation",
        "keywords": ["pareto", "cumulative percent", "monthly trend", "downtime driver"],
    },
    {
        "label": "Data Sources",
        "url": "/standards-and-documentation#data-panel",
        "kind": "function",
        "context": "Standards and Documentation",
        "keywords": ["where the numbers come from", "asset filter", "work order date", "limble"],
    },
]


def _search_index(is_admin: bool, can_edit: bool, has_account: bool) -> list[dict]:
    """The search catalog as the signed-in account may see it.

    Role filtering happens here rather than in the browser so an entry the
    account cannot open is never sent to it in the first place -- hiding it with
    a script would put the whole developer catalog in the page source of every
    anonymous visitor.
    """
    allowed = {None}
    if can_edit:
        allowed.add("editor")
    if is_admin:
        allowed.update({"editor", "admin"})

    entries = [
        {
            "label": entry["label"],
            "url": entry["url"],
            "kind": entry["kind"],
            "context": entry["context"],
            # One space-joined string: the browser only ever substring-matches
            # against it, and a list would cost bytes on every page for nothing.
            "keywords": " ".join(entry["keywords"]),
        }
        for entry in SEARCH_ENTRIES
        if entry.get("role") in allowed
    ]

    # The account dialog has no URL of its own -- it is a <dialog> the sidebar
    # opens -- so this entry names the action instead and global_search.js opens
    # it. Logging in is the one thing a visitor with no account most often wants
    # and the person icon at the foot of the sidebar is the only other way in.
    entries.append({
        "label": "Log out" if has_account else "Log in",
        "url": "#account",
        "kind": "function",
        "context": "Account",
        "keywords": "account sign in sign out session role user",
    })
    return entries


def _account_search_index() -> list[dict]:
    """The search catalog for whoever is asking, resolved from the session."""
    user = _resolved_account()
    level = ROLE_LEVEL.get(user["role"], -1) if user else -1
    return _search_index(
        is_admin=level >= ROLE_LEVEL["admin"],
        can_edit=level >= ROLE_LEVEL["editor"],
        has_account=bool(user),
    )


def _search_matches(query: str, entries: list[dict]) -> list[dict]:
    """Rank `entries` against `query`, best first, dropping the ones that miss.

    Deliberately the same rules global_search.js applies, so the no-script page
    and the dropdown agree: every whitespace-separated term must appear
    somewhere in an entry, and where it appears decides how much it is worth --
    the start of the label beats the middle of it, which beats a keyword nobody
    can see. Summing per-term scores is what makes a two-word query rank the
    entry matching both above one matching either.
    """
    terms = query.lower().split()
    if not terms:
        return []

    ranked = []
    for position, entry in enumerate(entries):
        label = entry["label"].lower()
        context = entry["context"].lower()
        keywords = entry["keywords"].lower()
        total = 0
        for term in terms:
            if label == term:
                score = 100
            elif label.startswith(term):
                score = 80
            elif any(word.startswith(term) for word in label.split()):
                score = 65
            elif term in label:
                score = 50
            elif any(word.startswith(term) for word in keywords.split()):
                score = 40
            elif term in keywords:
                score = 30
            elif term in context:
                score = 20
            else:
                total = 0
                break
            total += score
        if total:
            # Shorter labels win a tie: "Metrics" is a better answer to "metric"
            # than "Metrics Dashboard formulas" is. Catalog order breaks the rest,
            # which keeps pages ahead of the functions that live on them.
            ranked.append((-total, len(entry["label"]), position, entry))

    return [entry for _, _, _, entry in sorted(ranked, key=lambda row: row[:3])]


RELEVANT_LINKS = [
    {
        "label": "Limble CMMS",
        "url": "https://your-limble-url"
    },
    {
        "label": "Power BI Dashboards",
        "url": "#"
    },
    {
        "label": "SharePoint",
        "url": "#"
    },
    {
        "label": "Standards & Documentation",
        "url": "/standards-and-documentation"
    }
]

@app.route("/")
def home():
    return render_template("home.html", page_title="Home", nav_links=NAV_LINKS)


@app.post("/auth/login")
def login():
    # JSON is deliberately required. Cross-origin HTML forms cannot send this
    # content type, and a scripted cross-origin request must pass a CORS
    # preflight that GREMLIN does not allow, preventing login CSRF/session swaps.
    if not request.is_json:
        return jsonify({"error": "Login credentials must be sent as JSON."}), 415
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"error": "Login credentials must be a JSON object."}), 400
    if not access_control.has_users():
        return jsonify({
            "error": "No accounts are configured. Set GREMLIN_ADMIN_USERNAME and "
                     "GREMLIN_ADMIN_PIN in the deployment environment or .env, then restart GREMLIN."
        }), 503
    username = str(payload.get("username", "")).strip()
    pin = str(payload.get("pin", ""))
    user, retry_after = access_control.authenticate_limited(username, pin, request.remote_addr or "unknown")
    if not user:
        if retry_after:
            response = jsonify({"error": "Too many login attempts. Try again later."})
            response.headers["Retry-After"] = str(retry_after)
            return response, 429
        return jsonify({"error": "Incorrect username or PIN."}), 401
    session.clear()
    session["user"] = user
    session.permanent = False
    return jsonify({"user": user})


@app.post("/auth/logout")
@requires_csrf
def logout():
    # Same protection as /developer/lock, which does the same thing. SameSite
    # keeps the session off a cross-*site* form post, but a hostile page on
    # another origin of the same site is still same-site, and being signed out
    # mid-edit by one is a nuisance somebody would have to diagnose. The token
    # is what that page cannot obtain.
    session.clear()
    return jsonify({"ok": True})


@app.route("/life-data-analysis")
def life_data_analysis():
    # The "Choose a reliability workflow" landing page now *is* the home page:
    # its cards were moved there wholesale. The route survives only so existing
    # bookmarks and links land somewhere sensible instead of on a 404.
    return redirect(url_for("home"))




@app.route("/life-data-analysis/perform-analysis")
def perform_analysis():
    return render_template(
        "perform_analysis.html",
        page_title="Perform an Analysis",
        nav_links=NAV_LINKS,
    )


@app.route("/life-data-analysis/disposition")
def disposition():
    # The whole page is an editor: every table on it writes dispositions back to
    # GREMLIN.db. There is nothing here to show a viewer with the controls taken
    # away, so the page is refused rather than rendered read-only. Home still
    # shows the card to a viewer, but as a button that explains the role instead
    # of a link; this is what a typed or bookmarked URL meets.
    _user, error = _authorised_user("editor")
    if error:
        return (
            render_template(
                "not_authorized.html",
                page_title="Disposition",
                page_heading="Disposition is for editors.",
                required_role="editor",
                back_url=url_for("perform_analysis"),
                nav_links=NAV_LINKS,
            ),
            403,
        )
    return render_template(
        "disposition.html",
        page_title="Disposition",
        nav_links=NAV_LINKS,
    )


@app.route("/life-data-analysis/failure-classification")
def failure_classification():
    classification_data = reliability_service.get_failure_classification_data()
    return render_template(
        "failure_classification.html",
        page_title="Failure Classification",
        nav_links=NAV_LINKS,
        classification_data=classification_data,
    )

@app.route("/metrics")
def metrics():
    return render_template(
        "metrics.html",
        page_title="Metrics",
        nav_links=NAV_LINKS,
    )


@app.route("/metrics/api/reliability")
@life_data_api
def api_metrics_reliability():
    """Per-asset reliability KPIs for the Metrics dashboard.

    Reuses the shared maintenance dataset through the same LifeDataService the
    Life Data Analysis pages use. ``assets`` (repeatable or comma-separated) and
    ``start``/``end`` (YYYY-MM-DD) narrow the result; omitting ``assets`` returns
    every asset so the client can default to comparing them all.
    """

    service = _service_or_api_error()
    raw_assets = request.values.getlist("assets")
    asset_numbers: list[str] = []
    for value in raw_assets:
        asset_numbers.extend(part.strip() for part in str(value).split(",") if part.strip())
    start = (request.values.get("start") or "").strip() or None
    end = (request.values.get("end") or "").strip() or None
    return jsonify(
        service.asset_reliability_metrics(
            asset_numbers=asset_numbers or None,
            start_date=start,
            end_date=end,
        )
    )


# ---------------------------------------------------------------------------
# Availability Dashboard JSON API
#
# The Availability card is deliberately independent of the Metrics page's shared
# asset and date filters: it always renders every configured group over whole
# calendar months, because the Average line is a group-level statistic and
# linked downtime makes an asset's number depend on assets a filter could hide.
# See docs/availability-dashboard-design.md §2.2-2.3.
#
# Nothing derived is stored, so every request recomputes against the current
# schedule. Config edits therefore apply to all months, including reported ones.
# ---------------------------------------------------------------------------


def get_availability_repository() -> AvailabilityRepository:
    """Return a shared AvailabilityRepository for the configured database.

    Rebuilt when the configured path changes so the card can never read a
    different database than the rest of the site.
    """

    global _availability_repository
    db_path = _configured_db_path()
    if _availability_repository is None or _availability_repository.db_path != db_path:
        if not os.environ.get("GREMLIN_DB_PATH") and not db_path.is_file():
            raise RuntimeError(
                f"GREMLIN.db was not found at {db_path}. "
                "Set the GREMLIN_DB_PATH environment variable to a valid GREMLIN.db file."
            )
        repository = AvailabilityRepository(db_path)
        repository.ensure_schema()
        _availability_repository = repository
    return _availability_repository


def _bootstrap_mapped_records() -> None:
    """Bring mapped_cmms_record up to date before availability reads it.

    The Availability card is the Metrics page's *first* request -- it also
    supplies the equipment list the other cards default to -- so it can no
    longer rely on some earlier endpoint having constructed LifeDataService.
    Two things only that construction does:

    * ``ensure_schema`` adds columns a database predating a migration lacks;
    * ``ensure_mapped_records_available`` maps raw rows when the mapped table is
      still empty, which is the state right after an import.

    Without this the first visit after an upgrade fails on a missing column, and
    the first visit after an import reports "no work orders imported" while the
    raw rows sit there unmapped. Neither is retried once another request repairs
    things, so the card stays wrong for that whole page view.
    """

    get_life_data_service().ensure_mapped_records_available()


def availability_api(view):
    """Normalise availability endpoint errors into the page's JSON shape."""

    @wraps(view)
    def wrapped(*args, **kwargs):
        try:
            return view(*args, **kwargs)
        except AvailabilityConfigError as exc:
            return jsonify({"error": str(exc)}), 400
        except DatabaseWriteError as exc:
            return jsonify({"error": str(exc)}), 503
        except RuntimeError as exc:
            return jsonify({"error": str(exc)}), 503
        except Exception as exc:  # noqa: BLE001 - surfaced to the client as JSON
            return jsonify({"error": f"Unexpected error: {exc}"}), 500

    return wrapped


def _json_body() -> dict:
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        raise AvailabilityConfigError("Expected a JSON object body.")
    return body


def _required_number(body: dict, key: str) -> float:
    if key not in body:
        raise AvailabilityConfigError(f"Missing '{key}'.")
    try:
        return float(body[key])
    except (TypeError, ValueError) as exc:
        raise AvailabilityConfigError(f"'{key}' must be a number.") from exc


@app.route("/metrics/api/availability")
@availability_api
def api_availability():
    """Monthly availability for every configured asset group.

    ``months`` sets the window length and defaults to the five most recent
    complete months, clamped to the months that hold work-order data.
    """

    repository = get_availability_repository()
    _bootstrap_mapped_records()
    months = clamp_window_months(request.values.get("months"))
    return jsonify(build_dashboard(repository, months=months))


@app.route("/metrics/api/availability/work-orders")
@availability_api
def api_availability_work_orders():
    """The individual work orders behind one bar of one availability chart.

    Fetched only when a reader opens a bar, not shipped with the chart payload:
    the card draws several hundred asset-months and each one's work orders carry
    names, descriptions and completion notes.
    """

    repository = get_availability_repository()
    _bootstrap_mapped_records()
    return jsonify(
        build_work_order_detail(
            repository,
            asset_group=str(request.values.get("asset_group") or "").strip(),
            asset_number=str(request.values.get("asset_number") or "").strip(),
            month=request.values.get("month"),
        )
    )


@app.route("/metrics/api/availability/config")
@availability_api
def api_availability_config():
    return jsonify(build_config(get_availability_repository()))


@app.route("/metrics/api/availability/config/group/<path:asset_group>", methods=["PUT"])
@availability_api
@requires_role("editor")
def api_availability_save_group(asset_group: str):
    """Update one group's shift schedule and, optionally, its membership.

    Net hours per day is derived from the parts on every read, so it is
    intentionally not accepted here -- a stored net could drift from the
    schedule/break/lunch/setup values it is supposed to summarise.
    """

    body = _json_body()
    repository = get_availability_repository()
    assets = body.get("asset_numbers")
    if assets is not None and not isinstance(assets, list):
        raise AvailabilityConfigError("'asset_numbers' must be a list.")
    repository.save_group_schedule(
        asset_group,
        schedule_hours_per_day=_required_number(body, "schedule_hours_per_day"),
        break_hours_per_day=_required_number(body, "break_hours_per_day"),
        lunch_hours_per_day=_required_number(body, "lunch_hours_per_day"),
        setup_hours_per_day=_required_number(body, "setup_hours_per_day"),
        include=bool(body.get("include", True)),
        asset_numbers=assets,
    )
    return jsonify(build_config(repository))


@app.route("/metrics/api/availability/config/group/<path:asset_group>/reset", methods=["POST"])
@availability_api
@requires_role("editor")
def api_availability_reset_group(asset_group: str):
    if not request.is_json:
        return jsonify({"error": "Reset requests must be sent as JSON."}), 415
    repository = get_availability_repository()
    repository.reset_group_to_defaults(asset_group)
    return jsonify(build_config(repository))


@app.route("/metrics/api/availability/goal", methods=["PUT"])
@availability_api
@requires_role("editor")
def api_availability_save_goal():
    body = _json_body()
    group = str(body.get("asset_group") or "").strip()
    month = str(body.get("month") or "").strip()
    if not group or not month:
        raise AvailabilityConfigError("Both 'asset_group' and 'month' are required.")
    repository = get_availability_repository()
    repository.save_goal(group, month, _required_number(body, "goal_percent"))
    return jsonify({"ok": True})


@app.route("/metrics/api/availability/ot", methods=["PUT"])
@availability_api
@requires_role("editor")
def api_availability_save_ot():
    body = _json_body()
    asset = str(body.get("asset_number") or "").strip()
    month = str(body.get("month") or "").strip()
    if not asset or not month:
        raise AvailabilityConfigError("Both 'asset_number' and 'month' are required.")
    repository = get_availability_repository()
    repository.save_manual_ot(asset, month, _required_number(body, "hours"))
    return jsonify({"ok": True})


@app.route("/metrics/api/availability/display-name", methods=["PUT"])
@availability_api
@requires_role("editor")
def api_availability_save_display_name():
    body = _json_body()
    asset = str(body.get("asset_number") or "").strip()
    if not asset:
        raise AvailabilityConfigError("'asset_number' is required.")
    repository = get_availability_repository()
    repository.save_display_name(asset, str(body.get("display_name") or ""))
    return jsonify({"ok": True})


@app.route("/metrics/api/availability/linked-rules", methods=["PUT"])
@availability_api
@requires_role("editor")
def api_availability_save_linked_rules():
    body = _json_body()
    rules = body.get("rules")
    if not isinstance(rules, list):
        raise AvailabilityConfigError("'rules' must be a list.")
    repository = get_availability_repository()
    repository.replace_linked_rules(rules)
    return jsonify(build_config(repository))


@app.route("/standards-and-documentation")
def standards_and_documentation():
    return render_template(
        "standards_and_documentation.html",
        page_title="Standards and Documentation",
        nav_links=NAV_LINKS,
    )


@app.route("/configuration")
def configuration():
    return render_template("configuration.html", page_title="Configuration", nav_links=NAV_LINKS)


@app.route("/settings")
def settings():
    # The page is called Configuration now. The old route survives only so
    # existing bookmarks and anything that linked to a section of it land on the
    # renamed page instead of on a 404.
    #
    # A fragment on such a bookmark never reaches us -- the browser keeps it --
    # but it does not simply evaporate either: the browser re-applies it to the
    # address this redirect names, so /settings#settings-linked-heading becomes
    # /configuration#settings-linked-heading, naming an element the renamed page
    # does not have. configuration.html translates those three old names, which
    # is the only place that can: by the time the fragment matters, the request
    # is long over.
    return redirect(url_for("configuration"))


# ---------------------------------------------------------------------------
# Footer pages. Reached from the footer on every page and from nowhere else --
# they are not in PAGES, so they never appear in the sidebar. The templates are
# placeholders for now: the shell exists so the links resolve instead of 404ing
# while the copy is still being written.
# ---------------------------------------------------------------------------


@app.route("/search")
def search_results():
    """The topbar search box's destination when it is submitted rather than clicked.

    The dropdown answers every search that goes through the keyboard or the
    mouse, so nothing that works reaches here: this is the page for a visitor
    whose browser ran no script, and for anyone who hits Enter before a result is
    highlighted. It searches the same catalog by the same rules, so the answer is
    the one the dropdown would have given.
    """
    query = (request.args.get("q") or "").strip()
    # Entries whose "URL" is a bare fragment name an action the dropdown
    # performs rather than a place to go -- the account dialog has no page of its
    # own. Without a script to perform it, linking to one would be a dead end.
    catalog = [entry for entry in _account_search_index() if not entry["url"].startswith("#")]
    return render_template(
        "search_results.html",
        page_title="Search",
        nav_links=NAV_LINKS,
        query=query,
        results=_search_matches(query, catalog) if query else [],
    )


@app.route("/about")
def about():
    return render_template("about.html", page_title="About", nav_links=NAV_LINKS)


@app.route("/patch-notes")
def patch_notes():
    # A share that cannot be reached is reported by the page rather than by a
    # 500: the visitor is told which document is missing, which is the one thing
    # that gets it fixed. So this view has nothing to catch.
    return render_template(
        "patch_notes.html",
        page_title="Patch Notes",
        nav_links=NAV_LINKS,
        notes=patch_notes_reader.read(),
    )


# The areas the form offers, so a report arrives already sorted by where it
# happened. Drawn from the sidebar's own page titles rather than a second list
# that would drift from it, plus the two answers no page title covers.
def _bug_report_areas() -> list[str]:
    return [page["title"] for page in PAGES if page["route"] not in UNLISTED_ROUTES] + [
        "Signing in",
        "Somewhere else",
    ]


def _bug_form_context(**extra):
    """Everything report_a_bug.html needs, whoever is asking for it."""

    account = _resolved_account()
    return {
        "page_title": "Report a Bug",
        "nav_links": NAV_LINKS,
        # Anonymous visitors reach this page too, so the token is minted here
        # rather than taken from auth_csrf_token, which only exists once
        # somebody has signed in.
        "csrf_token": csrf_token(),
        "areas": _bug_report_areas(),
        "severities": SEVERITIES,
        # Signed in, the name is already known, so the form fills it in rather
        # than asking. Still editable: somebody filing on a colleague's behalf
        # should be able to say so.
        "reporter_name": account["username"] if account else "",
        **extra,
    }


def _bug_form_values() -> dict:
    """What the reporter typed, for a form that has to be drawn again.

    A refused submission -- incomplete, rate limited, or landing on a share that
    is down -- comes back with the boxes still filled, because losing a
    paragraph of description is how somebody gives up on reporting at all.
    """

    return {
        "title": request.form.get("title", ""),
        "description": request.form.get("description", ""),
        "area": request.form.get("area", ""),
        "severity": request.form.get("severity", "minor"),
        "reporter": request.form.get("reporter", ""),
        # Carried through explicitly. The browser cannot recover it: on a
        # re-rendered response document.referrer is the form itself, so a
        # corrected retry would otherwise file the bug against /report-a-bug
        # rather than the page that actually broke.
        "page_url": request.form.get("page_url", ""),
    }


@app.route("/report-a-bug")
def report_a_bug():
    # ?submitted=<id> is where a successful post lands. Redirecting rather than
    # rendering the confirmation straight from the POST means a refresh cannot
    # file the same report twice.
    submitted = request.args.get("submitted", "").strip()
    return render_template(
        "report_a_bug.html",
        **_bug_form_context(submitted_id=submitted if submitted.isdigit() else None),
    )


@app.post("/report-a-bug")
@requires_csrf
def submit_bug_report():
    """File one report from the public form.

    Deliberately open to anyone who can reach GREMLIN, signed in or not: the
    people most likely to hit a bug are the ones who cannot get past it, and a
    report that needs an account is a report that does not get filed. What the
    account adds, when there is one, is who filed it.
    """

    account = _resolved_account()
    try:
        report_id = bug_reports.submit(
            title=request.form.get("title", ""),
            description=request.form.get("description", ""),
            area=request.form.get("area", ""),
            severity=request.form.get("severity", "minor"),
            reporter=request.form.get("reporter", "") or (account["username"] if account else ""),
            reporter_user_id=account["id"] if account else None,
            page_url=request.form.get("page_url", ""),
            user_agent=request.headers.get("User-Agent", ""),
            # Signed-in reporters are limited as themselves rather than as their
            # address: behind a proxy the whole plant shares one, and one
            # person's runaway retry must not spend everybody else's allowance.
            client_key=(
                f"user:{account['id']}" if account else f"addr:{request.remote_addr or 'unknown'}"
            ),
        )
    except BugReportValidationError as exc:
        # Re-render with what was typed still in the boxes. Losing a paragraph
        # of description to a missing title is how a reporter gives up.
        return (
            render_template(
                "report_a_bug.html",
                **_bug_form_context(
                    error=str(exc),
                    form_values=_bug_form_values(),
                ),
            ),
            400,
        )
    except BugReportRateLimitError as exc:
        # 429 with what the reporter typed still in the boxes, so coming back
        # when the window frees up is a resubmit rather than a retype.
        response = make_response(
            render_template(
                "report_a_bug.html",
                **_bug_form_context(error=str(exc), form_values=_bug_form_values()),
            ),
            429,
        )
        response.headers["Retry-After"] = str(exc.retry_after)
        return response
    except BugReportStoreError as exc:
        # The share is down. Say so, and say it on the page rather than as a
        # 500: the reporter needs to know their report was not stored.
        app.logger.exception("A bug report could not be stored")
        return (
            render_template(
                "report_a_bug.html",
                **_bug_form_context(
                    error=str(exc),
                    form_values=_bug_form_values(),
                ),
            ),
            503,
        )
    return redirect(url_for("report_a_bug", submitted=report_id))


# ---------------------------------------------------------------------------
# Life Data Analysis / Weibull JSON API
#
# These endpoints mirror, one-to-one, the LifeDataService calls the desktop GUI
# makes, so the browser workspace performs the exact same backend operations:
# asset selection, readiness summary + Pareto + beta rankings, REL disposition
# editing (with Excel round-trips), Weibull MLE fitting, bulk calculation, and
# saving manual parameter adjustments.
# ---------------------------------------------------------------------------


@app.route("/life-data-analysis/api/assets")
@life_data_api
def api_assets():
    # Auto-map only happens when the mapped table is empty, so an already-populated
    # database does not silently pick up later Limble syncs. Allow the client to
    # request an explicit re-map (mirroring the desktop "Refresh CMMS mapping"
    # action) before reading the asset list.
    if request.values.get("refresh") in ("1", "true", "yes"):
        return jsonify({"error": "Refreshing via GET is not allowed. Use the refresh action."}), 405
    service = _service_or_api_error()
    return jsonify({"assets": service.asset_number_options()})


@app.route("/life-data-analysis/api/refresh-mapping", methods=["POST"])
@life_data_api
@requires_role("editor")
def api_refresh_mapping():
    if not request.is_json:
        return jsonify({"error": "Refresh requests must be sent as JSON."}), 415
    service = _service_or_api_error()
    mapped = service.refresh_mapped_cmms_records()
    return jsonify({"mapped": int(mapped or 0), "assets": service.asset_number_options()})


@app.route("/life-data-analysis/api/summary")
@life_data_api
def api_summary():
    service = _service_or_api_error()
    asset_number = _required_asset()
    summary = service.summary_for_asset(asset_number)
    return jsonify(
        {
            "asset_number": asset_number,
            "summary": {field: getattr(summary, field) for field in summary.__dataclass_fields__},
            "rankings": service.latest_failure_mechanism_beta_rankings(asset_number, limit=5),
            "pareto": service.failure_mechanism_pareto(asset_number),
            "trend": service.failure_mode_trend(asset_number),
        }
    )


@app.route("/life-data-analysis/api/dispositions")
@life_data_api
def api_dispositions():
    service = _service_or_api_error()
    asset_number = _required_asset()
    kind = _disposition_kind()
    scope = (request.values.get("scope") or "all").strip().lower()
    only_needing = scope == "new"
    search = (request.values.get("search") or "").strip()
    page_size = 50
    try:
        page_index = max(0, int(request.values.get("page", 0)))
    except (TypeError, ValueError):
        page_index = 0

    all_count = service.disposition_row_count(asset_number, kind)
    displayed_count = service.disposition_row_count(
        asset_number, kind, only_needing_disposition=only_needing, search=search or None
    )
    max_page_index = max(0, math.ceil(displayed_count / page_size) - 1) if displayed_count else 0
    page_index = min(page_index, max_page_index)
    offset = page_index * page_size
    rows = service.disposition_rows(
        asset_number,
        kind,
        only_needing_disposition=only_needing,
        limit=page_size,
        offset=offset,
        search=search or None,
    )

    wo_record_classes = ["CORRECTIVE_WO", "PM", "INSPECTION", "PARTS_ORDER", "ADMINISTRATIVE", "PROJECT_WORK", "UNKNOWN"]
    pm_record_classes = ["PM", "PM_RESET_CANDIDATE", "INSPECTION", "PARTS_ORDER", "ADMINISTRATIVE", "PROJECT_WORK", "UNKNOWN"]
    return jsonify(
        {
            "asset_number": asset_number,
            "kind": kind,
            "scope": scope,
            "search": search,
            "rows": rows,
            "display_columns": list(DISPLAY_COLUMNS),
            # The narrative boxes travel with their labels rather than as bare
            # column keys: the table renders them as one stacked cell, so it needs
            # to caption each line it draws.
            "narrative_columns": [dict(column) for column in NARRATIVE_COLUMNS],
            "mode_options": service.get_asset_failure_mode_options(asset_number),
            "mechanism_options": service.get_asset_failure_mechanism_options(asset_number),
            "categories": list(PM_DISPOSITION_CATEGORIES if kind == "pm" else WO_DISPOSITION_CATEGORIES),
            "record_classes": pm_record_classes if kind == "pm" else wo_record_classes,
            "pm_reset_decisions": list(PM_RESET_DECISIONS),
            "page_index": page_index,
            "max_page_index": max_page_index,
            "page_size": page_size,
            "offset": offset,
            "displayed_count": displayed_count,
            "all_count": all_count,
        }
    )


@app.route("/life-data-analysis/api/dispositions/save", methods=["POST"])
@life_data_api
@requires_role("editor")
def api_save_dispositions():
    service = _service_or_api_error()
    payload = request.get_json(silent=True) or {}
    dispositions = payload.get("dispositions")
    if not isinstance(dispositions, list) or not dispositions:
        raise LifeDataApiError("No disposition rows were provided to save.", status_code=400)
    saved = service.save_dispositions(dispositions)
    return jsonify({"saved": saved})


@app.route("/life-data-analysis/api/dispositions/excel")
@life_data_api
def api_download_disposition_excel():
    service = _service_or_api_error()
    asset_number = _required_asset()
    kind = _disposition_kind()
    safe_asset = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in asset_number).strip("_") or "asset"
    # Build the workbook in a temp file, then serve it from memory and delete the
    # temp file immediately so repeated downloads never orphan files on disk.
    fd, path = tempfile.mkstemp(suffix=".xlsx", prefix=f"{safe_asset}_{kind}_")
    os.close(fd)
    try:
        service.export_disposition_excel(asset_number, kind, path)
        with open(path, "rb") as handle:
            workbook_bytes = handle.read()
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
    return send_file(
        io.BytesIO(workbook_bytes),
        as_attachment=True,
        download_name=f"{safe_asset}_{kind}_dispositions.xlsx",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@app.route("/life-data-analysis/api/dispositions/excel", methods=["POST"])
@life_data_api
@requires_role("editor")
@requires_csrf
def api_upload_disposition_excel():
    service = _service_or_api_error()
    asset_number = _required_asset()
    kind = _disposition_kind()
    upload = request.files.get("file")
    if upload is None or not upload.filename:
        raise LifeDataApiError("Choose an .xlsx file exported from this disposition screen.", status_code=400)
    fd, path = tempfile.mkstemp(suffix=".xlsx")
    os.close(fd)
    try:
        upload.save(path)
        imported = service.import_disposition_excel(asset_number, kind, path)
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
    return jsonify({"imported": imported})


@app.route("/life-data-analysis/api/pm-effectiveness")
@life_data_api
def api_pm_effectiveness():
    service = _service_or_api_error()
    asset_number = _required_asset()
    mechanism_raw = request.values.get("failure_mechanism_id")
    if mechanism_raw in (None, ""):
        raise LifeDataApiError("Select a failure mechanism to evaluate PM effectiveness.", status_code=400)
    try:
        failure_mechanism_id = int(mechanism_raw)
    except (TypeError, ValueError):
        raise LifeDataApiError("failure_mechanism_id must be an integer.", status_code=400)
    mode_raw = request.values.get("failure_mode_id")
    failure_mode_id = None
    if mode_raw not in (None, ""):
        try:
            failure_mode_id = int(mode_raw)
        except (TypeError, ValueError):
            raise LifeDataApiError("failure_mode_id must be an integer.", status_code=400)
    return jsonify(
        {
            "pm_effectiveness": service.pm_effectiveness(
                asset_number,
                failure_mechanism_id=failure_mechanism_id,
                failure_mode_id=failure_mode_id,
            )
        }
    )


@app.route("/life-data-analysis/api/downtime-drivers")
@life_data_api
def api_downtime_drivers():
    service = _service_or_api_error()
    asset_number = _required_asset()
    # The selected failure mode/mechanism comes from a Pareto bar (both ids) or the
    # Perform Analysis picker (a mode-level choice carries only the mode id), so the
    # mode id is required and the mechanism id is optional.
    mode_raw = request.values.get("failure_mode_id")
    if mode_raw in (None, ""):
        raise LifeDataApiError("Select a failure mode or mechanism to analyze downtime drivers.", status_code=400)
    try:
        failure_mode_id = int(mode_raw)
    except (TypeError, ValueError):
        raise LifeDataApiError("failure_mode_id must be an integer.", status_code=400)
    mechanism_raw = request.values.get("failure_mechanism_id")
    failure_mechanism_id = None
    if mechanism_raw not in (None, ""):
        try:
            failure_mechanism_id = int(mechanism_raw)
        except (TypeError, ValueError):
            raise LifeDataApiError("failure_mechanism_id must be an integer.", status_code=400)
    return jsonify(
        {
            "downtime_drivers": service.downtime_driver_analysis(
                asset_number,
                failure_mechanism_id=failure_mechanism_id,
                failure_mode_id=failure_mode_id,
            )
        }
    )


@app.route("/life-data-analysis/api/weibull-groups")
@life_data_api
def api_weibull_groups():
    service = _service_or_api_error()
    asset_number = _required_asset()
    return jsonify({"groups": service.weibull_group_options(asset_number)})


def _serialize_analysis_result(result) -> dict:
    return {field: getattr(result, field) for field in result.__dataclass_fields__}


@app.route("/life-data-analysis/api/perform-analysis", methods=["POST"])
@life_data_api
@requires_role("editor")
def api_perform_analysis():
    service = _service_or_api_error()
    payload = request.get_json(silent=True) or {}
    asset_number = (payload.get("asset") or "").strip()
    if not asset_number:
        raise LifeDataApiError("Select an Asset Number first.", status_code=400)
    grouping_level = (payload.get("grouping_level") or "").strip()
    failure_mode_id = payload.get("failure_mode_id")
    failure_mechanism_id = payload.get("failure_mechanism_id")
    if failure_mode_id is None:
        raise LifeDataApiError("Select a failure mode or failure mechanism first.", status_code=400)
    result = service.perform_weibull_analysis(
        asset_number,
        grouping_level=grouping_level,
        failure_mode_id=int(failure_mode_id),
        failure_mechanism_id=int(failure_mechanism_id) if failure_mechanism_id is not None else None,
    )
    return jsonify({"result": _serialize_analysis_result(result)})


@app.route("/life-data-analysis/api/saved-analysis")
@life_data_api
def api_saved_analysis():
    """Read back the Weibull fit already saved for a failure group.

    Performing an analysis rebuilds and stores event processing, observations, a
    dataset, a run, and a result, so that route stays editor-only. This is the
    read-only twin: a viewer or a signed-out visitor can open the Weibull view for
    any group an editor has already run, without writing anything. ``result`` comes
    back null when the group has never been analyzed.
    """
    service = _service_or_api_error()
    asset_number = _required_asset()
    grouping_level = (request.values.get("grouping_level") or "FAILURE_MECHANISM").strip().upper()
    if grouping_level not in {"FAILURE_MODE", "FAILURE_MECHANISM"}:
        raise LifeDataApiError("grouping_level must be FAILURE_MODE or FAILURE_MECHANISM.", status_code=400)
    mode_raw = request.values.get("failure_mode_id")
    if mode_raw in (None, ""):
        raise LifeDataApiError("Select a failure mode or mechanism to view its Weibull analysis.", status_code=400)
    try:
        failure_mode_id = int(mode_raw)
    except (TypeError, ValueError):
        raise LifeDataApiError("failure_mode_id must be an integer.", status_code=400)
    mechanism_raw = request.values.get("failure_mechanism_id")
    failure_mechanism_id = None
    if mechanism_raw not in (None, ""):
        try:
            failure_mechanism_id = int(mechanism_raw)
        except (TypeError, ValueError):
            raise LifeDataApiError("failure_mechanism_id must be an integer.", status_code=400)
    result = service.load_saved_weibull_analysis(
        asset_number,
        grouping_level=grouping_level,
        failure_mode_id=failure_mode_id,
        failure_mechanism_id=failure_mechanism_id,
    )
    return jsonify({"result": _serialize_analysis_result(result) if result is not None else None})


@app.route("/life-data-analysis/api/calculate-all", methods=["POST"])
@life_data_api
@requires_role("editor")
def api_calculate_all():
    service = _service_or_api_error()
    payload = request.get_json(silent=True) or {}
    asset_number = (payload.get("asset") or "").strip()
    if not asset_number:
        raise LifeDataApiError("Select an Asset Number first.", status_code=400)
    if str(payload.get("password") or "") != MLE_CALCULATION_PASSWORD:
        raise LifeDataApiError("The password was incorrect. No Weibull MLE calculations were performed.", status_code=403)
    return jsonify({"summary": service.calculate_all_weibull_results(asset_number)})


@app.route("/life-data-analysis/api/parameter-adjustment", methods=["POST"])
@life_data_api
@requires_role("editor")
def api_parameter_adjustment():
    service = _service_or_api_error()
    payload = request.get_json(silent=True) or {}
    try:
        result_id = int(payload.get("result_id"))
        beta = float(payload.get("beta"))
        eta = float(payload.get("eta"))
    except (TypeError, ValueError):
        raise LifeDataApiError("Adjusted beta, eta, and the source result id are required.", status_code=400)
    if not (math.isfinite(beta) and math.isfinite(eta) and beta > 0 and eta > 0):
        raise LifeDataApiError("Adjusted beta and eta must both be finite and greater than zero.", status_code=400)
    reason = str(payload.get("reason") or "")
    adjustment_id = service.save_parameter_adjustment(result_id, beta, eta, reason)
    return jsonify({"parameter_adjustment_id": adjustment_id})


@app.route("/life-data-analysis/api/weibull-report", methods=["POST"])
@life_data_api
@requires_role("editor")
def api_weibull_report():
    service = _service_or_api_error()
    payload = request.get_json(silent=True) or {}
    asset_number = (payload.get("asset") or "").strip()
    if not asset_number:
        raise LifeDataApiError("Select an Asset Number first.", status_code=400)
    result_id = payload.get("result_id")
    if result_id in (None, ""):
        result_id = (payload.get("result") or {}).get("result_id")
    if result_id in (None, ""):
        raise LifeDataApiError("Run a Weibull analysis before generating a report.", status_code=400)
    try:
        payload["result_id"] = int(result_id)
    except (TypeError, ValueError):
        raise LifeDataApiError("A valid saved Weibull result id is required.", status_code=400)
    # Build the .docx in a temp file, serve it from memory, then delete the temp
    # file immediately so repeated downloads never orphan files on disk.
    fd, path = tempfile.mkstemp(suffix=".docx", prefix="weibull_report_")
    os.close(fd)
    try:
        report_number = service.build_weibull_report_docx(asset_number, payload, path)
        with open(path, "rb") as handle:
            document_bytes = handle.read()
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
    return send_file(
        io.BytesIO(document_bytes),
        as_attachment=True,
        download_name=f"{report_number}.docx",
        mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )

@app.route("/reliability-links")
def reliability_links():
    return render_template(
        "reliability_links.html",
        page_title="Reliability Links",
        nav_links=NAV_LINKS,
    )
@app.route("/pm-calendar")
def pm_calendar():
    return render_template(
        "pm_calendar.html",
        page_title="PM Calendar",
        nav_links=NAV_LINKS,
    )
# ---------------------------------------------------------------------------
# Developer area
#
# An admin-gated workspace for developers and maintainers, split across three
# pages behind a hub at /developer:
#
#   /developer/database    - database inspection: runtime/app configuration, the
#                            health of GREMLIN.db as a file, how far data has
#                            travelled down the ingestion -> Weibull pipeline,
#                            the live schema, where that schema has drifted from
#                            what the schema code would build today, a row
#                            browser and a read-only SQL console.
#   /developer/limble-sync - run the nightly Limble import on demand.
#   /developer/access      - accounts and roles.
#
# Splitting them apart keeps the one page that writes (Limble sync) and the one
# that grants access (accounts) off the same screen as read-only inspection, so
# neither is a mis-click away from a panel someone opened to read.
#
# Every database read goes through SchemaService, which only ever opens
# `mode=ro` connections. That is deliberate: GREMLIN.db is expected to sit on a
# shared drive and serialises writers through BEGIN IMMEDIATE, so a dev query
# holding the writer slot would stall every other GREMLIN user.
#
# The one deliberate exception is the Limble sync panel, which runs the nightly
# ingestion job on demand and therefore does write. It is a background job with
# its own progress endpoint rather than an inline request; see
# services/sync_service.py.
# ---------------------------------------------------------------------------


def get_schema_service() -> SchemaService:
    """Return a shared read-only SchemaService for the configured database."""

    global _schema_service
    if _schema_service is None or _schema_service.db_path != _configured_db_path():
        _schema_service = SchemaService(_configured_db_path())
    return _schema_service


def _dev_unlocked() -> bool:
    user, error = _authorised_user("admin")
    return bool(user and not error)


def dev_api(view):
    """Gate a developer JSON endpoint behind an admin account and normalise errors."""

    @wraps(view)
    def wrapped(*args, **kwargs):
        if not _dev_unlocked():
            return jsonify({"error": "Developer dashboard access requires an administrator account."}), 403
        try:
            return view(*args, **kwargs)
        except SchemaServiceError as exc:
            return jsonify({"error": str(exc)}), 400
        except Exception as exc:  # noqa: BLE001 - last-resort guard for the API
            return jsonify({"error": f"Unexpected error: {exc}"}), 500

    return wrapped


def dev_page(view):
    """Turn away anyone but a signed-in administrator, on every developer page.

    The developer area is entered from the account dialog, which only offers the
    link to an administrator -- but a link is not a gate. Every page checks the
    role itself, so typing a URL, keeping an old bookmark or being demoted
    mid-session all end at the same explanation rather than at the page.
    """

    @wraps(view)
    def wrapped(*args, **kwargs):
        if not _dev_unlocked():
            return render_template("developer_lock.html", page_title="Developer", nav_links=NAV_LINKS), 403
        return view(*args, **kwargs)

    return wrapped


def _dev_page_context(section: str, **extra):
    """Shared template context: the sub-navigation needs to know where it is."""

    return {
        "page_title": "Developer",
        "nav_links": NAV_LINKS,
        "dev_section": section,
        "db_path": str(_configured_db_path()),
        **extra,
    }


@app.route("/developer")
@dev_page
def developer_home():
    return render_template(
        "developer_home.html",
        **_dev_page_context(
            "home",
            access_db_path=str(ACCESS_DB_PATH),
            bugs_db_path=str(BUGS_DB_PATH),
            # Rendering the hub establishes the session token the pages behind
            # it post with, so arriving here is enough to make their forms work.
            csrf_token=csrf_token(),
        ),
    )


@app.route("/developer/database")
@dev_page
def developer_database():
    return render_template("developer_database.html", **_dev_page_context("database"))


@app.route("/developer/limble-sync")
@dev_page
def developer_limble_sync():
    return render_template("developer_sync.html", **_dev_page_context("sync"))


@app.route("/developer/activity")
@dev_page
def developer_activity():
    return render_template(
        "developer_activity.html",
        **_dev_page_context(
            "activity",
            access_db_path=str(ACCESS_DB_PATH),
            default_days=ACTIVITY_DEFAULT_DAYS,
            max_days=ACTIVITY_MAX_DAYS,
        ),
    )


@app.route("/developer/bugs")
@dev_page
def developer_bugs():
    return render_template(
        "developer_bugs.html",
        **_dev_page_context(
            "bugs",
            bugs_db_path=str(BUGS_DB_PATH),
            severities=SEVERITIES,
            statuses=STATUSES,
        ),
    )


@app.route("/developer/access")
@dev_page
def developer_access():
    return render_template(
        "developer_access.html",
        **_dev_page_context(
            "access",
            access_db_path=str(ACCESS_DB_PATH),
            users=access_control.list_users(),
            roles=ROLES,
            csrf_token=csrf_token(),
        ),
    )


@app.post("/developer/access/users")
@requires_role("admin")
@requires_csrf
def developer_save_user():
    try:
        raw_id = request.form.get("user_id", "").strip()
        access_control.save_user(int(raw_id) if raw_id else None, request.form.get("username", ""), request.form.get("pin", ""), request.form.get("role", ""))
    except (ValueError, sqlite3.IntegrityError) as exc:
        return jsonify({"error": str(exc)}), 400
    return redirect(url_for("developer_access"))


@app.post("/developer/access/users/<int:user_id>/delete")
@requires_role("admin")
@requires_csrf
def developer_delete_user(user_id: int):
    try:
        access_control.delete_user(user_id, int(current_user()["id"]))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return redirect(url_for("developer_access"))


@app.route("/developer/lock", methods=["POST"])
@requires_csrf
def developer_lock():
    # There is no dashboard-only lock left to release: reaching this page is a
    # property of the account, so the only thing this button can do is end the
    # session. It says so, and it lands on Home rather than bouncing off the
    # 403 the developer page would now return.
    session.clear()
    return redirect(url_for("home"))


@app.route("/developer/api/runtime")
@dev_api
def api_dev_runtime():
    """App-level configuration: how this process is wired, independent of the DB."""

    try:
        from importlib.metadata import version as _package_version

        flask_version = _package_version("flask")
    except Exception:  # noqa: BLE001 - version reporting must never break the page
        flask_version = "unknown"

    service_status = "not initialised"
    service_error = _life_data_service_error
    if _life_data_service is not None:
        service_status = "ready"
    elif service_error:
        service_status = "failed"

    routes = sorted(
        (
            {
                "rule": str(rule),
                "endpoint": rule.endpoint,
                "methods": sorted(rule.methods - {"HEAD", "OPTIONS"}),
            }
            for rule in app.url_map.iter_rules()
            if rule.endpoint != "static"
        ),
        key=lambda entry: entry["rule"],
    )

    return jsonify(
        {
            "python_version": sys.version.split()[0],
            "flask_version": flask_version,
            "db_path": str(_configured_db_path()),
            "db_path_source": "GREMLIN_DB_PATH" if os.environ.get("GREMLIN_DB_PATH") else "default",
            "default_db_path": str(DEFAULT_DB_PATH),
            # Names where the key actually came from. The fallback is no longer
            # per-process: it is one key kept in the access database, shared by
            # every worker and held across restarts.
            "secret_key_source": (
                "GREMLIN_SECRET_KEY" if os.environ.get("GREMLIN_SECRET_KEY")
                else f"generated, stored in {ACCESS_DB_PATH.name}"
            ),
            "life_data_service_status": service_status,
            "life_data_service_error": service_error,
            "route_count": len(routes),
            "routes": routes,
        }
    )


@app.route("/developer/api/overview")
@dev_api
def api_dev_overview():
    return jsonify(get_schema_service().overview())


@app.route("/developer/api/pipeline")
@dev_api
def api_dev_pipeline():
    return jsonify(get_schema_service().pipeline())


@app.route("/developer/api/drift")
@dev_api
def api_dev_drift():
    return jsonify(get_schema_service().drift_report())


@app.route("/developer/api/tables")
@dev_api
def api_dev_tables():
    return jsonify({"tables": get_schema_service().tables()})


@app.route("/developer/api/tables/<table>")
@dev_api
def api_dev_table_detail(table):
    return jsonify(get_schema_service().table_detail(table))


@app.route("/developer/api/tables/<table>/rows")
@dev_api
def api_dev_table_rows(table):
    def _int_arg(name: str, default: int) -> int:
        raw = request.args.get(name)
        if raw in (None, ""):
            return default
        try:
            return int(raw)
        except (TypeError, ValueError):
            raise SchemaServiceError(f"{name} must be a whole number.")

    return jsonify(
        get_schema_service().table_rows(
            table,
            limit=_int_arg("limit", 50),
            offset=_int_arg("offset", 0),
        )
    )


@app.route("/developer/api/query", methods=["POST"])
@dev_api
def api_dev_query():
    payload = request.get_json(silent=True) or {}
    return jsonify(get_schema_service().run_query(payload.get("sql") or ""))


# --- User activity ---------------------------------------------------------
#
# The one developer endpoint that reads the access database rather than
# GREMLIN.db: who has been signing in, how often, and what they changed once
# inside. Both sources are already collected for other reasons -- login_events
# by the login endpoint, audit_log by every protected write -- so this reports
# what the deployment already knows instead of adding tracking of its own.


@app.route("/developer/api/activity")
@dev_api
def api_dev_activity():
    """Sign-in and change activity over a window of days."""

    raw = request.args.get("days")
    try:
        days = ACTIVITY_DEFAULT_DAYS if raw in (None, "") else int(raw)
        payload = access_control.activity_report(days)
    except ValueError:
        # Covers both a window that is not a number and one out of range; the
        # service owns the bound, so its message is the one worth showing.
        return jsonify({
            "error": f"days must be a whole number between 1 and {ACTIVITY_MAX_DAYS}."
        }), 400
    payload["access_db_path"] = str(ACCESS_DB_PATH)
    return jsonify(payload)


# --- Limble sync -----------------------------------------------------------
#
# The one place on this page that writes. Everything else here reads GREMLIN.db
# through SchemaService's read-only connections; this runs the same ingestion
# the nightly Task Scheduler job runs, so it fetches from the Limble API and
# writes what it finds. An administrator account limits this consequential write to named, auditable users.


@app.route("/developer/api/bugs")
@dev_api
def api_dev_bugs():
    """The dashboard's whole payload: the tiles' counts and the filtered list."""

    try:
        page = bug_reports.list_reports(
            status=request.args.get("status", "all"),
            search=request.args.get("search", ""),
            # Where the previous page stopped, rather than how many rows to skip
            # -- see list_reports on why a queue being worked cannot be paged by
            # counting.
            cursor=request.args.get("cursor") or None,
        )
        summary = bug_reports.summary()
    except BugReportValidationError as exc:
        return jsonify({"error": str(exc)}), 400
    except BugReportStoreError as exc:
        # 503, not 500: the code is fine and the share is not, and the page
        # tells the administrator which path it could not open.
        return jsonify({"error": str(exc)}), 503
    return jsonify(
        {
            "reports": page["reports"],
            # What the filter matches in total, against what this page holds --
            # the dashboard needs both to say "showing 200 of 640" and to know
            # whether to offer more.
            "total_matching": page["total"],
            "has_more": page["has_more"],
            "next_cursor": page["next_cursor"],
            "page_size": LIST_LIMIT,
            "summary": summary,
            "bugs_db_path": str(BUGS_DB_PATH),
            "severities": list(SEVERITIES),
            "statuses": list(STATUSES),
        }
    )


@app.post("/developer/api/bugs/<int:report_id>/status")
@dev_api
@requires_role("admin")
def api_dev_bug_status(report_id: int):
    """Resolve a report, or reopen one.

    requires_role sits under dev_api the way the sync endpoint's does: dev_api
    turns away anyone who is not an administrator, and requires_role records the
    change in the same audit trail every other protected write lands in, so who
    resolved what is answerable later.
    """

    # Same reasoning as the sync endpoint: a cross-site <form> can POST here
    # with the browser's cookies attached, but it cannot set this content type
    # without a preflight the browser refuses. That is what stands between a
    # drive-by page and reopening every report in an admin's live session.
    if not request.is_json:
        return jsonify({"error": "Status changes must be sent as JSON."}), 415
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"error": "The status change must be a JSON object."}), 400

    # Absent means the caller is deliberately not guarding -- a script, or the
    # tests. Present but unreadable is a bug in the caller, and is refused
    # rather than quietly treated as "no guard": silently dropping the check is
    # the one outcome that loses somebody's resolution.
    raw_revision = payload.get("expected_revision")
    expected_revision = None
    if raw_revision not in (None, ""):
        try:
            expected_revision = int(raw_revision)
        except (TypeError, ValueError):
            return jsonify({"error": "expected_revision must be a whole number."}), 400

    try:
        report = bug_reports.set_status(
            report_id,
            payload.get("status", ""),
            actor=(current_user() or {}).get("username", ""),
            note=payload.get("note", ""),
            # The revision the dashboard drew this row with. Sending it is what
            # stops a second administrator, whose page is out of date,
            # overwriting the first one's resolution note -- including when the
            # report has been resolved and reopened since, which a status alone
            # could not tell apart from no change at all.
            expected_revision=expected_revision,
        )
    except BugReportConflictError as exc:
        return jsonify(
            {"error": str(exc), "actual_status": exc.actual_status, "actual_revision": exc.actual}
        ), 409
    except BugReportValidationError as exc:
        return jsonify({"error": str(exc)}), 400
    except BugReportStoreError as exc:
        return jsonify({"error": str(exc)}), 503
    return jsonify({"report": report})


@app.post("/developer/api/bugs/<int:report_id>/delete")
@dev_api
@requires_role("admin")
def api_dev_bug_delete(report_id: int):
    """Drop a report entirely -- a duplicate, or something filed by accident."""

    if not request.is_json:
        return jsonify({"error": "Deletions must be sent as JSON."}), 415
    try:
        bug_reports.delete(report_id)
    except BugReportValidationError as exc:
        return jsonify({"error": str(exc)}), 400
    except BugReportStoreError as exc:
        return jsonify({"error": str(exc)}), 503
    return jsonify({"deleted": report_id})


@app.route("/developer/api/sync")
@dev_api
def api_dev_sync_status():
    """Progress of the current (or most recent) sync, plus how to judge it."""

    payload = sync_runner.describe(_configured_db_path())
    # Reported rather than enforced here: status stays readable when starting is
    # not allowed, so the page can explain why its button is disabled instead of
    # a click failing with no account of itself.
    payload["start_allowed"] = True
    payload["start_blocked_reason"] = None
    return jsonify(payload)


@app.route("/developer/api/sync", methods=["POST"])
@dev_api
@requires_role("admin")
def api_dev_sync_start():
    """Start a Limble sync in the background and return its initial status."""

    # Require a JSON body. A cross-site <form> can POST to this URL with the
    # browser's cookies attached but cannot set this content type without a
    # preflight the browser will refuse, so insisting on it keeps a drive-by
    # page from triggering a database write in an unlocked dev session.
    if not request.is_json:
        return jsonify({"error": "Sync requests must be sent as JSON."}), 415
    # A body that does not parse is a failed request, not an empty one. Treating
    # it as "no options given" would answer a truncated or malformed request by
    # running a full, writing sync -- the most consequential thing this endpoint
    # can do -- so require an object and say so otherwise. An explicit `{}` is
    # still a valid way to ask for the defaults.
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"error": "Sync options must be a JSON object."}), 400
    try:
        options = SyncOptions.from_payload(payload)
        return jsonify(sync_runner.start(_configured_db_path(), options))
    except SyncAlreadyRunningError as exc:
        return jsonify({"error": str(exc)}), 409
    except SyncOptionError as exc:
        return jsonify({"error": str(exc)}), 400


@app.errorhandler(404)
def not_found(_err):
    return render_template("home.html", page_title="Not Found", nav_links=NAV_LINKS), 404


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
