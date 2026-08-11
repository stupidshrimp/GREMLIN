import io
import math
import os
import secrets
import sys
import tempfile
from functools import wraps
from pathlib import Path

from flask import Flask, jsonify, redirect, render_template, request, send_file, session, url_for

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
    PM_DISPOSITION_CATEGORIES,
    PM_RESET_DECISIONS,
    WO_DISPOSITION_CATEGORIES,
    DatabaseWriteError,
    LifeDataService,
)
from services.schema_service import SchemaService, SchemaServiceError
from services.sync_service import (
    LimbleSyncRunner,
    SyncAlreadyRunningError,
    SyncOptionError,
    SyncOptions,
)

app = Flask(__name__)

# Needed for the developer dashboard's PIN session. A stable value keeps the
# unlock across restarts when one is configured; otherwise a per-process random
# key simply means developers re-enter the PIN after a restart.
app.secret_key = os.environ.get("GREMLIN_SECRET_KEY") or secrets.token_hex(32)

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
        # Analysis workspace instead of the "Choose a reliability workflow" landing
        # page. The landing page (/life-data-analysis) still exists and remains
        # reachable from the Home "Explore Analysis" button, so Failure
        # Classification and Standards and Documentation stay accessible.
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
    {"route": "/settings", "template": "settings.html", "title": "Settings", "icon": ICONS["settings"]},
    {
        "route": "/developer",
        "template": "developer_dashboard.html",
        "title": "Developer",
        "icon": ICONS["code"],
    },
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

# Developer dashboard access. The PIN is a shared speed bump for an app that has
# no user accounts: it keeps dev tooling out of the way of normal users on the
# plant network. It is deliberately NOT authentication, and the default is a
# known value in a public repository -- so treat anyone who can reach this port
# as able to read the whole database through the dashboard. That trade-off was
# made knowingly in favour of the page working with no configuration; if it ever
# needs to hold against the network, set GREMLIN_DEV_PIN rather than assuming
# this default protects anything.
DEFAULT_DEV_PIN = "1336"
_DEV_PIN_FROM_ENV = os.environ.get("GREMLIN_DEV_PIN")
DEV_DASHBOARD_PIN = _DEV_PIN_FROM_ENV or DEFAULT_DEV_PIN


def _pin_is_configured(pin_from_env: str | None) -> bool:
    """Whether the PIN in force is a secret, rather than the published fallback.

    Exporting GREMLIN_DEV_PIN=1336 is not configuring a PIN. The value is in this
    repository, so a deployment that sets it is protected by nothing at all --
    and a check that only asked whether the variable *existed* would hand it
    authority to write to the database on that basis. What matters is that the
    PIN is not the one everybody can read.
    """

    return bool(pin_from_env) and pin_from_env != DEFAULT_DEV_PIN


# Read once, from the same value the PIN itself comes from, so the two can never
# disagree: a GREMLIN_DEV_PIN exported after startup does not change the PIN
# being checked, and must not change this either.
DEV_PIN_IS_CONFIGURED = _pin_is_configured(_DEV_PIN_FROM_ENV)
DEV_SESSION_KEY = "dev_dashboard_unlocked"

# Reading the database through this page needs no configuration; starting a sync
# does. The difference is what the action can do: it writes to a GREMLIN.db other
# people are using and spends this server's Limble credentials, and the default
# PIN is published, so that authority has to be granted deliberately rather than
# inherited from a fallback. Inspection panels are unaffected.
SYNC_LOCKED_MESSAGE = (
    "Starting a sync is disabled because the developer PIN is still the published default. "
    "That is acceptable for the read-only panels, but not for an action that writes to "
    "GREMLIN.db and calls the Limble API with this server's credentials. Set GREMLIN_DEV_PIN "
    "to a value of your own on the GREMLIN host and restart it to enable this. The scheduled "
    "nightly sync is unaffected."
)
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


# Pages that exist and are routable but are deliberately kept out of the
# sidebar. Standards and Documentation is reached from the Life Data Analysis
# landing page instead; the developer dashboard is unlisted on purpose, so it is
# only reachable by typing its URL (and then entering the PIN).
UNLISTED_ROUTES = {"/standards-and-documentation", "/developer"}

NAV_LINKS = [
    {"label": page["title"], "url": page["route"], "icon": page["icon"]}
    for page in PAGES
    if page["route"] not in UNLISTED_ROUTES
]


@app.route("/")
def home():
    return render_template("home.html", page_title="Home", nav_links=NAV_LINKS)


@app.route("/life-data-analysis")
def life_data_analysis():
    return render_template(
        "life_data_analysis.html",
        page_title="Life Data Analysis",
        nav_links=NAV_LINKS,
    )




@app.route("/life-data-analysis/perform-analysis")
def perform_analysis():
    return render_template(
        "perform_analysis.html",
        page_title="Perform an Analysis",
        nav_links=NAV_LINKS,
    )


@app.route("/life-data-analysis/disposition")
def disposition():
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
def api_availability_reset_group(asset_group: str):
    repository = get_availability_repository()
    repository.reset_group_to_defaults(asset_group)
    return jsonify(build_config(repository))


@app.route("/metrics/api/availability/goal", methods=["PUT"])
@availability_api
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


@app.route("/settings")
def settings():
    return render_template("settings.html", page_title="Settings", nav_links=NAV_LINKS)


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
    service = _service_or_api_error()
    # Auto-map only happens when the mapped table is empty, so an already-populated
    # database does not silently pick up later Limble syncs. Allow the client to
    # request an explicit re-map (mirroring the desktop "Refresh CMMS mapping"
    # action) before reading the asset list.
    if request.values.get("refresh") in ("1", "true", "yes"):
        service.refresh_mapped_cmms_records()
    return jsonify({"assets": service.asset_number_options()})


@app.route("/life-data-analysis/api/refresh-mapping", methods=["POST"])
@life_data_api
def api_refresh_mapping():
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


@app.route("/life-data-analysis/api/calculate-all", methods=["POST"])
@life_data_api
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


# ---------------------------------------------------------------------------
# Developer dashboard
#
# A PIN-gated workspace for developers and maintainers. The database explorer is
# one panel among several: the dashboard also reports app/runtime configuration,
# the health of GREMLIN.db as a file, how far data has travelled down the
# ingestion -> Weibull pipeline, and where the live file's schema has drifted
# from what the schema code would build today.
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
    return bool(session.get(DEV_SESSION_KEY))


def dev_api(view):
    """Gate a dev JSON endpoint behind the PIN and normalise its errors."""

    @wraps(view)
    def wrapped(*args, **kwargs):
        if not _dev_unlocked():
            return jsonify({"error": "Developer dashboard is locked. Enter the PIN to continue."}), 403
        try:
            return view(*args, **kwargs)
        except SchemaServiceError as exc:
            return jsonify({"error": str(exc)}), 400
        except Exception as exc:  # noqa: BLE001 - last-resort guard for the API
            return jsonify({"error": f"Unexpected error: {exc}"}), 500

    return wrapped


@app.route("/developer")
def developer_dashboard():
    if not _dev_unlocked():
        return render_template("developer_lock.html", page_title="Developer", nav_links=NAV_LINKS)
    return render_template(
        "developer_dashboard.html",
        page_title="Developer",
        nav_links=NAV_LINKS,
        db_path=str(_configured_db_path()),
    )


@app.route("/developer/unlock", methods=["POST"])
def developer_unlock():
    submitted = (request.form.get("pin") or "").strip()
    # compare_digest keeps the check constant-time. Compare as UTF-8 bytes: on
    # str arguments it rejects any non-ASCII character with a TypeError, so a
    # stray accented character typed into the PIN box would surface as a 500
    # with a traceback instead of an ordinary "Incorrect PIN".
    if secrets.compare_digest(submitted.encode("utf-8"), DEV_DASHBOARD_PIN.encode("utf-8")):
        session[DEV_SESSION_KEY] = True
        return redirect(url_for("developer_dashboard"))
    return (
        render_template(
            "developer_lock.html",
            page_title="Developer",
            nav_links=NAV_LINKS,
            error="Incorrect PIN.",
        ),
        403,
    )


@app.route("/developer/lock", methods=["POST"])
def developer_lock():
    session.pop(DEV_SESSION_KEY, None)
    return redirect(url_for("developer_dashboard"))


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
            "secret_key_source": "GREMLIN_SECRET_KEY" if os.environ.get("GREMLIN_SECRET_KEY") else "per-process random",
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


# --- Limble sync -----------------------------------------------------------
#
# The one place on this page that writes. Everything else here reads GREMLIN.db
# through SchemaService's read-only connections; this runs the same ingestion
# the nightly Task Scheduler job runs, so it fetches from the Limble API and
# writes what it finds. The PIN is what limits that to certain users -- it is the
# only notion of one this app has -- and because it authorises a write, it has to
# be a PIN someone chose: see DEV_PIN_IS_CONFIGURED.


@app.route("/developer/api/sync")
@dev_api
def api_dev_sync_status():
    """Progress of the current (or most recent) sync, plus how to judge it."""

    payload = sync_runner.describe(_configured_db_path())
    # Reported rather than enforced here: status stays readable when starting is
    # not allowed, so the page can explain why its button is disabled instead of
    # a click failing with no account of itself.
    payload["start_allowed"] = DEV_PIN_IS_CONFIGURED
    payload["start_blocked_reason"] = None if DEV_PIN_IS_CONFIGURED else SYNC_LOCKED_MESSAGE
    return jsonify(payload)


@app.route("/developer/api/sync", methods=["POST"])
@dev_api
def api_dev_sync_start():
    """Start a Limble sync in the background and return its initial status."""

    # Fail closed on the default PIN. This is the check the page's disabled
    # button reflects, but the button is not the guard -- the endpoint is.
    if not DEV_PIN_IS_CONFIGURED:
        return jsonify({"error": SYNC_LOCKED_MESSAGE}), 403

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
