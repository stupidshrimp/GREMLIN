"""User authentication stored independently from the reliability database."""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
import sqlite3
import time
from datetime import date, timedelta
from pathlib import Path

from werkzeug.security import check_password_hash, generate_password_hash

ROLES = ("viewer", "editor", "admin")

# Departments and staff levels sit alongside the role above, and are a different
# kind of fact about an account. `role` says what the account may do to stored
# data -- read it, change it, administer it -- and every protected write is
# gated on it. These two say where in the plant the person sits, and gate
# nothing at all: which pages and sidebar entries each department or level
# should be shown has not been decided yet, so this module records the answer
# and enforces none of it. When those rules do arrive they belong wherever the
# navigation is assembled, not here -- nothing below should grow a notion of
# which department outranks which, because none of them does.
#
# Both sets carry an explicit "all" member meaning "not narrowed to anything",
# and that is what every account gets by default. Accounts created before these
# columns existed are migrated onto it, so adding them cannot take a page away
# from somebody who can reach it today; the first deployment to write a
# filtering rule decides who stops being "all", not this migration.
DEPARTMENT_ALL = "all"
# One value rather than two rows against the account: somebody who covers both
# shifts is common enough here to be worth a word of its own, and a
# single-valued column is what the dropdown on the developer page edits.
DEPARTMENT_OPERATIONS_MAINTENANCE = "operations_maintenance"
DEPARTMENTS = (
    "facilities",
    "operations",
    "maintenance",
    DEPARTMENT_OPERATIONS_MAINTENANCE,
    DEPARTMENT_ALL,
)
DEFAULT_DEPARTMENT = DEPARTMENT_ALL
# Which single departments a combined value stands for. Only combined values
# belong here; anything absent stands for itself, and DEPARTMENT_ALL stands for
# every single one. Kept as data so that adding a department -- or a second
# combination -- is an edit to these two structures rather than a hunt through
# string comparisons; department_scope() below is the only reader.
DEPARTMENT_COMPOSITES = {DEPARTMENT_OPERATIONS_MAINTENANCE: ("operations", "maintenance")}
DEPARTMENT_SINGLES = tuple(
    name for name in DEPARTMENTS if name != DEPARTMENT_ALL and name not in DEPARTMENT_COMPOSITES
)
DEPARTMENT_LABELS = {
    "facilities": "Facilities",
    "operations": "Operations",
    "maintenance": "Maintenance",
    "operations_maintenance": "Operations & Maintenance",
    DEPARTMENT_ALL: "All departments",
}

STAFF_LEVEL_ALL = "all"
# Spelled `staff_level` everywhere rather than plain "level": app.py already
# ranks the three permission roles as ROLE_LEVEL, and two unrelated things
# called "level" in one request get read as each other sooner or later.
#
# The order here is the order the dropdown draws, and nothing more. These are
# three kinds of job, not a ladder: nothing in GREMLIN compares two staff
# levels, and the tuple is deliberately not written so that it could.
STAFF_LEVELS = ("engineer", "leadership", "associate", STAFF_LEVEL_ALL)
DEFAULT_STAFF_LEVEL = STAFF_LEVEL_ALL
STAFF_LEVEL_LABELS = {
    "engineer": "Engineer",
    "leadership": "Leadership",
    "associate": "Associate",
    STAFF_LEVEL_ALL: "All levels",
}

# Every read of an account is projected onto this list -- the session snapshot,
# the login response, the developer roster -- so a column added here reaches all
# of them at once instead of being remembered in three places.
ACCOUNT_FIELDS = ("id", "username", "role", "department", "staff_level", "credential_version")
_ACCOUNT_COLUMNS = ",".join(ACCOUNT_FIELDS)

LOGIN_FAILURE_LIMIT = 5
LOGIN_WINDOW_SECONDS = 5 * 60
LOGIN_LOCK_SECONDS = 15 * 60
DB_BUSY_TIMEOUT_SECONDS = 30
MAX_USERNAME_CHARS = 128
MAX_PIN_CHARS = 256
# How many *unlocked* limiter rows to keep. Hashing bounds each key's size but
# not how many arrive: a distributed caller submitting a fresh username per
# request adds an account scope every time, and rows survive until their window
# expires. Since sqlite does not return freed pages to the filesystem, an
# unbounded burst would permanently enlarge accesscontrol.db. Rows that are
# currently locked out are never evicted -- those are the ones doing the work,
# and dropping them would let a flood clear somebody's lockout.
LOGIN_ATTEMPT_ROW_CAP = 10_000

# Sign-in history, which is what the developer area's user-activity page counts.
# Distinct from login_attempts above: that table is the rate limiter's working
# state and is deleted the moment a login succeeds, so it can say who is locked
# out right now but nothing about who has been using GREMLIN.
LOGIN_SUCCESS = "success"
LOGIN_FAILURE = "failure"
LOGIN_BLOCKED = "blocked"
LOGIN_OUTCOMES = (LOGIN_SUCCESS, LOGIN_FAILURE, LOGIN_BLOCKED)
# Same reasoning as LOGIN_ATTEMPT_ROW_CAP: every attempt writes a row, including
# ones nobody legitimate made, so the history is capped rather than allowed to
# enlarge the file forever. Sized for years of ordinary use by a plant-sized
# roster -- a few dozen accounts signing in a handful of times a day.
LOGIN_EVENT_ROW_CAP = 50_000
# How far over the cap the history is allowed to run before it is trimmed back
# to it. Deciding what to keep means ordering the whole table by a priority no
# index can supply, which is a full scan and a temporary sort -- tens of
# milliseconds at the cap, held inside the login path's serialized writer slot.
# Paying that on every attempt would let anyone who first fills the cap put
# every subsequent sign-in, their own or somebody else's, behind another sort.
# Trimming in batches pays it once per this many attempts instead, so the file
# holds at most cap + this.
LOGIN_EVENT_PRUNE_SLACK = 500
# What the activity page may ask for. A window is a whole number of days back
# from now; the cap keeps a hand-edited URL from asking for a decade of history.
ACTIVITY_DEFAULT_DAYS = 30
ACTIVITY_MAX_DAYS = 365
ACTIVITY_RECENT_LIMIT = 25

logger = logging.getLogger(__name__)


def _account_scope(username: str) -> str:
    """The limiter key for one account name, however it was typed."""

    normalized = username.strip().casefold()
    return "account:" + hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _check_credential_bounds(username: str, pin: str) -> None:
    """Refuse credentials the login path would decline to look up.

    ``authenticate_limited`` bounds what it will search for, so that an
    attacker cannot make the limiter table grow by inventing enormous
    usernames. Anything stored beyond those bounds is therefore an account that
    cannot sign in, whichever door created it -- so every door checks here.
    """

    if len(username) > MAX_USERNAME_CHARS:
        raise ValueError(f"Username must be {MAX_USERNAME_CHARS} characters or fewer.")
    if len(pin) > MAX_PIN_CHARS:
        raise ValueError(f"PIN must be {MAX_PIN_CHARS} characters or fewer.")


def department_scope(department: str) -> tuple[str, ...]:
    """The single-department values a stored department stands for.

    Only unpacks the value; it decides nothing. "operations_maintenance" is one
    string in the column but names two parts of the plant, and "all" names every
    part, so anything that eventually filters by department needs a set rather
    than the string it was given. Returning it here means that filtering, when
    it is written, does not re-derive what the combined values mean each time it
    is asked. An unrecognised value stands only for itself.
    """

    if department == DEPARTMENT_ALL:
        return DEPARTMENT_SINGLES
    return DEPARTMENT_COMPOSITES.get(department, (department,))


def department_label(department: str) -> str:
    """How a stored department is written on screen."""

    return DEPARTMENT_LABELS.get(department, department)


def staff_level_label(staff_level: str) -> str:
    """How a stored staff level is written on screen."""

    return STAFF_LEVEL_LABELS.get(staff_level, staff_level)


class AccessControl:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._origin_key_cache: bytes | None = None

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=DB_BUSY_TIMEOUT_SECONDS)
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout = {DB_BUSY_TIMEOUT_SECONDS * 1000}")
        return conn

    def ensure_schema(self, *, initial_username: str | None = None, initial_pin: str | None = None) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            # Connecting is what creates the file, so narrow it here -- before
            # anything below can raise. A refused bootstrap still leaves the
            # file on disk, and every later start would find it already there.
            self._restrict_permissions()
            # Startup can occur concurrently in a multi-worker deployment.
            # Claim the writer slot before schema migration and the empty-table
            # bootstrap decision so only one worker can create the first admin;
            # later workers observe its committed row rather than racing into a
            # uniqueness error during module import.
            conn.execute("BEGIN IMMEDIATE")
            # `department` and `staff_level` are written from this module's own
            # tuples rather than spelled out, so the column and the code that
            # validates it cannot drift apart. The permitted values live in the
            # schema, which is the point: the accounts file states what a
            # department may be, and a hand-edited row that says otherwise is
            # refused by sqlite rather than discovered later by a page.
            conn.execute(f"""
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT NOT NULL COLLATE NOCASE UNIQUE,
                    password_hash TEXT NOT NULL,
                    role TEXT NOT NULL CHECK(role IN ('viewer','editor','admin')),
                    department TEXT NOT NULL DEFAULT '{DEFAULT_DEPARTMENT}'
                        CHECK(department IN {DEPARTMENTS!r}),
                    staff_level TEXT NOT NULL DEFAULT '{DEFAULT_STAFF_LEVEL}'
                        CHECK(staff_level IN {STAFF_LEVELS!r}),
                    credential_version INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
            """)
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(users)")}
            if "credential_version" not in columns:
                conn.execute("ALTER TABLE users ADD COLUMN credential_version INTEGER NOT NULL DEFAULT 1")
            # Added rather than rebuilt, so nobody's PIN is disturbed by a
            # migration. Every account that predates these columns lands on the
            # unrestricted default: the roster does not know where anyone works
            # yet, and guessing would quietly narrow what a real person can
            # reach. An administrator sets them from the developer page.
            if "department" not in columns:
                conn.execute(
                    f"""ALTER TABLE users ADD COLUMN department TEXT NOT NULL
                        DEFAULT '{DEFAULT_DEPARTMENT}' CHECK(department IN {DEPARTMENTS!r})"""
                )
            if "staff_level" not in columns:
                conn.execute(
                    f"""ALTER TABLE users ADD COLUMN staff_level TEXT NOT NULL
                        DEFAULT '{DEFAULT_STAFF_LEVEL}' CHECK(staff_level IN {STAFF_LEVELS!r})"""
                )
            conn.execute("""
                CREATE TABLE IF NOT EXISTS audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    username TEXT NOT NULL,
                    action TEXT NOT NULL,
                    changed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS login_attempts (
                    scope_key TEXT PRIMARY KEY,
                    failure_count INTEGER NOT NULL,
                    window_started REAL NOT NULL,
                    locked_until REAL NOT NULL DEFAULT 0
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS deployment_secret (
                    id INTEGER PRIMARY KEY CHECK(id = 1),
                    secret TEXT NOT NULL
                )
            """)
            # One row per sign-in attempt that reached the login endpoint.
            #
            # `username` holds the account's own name and is filled in only when
            # the typed name matched a real account. A name that matched nothing
            # is stored as NULL rather than as typed: the commonest way to
            # produce one is a PIN entered in the username box, and this file
            # otherwise keeps no credential in the clear. What is lost is the
            # ability to read back which invented names were tried; `outcome`
            # and `client_digest` still count the attempts and group them by
            # origin, which is what the activity page reports.
            #
            # `user_id` is not a foreign key. History outlives the account it
            # belongs to -- removing a user must not silently rewrite what the
            # deployment recorded about the period they were signing in.
            # Built from the constants rather than repeating them, so the column
            # and the code writing it cannot drift apart. Quoted here rather than
            # bound as parameters: a CHECK constraint is part of the table
            # definition, and these values are this module's own literals.
            outcomes = ",".join(f"'{outcome}'" for outcome in LOGIN_OUTCOMES)
            conn.execute(f"""
                CREATE TABLE IF NOT EXISTS login_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    username TEXT,
                    outcome TEXT NOT NULL CHECK(outcome IN ({outcomes})),
                    client_digest TEXT NOT NULL,
                    occurred_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
            """)
            # Every activity query is "since a cutoff", and the per-user panel
            # then groups by account.
            # Whether the history has ever actually been trimmed, which is the
            # only thing that makes it incomplete. Row count cannot answer it:
            # trimming runs at cap + slack and takes the table back to exactly
            # the cap, so "at the cap" is true both of a file that has never
            # dropped anything and of one that has just finished dropping.
            conn.execute("""
                CREATE TABLE IF NOT EXISTS login_event_pruning (
                    id INTEGER PRIMARY KEY CHECK(id = 1),
                    last_pruned_at TEXT NOT NULL,
                    rows_dropped INTEGER NOT NULL DEFAULT 0,
                    dropped_success INTEGER NOT NULL DEFAULT 0,
                    dropped_attributed INTEGER NOT NULL DEFAULT 0,
                    dropped_unattributed INTEGER NOT NULL DEFAULT 0,
                    -- The first instant each tier is intact from: a second
                    -- past the newest attempt the trim removed, since that
                    -- second is one rows were deleted from. What survives
                    -- cannot answer this on its own: eviction is
                    -- newest-first *within* a tier, so the oldest surviving row
                    -- normally sits just after the newest dropped one -- but
                    -- put the clock back and a later attempt arrives dated
                    -- earlier than rows already dropped, and the oldest
                    -- surviving row moves backwards past the trim line. The
                    -- panel would then date the history from it and claim a
                    -- stretch is complete that was trimmed. Recorded while the
                    -- rows are still here, because afterwards it is unknowable.
                    cutoff_success TEXT,
                    cutoff_attributed TEXT,
                    cutoff_unattributed TEXT
                )
            """)
            # Split by tier after the fact, for a database created before those
            # columns existed. Without them a tier trimmed away to nothing is
            # indistinguishable from one that never had a row, and the page
            # would report "no attempts on unrecognised names" about a history
            # that dropped thousands of them.
            pruning_columns = {row["name"] for row in conn.execute("PRAGMA table_info(login_event_pruning)")}
            added = False
            for column in ("dropped_success", "dropped_attributed", "dropped_unattributed", "dropped_unclassified"):
                if column not in pruning_columns:
                    conn.execute(f"ALTER TABLE login_event_pruning ADD COLUMN {column} INTEGER NOT NULL DEFAULT 0")
                    added = True
            # Nullable text, and no backfill: a database that trimmed rows
            # before this column existed cannot say when they were dated, and
            # NULL is how that is said. Until the next prune writes one, such a
            # tier is described by its surviving rows exactly as before.
            for column in ("cutoff_success", "cutoff_attributed", "cutoff_unattributed"):
                if column not in pruning_columns:
                    conn.execute(f"ALTER TABLE login_event_pruning ADD COLUMN {column} TEXT")
            # Rows dropped before the per-tier columns existed cannot be
            # classified after the fact -- the rows are gone. Defaulting their
            # tiers to zero would state as fact that none of them were sign-ins,
            # so they are carried as unclassified instead and the page says so.
            if added and pruning_columns:
                conn.execute(
                    """UPDATE login_event_pruning
                          SET dropped_unclassified = rows_dropped
                                - dropped_success - dropped_attributed - dropped_unattributed
                        WHERE id = 1"""
                )
            conn.execute("CREATE INDEX IF NOT EXISTS login_events_occurred_at ON login_events(occurred_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS login_events_user ON login_events(user_id, occurred_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS audit_log_changed_at ON audit_log(changed_at)")
            # GREMLIN_ADMIN_* creates the first administrator and nothing else,
            # so it is read only when there is a first administrator to create.
            # Every complaint about it belongs inside this branch: on a
            # deployment that is already bootstrapped these values are inert,
            # and refusing to start over an inert setting -- a stale one left in
            # .env, a typo nobody has needed since -- would take a working plant
            # offline to report something that changes nothing.
            if conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0:
                if bool(initial_username) != bool(initial_pin):
                    raise RuntimeError("GREMLIN_ADMIN_USERNAME and GREMLIN_ADMIN_PIN must be configured together.")
                if initial_username and initial_pin:
                    # The account being created has to survive the checks
                    # save_user applies and the bounds the login path enforces.
                    # One that is blank once stripped, or longer than
                    # authenticate_limited will look up, can never sign in --
                    # and has_users() would then report the table as populated,
                    # so no later start would replace it with corrected
                    # credentials. Refusing costs a restart; accepting costs the
                    # deployment every protected write, permanently.
                    bootstrap_username = initial_username.strip()
                    if not bootstrap_username:
                        raise ValueError("GREMLIN_ADMIN_USERNAME must not be blank.")
                    _check_credential_bounds(bootstrap_username, initial_pin)
                    conn.execute(
                        "INSERT INTO users(username,password_hash,role) VALUES (?,?, 'admin')",
                        (bootstrap_username, generate_password_hash(initial_pin)),
                    )
    def _restrict_permissions(self) -> None:
        """Keep the file readable only by the account GREMLIN runs as.

        This database holds the password hashes and the session signing key, so
        read access to it is enough to forge an administrator without knowing a
        PIN. The default location is beside GREMLIN.db, which on a plant share
        is a directory other people can list -- and a default umask would have
        created this file world-readable there.

        Applied on every start rather than only when this call creates the
        file. An earlier version did the latter, to avoid overruling an
        operator who had widened it deliberately, and that was the wrong trade:
        sqlite creates the file on connect, so a bootstrap that was refused
        left a world-readable file behind that no later start would ever narrow
        again. Widening this file is not a thing to protect anyway -- nothing
        legitimate needs to read another account's session-forging material.

        Checked afterwards rather than assumed, and a failure is reported at
        WARNING with the remedy in it. It is not fatal: refusing to start on
        mode bits would take down every Windows deployment, which is the
        platform GREMLIN actually runs on -- there os.chmod only toggles the
        read-only attribute, never raises for an ACL it cannot express, and
        st_mode is synthesised back from that attribute, so an ordinary
        writable file reports 0o666 and would look world-readable to any such
        check. The verification is therefore limited to POSIX, where the bits
        mean what they say. On Windows the directory ACL governs, and a
        deployment sharing that directory wants GREMLIN_ACCESS_DB_PATH pointed
        somewhere only the service account can read.
        """

        try:
            mode = self.path.stat().st_mode & 0o777
            if mode & 0o077:
                os.chmod(self.path, mode & 0o700)
        except OSError as exc:
            logger.warning("Could not restrict permissions on %s: %s", self.path, exc)
        if os.name != "posix":
            return
        try:
            if self.path.stat().st_mode & 0o077:
                logger.warning(
                    "%s is readable by other accounts on this machine. It holds the "
                    "password hashes and the session signing key, so any of them can "
                    "forge an administrator. Restrict it to the account GREMLIN runs "
                    "as, or set GREMLIN_ACCESS_DB_PATH to a directory only that "
                    "account can read.",
                    self.path,
                )
        except OSError:
            pass

    def shared_secret_key(self) -> str:
        """The session signing key every worker of this deployment agrees on.

        A key generated per process is fine while GREMLIN is one process, and
        wrong the moment it is more than one: each worker would reject the
        cookies the others signed, so a signed-in user's writes would fail
        whenever the next request landed on a different worker. Keeping the key
        beside the accounts it protects gives the deployment a single key with
        no configuration, and it survives a restart instead of signing everyone
        out. GREMLIN_SECRET_KEY still overrides it where the operator would
        rather hold the key themselves.

        The secret is stored in the clear, in the same file as the password
        hashes -- the file permissions on accesscontrol.db are what protect
        both.
        """

        with self._connect() as conn:
            # Two workers starting together must not each generate a key and
            # then disagree about which one was stored.
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT secret FROM deployment_secret WHERE id = 1").fetchone()
            if row:
                return row["secret"]
            secret = secrets.token_hex(32)
            conn.execute("INSERT INTO deployment_secret(id, secret) VALUES (1, ?)", (secret,))
        return secret

    def _origin_key(self) -> bytes:
        """The deployment's own key for digesting where an attempt came from.

        Derived from the deployment secret rather than being it: that value
        signs session cookies, and a key should do one job. Read once per
        process -- the secret is settled at startup and never rotates while
        GREMLIN is running.
        """

        if self._origin_key_cache is None:
            self._origin_key_cache = hashlib.sha256(
                b"gremlin-login-origin:" + self.shared_secret_key().encode("utf-8")
            ).digest()
        return self._origin_key_cache

    def _origin_digest(self, client_key: str) -> str:
        """A stable, deployment-scoped stand-in for a client address.

        The activity page counts *distinct* origins; it never needs to say where
        anybody was sitting, so the address itself is not kept. A plain SHA-256
        would not have achieved that: IPv4 is four billion values and a plant
        subnet is tens of thousands, so anybody holding the digest could simply
        hash every candidate address until one matched. Keyed, the stored value
        means nothing without this deployment's secret -- which is what these
        rows need, because unlike the rate limiter's working state they are
        years of history that gets exported, screenshotted and pasted into
        tickets.

        Not a defence against somebody holding the access database itself. The
        key is in it, beside the password hashes; nothing in this file is.
        """

        return hmac.new(self._origin_key(), str(client_key).encode("utf-8"), hashlib.sha256).hexdigest()

    def get_user(self, user_id: int) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(f"SELECT {_ACCOUNT_COLUMNS} FROM users WHERE id=?", (user_id,)).fetchone()
        return dict(row) if row else None

    def has_users(self) -> bool:
        with self._connect() as conn:
            return bool(conn.execute("SELECT 1 FROM users LIMIT 1").fetchone())

    def authenticate(self, username: str, pin: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(f"SELECT password_hash,{_ACCOUNT_COLUMNS} FROM users WHERE username=?", (username,)).fetchone()
        if row and check_password_hash(row["password_hash"], pin):
            return {key: row[key] for key in ACCOUNT_FIELDS}
        return None

    def authenticate_limited(self, username: str, pin: str, client_key: str) -> tuple[dict | None, int]:
        """Authenticate with persistent per-account and per-client lockouts."""
        now = time.time()
        # Scope keys have a fixed storage cost even for attacker-controlled,
        # oversized usernames/client identifiers. The real username remains a
        # parameterized users-table lookup only when it satisfies the account
        # input bound.
        #
        # Two digests of the same caller, for two jobs that want different
        # things.
        #
        # The limiter's key is the plain digest it has always been. It only has
        # to be stable and the same on every worker: these rows live for minutes,
        # are never displayed and never leave the file, so there is nothing here
        # for a keyed digest to protect. Changing it would have a cost, though --
        # every existing lockout is filed under the old key, so a client already
        # locked out for spraying guesses would be handed a fresh budget by the
        # upgrade, and during a rolling deployment its attempts would split
        # across two keys and count twice over.
        #
        # The history's is keyed, because those rows are kept for years and get
        # exported; see _origin_digest. Computed before the transaction below is
        # opened: the first call reads the deployment secret on a connection of
        # its own, which cannot be done while this one holds the writer slot.
        limiter_digest = hashlib.sha256(str(client_key).encode("utf-8")).hexdigest()
        origin_digest = self._origin_digest(client_key)
        scopes = (_account_scope(username), f"client:{limiter_digest}")
        with self._connect() as conn:
            # Serialize the complete read/check/increment sequence. A deferred
            # transaction would let parallel requests all read the same count
            # and then overwrite one another with the same next value, making
            # a batch of guesses count as one. BEGIN IMMEDIATE takes SQLite's
            # writer slot before the count is read, so every request observes
            # the preceding request's committed increment.
            conn.execute("BEGIN IMMEDIATE")
            # Unknown usernames and rotating client addresses must not grow the
            # limiter table forever. Rows no longer participating in a failure
            # window or lockout are disposable; sweep them while already
            # holding the serialized writer transaction.
            conn.execute(
                "DELETE FROM login_attempts WHERE window_started < ? AND locked_until <= ?",
                (now - LOGIN_WINDOW_SECONDS, now),
            )
            # The sweep above only reaches rows whose window has already run
            # out, so a fast enough burst outruns it inside the window. Cap
            # what is left.
            #
            # Locked rows are capped too, which they were not at first. The
            # reason they can be is that a lockout guarding a *real* account is
            # distinguishable from one guarding a name an attacker invented:
            # the scope key is a digest of the username, and this deployment
            # has tens of accounts, so their digests can simply be listed and
            # held back. Everything else -- lockouts on names that match no
            # account, and per-address lockouts -- is evictable, so no flood
            # can clear the protection on somebody's real account, and the
            # table is still bounded by cap + accounts + this request's two.
            #
            # Only when there is something to cap: this is the one place that
            # reads the users table on a login, and the ordinary case is a
            # table far below the cap.
            if conn.execute("SELECT COUNT(*) FROM login_attempts").fetchone()[0] > LOGIN_ATTEMPT_ROW_CAP:
                protected = [
                    _account_scope(row["username"])
                    for row in conn.execute("SELECT username FROM users")
                ]
                placeholders = ",".join("?" * len(protected)) or "NULL"
                conn.execute(
                    f"""DELETE FROM login_attempts WHERE scope_key IN (
                            SELECT scope_key FROM login_attempts
                             WHERE scope_key NOT IN ({placeholders})
                             ORDER BY window_started DESC
                             LIMIT -1 OFFSET ?
                        )""",
                    (*protected, LOGIN_ATTEMPT_ROW_CAP),
                )
            attempts = {row["scope_key"]: row for row in conn.execute(
                "SELECT * FROM login_attempts WHERE scope_key IN (?,?)", scopes
            )}
            locked_until = max((float(row["locked_until"]) for row in attempts.values()), default=0)
            # Two bounds doing two different jobs, so they are asked separately.
            # The username bound decides whether the account is looked up at all
            # -- it is what keeps an enormous name from being searched for, and
            # therefore also what decides whether this attempt can be attributed
            # to an account in the history. The PIN bound decides only whether a
            # hash is verified. Asking for both at once meant an oversized PIN
            # against a real account was recorded as an attempt on a name that
            # matches nothing, which is the opposite of what happened.
            searchable = len(username) <= MAX_USERNAME_CHARS
            checkable = len(pin) <= MAX_PIN_CHARS
            if locked_until > now:
                # Named, so the activity page can report an account somebody is
                # being kept out of rather than only a count of refusals. No hash
                # is verified here -- the attempt was refused before any PIN was
                # considered, which is why the PIN's length does not arise.
                account = conn.execute("SELECT id,username FROM users WHERE username=?", (username,)).fetchone() if searchable else None
                self._record_login_event(conn, account, LOGIN_BLOCKED, origin_digest)
                return None, max(1, int(locked_until - now + 0.999))
            row = conn.execute(
                f"SELECT password_hash,{_ACCOUNT_COLUMNS} FROM users WHERE username=?", (username,)
            ).fetchone() if searchable else None
            if row and checkable and check_password_hash(row["password_hash"], pin):
                conn.executemany("DELETE FROM login_attempts WHERE scope_key=?", ((scope,) for scope in scopes))
                self._record_login_event(conn, row, LOGIN_SUCCESS, origin_digest)
                return {key: row[key] for key in ACCOUNT_FIELDS}, 0
            retry_after = 0
            for scope in scopes:
                previous = attempts.get(scope)
                in_window = previous and now - float(previous["window_started"]) < LOGIN_WINDOW_SECONDS
                count = int(previous["failure_count"]) if in_window else 0
                started = float(previous["window_started"]) if in_window else now
                count += 1
                lock_until = now + LOGIN_LOCK_SECONDS if count >= LOGIN_FAILURE_LIMIT else 0
                retry_after = max(retry_after, int(lock_until - now))
                conn.execute(
                    """INSERT INTO login_attempts(scope_key,failure_count,window_started,locked_until)
                       VALUES (?,?,?,?) ON CONFLICT(scope_key) DO UPDATE SET
                       failure_count=excluded.failure_count, window_started=excluded.window_started,
                       locked_until=excluded.locked_until""",
                    (scope, count, started, lock_until),
                )
            self._record_login_event(conn, row, LOGIN_FAILURE, origin_digest)
            return None, retry_after

    def _record_login_event(
        self,
        conn: sqlite3.Connection,
        account: sqlite3.Row | None,
        outcome: str,
        client_digest: str,
    ) -> None:
        """Append one attempt to the sign-in history, inside the caller's transaction.

        Written from ``authenticate_limited`` -- the login endpoint's only door --
        so an attempt is recorded exactly once, under the same serialized writer
        slot that decided its outcome. A history row can therefore never
        contradict the lockout state it was recorded alongside.
        """

        conn.execute(
            "INSERT INTO login_events(user_id,username,outcome,client_digest) VALUES (?,?,?,?)",
            (
                account["id"] if account else None,
                account["username"] if account else None,
                outcome,
                client_digest,
            ),
        )
        # What is kept when the cap is reached, in order of what an outsider can
        # manufacture:
        #
        #   1. Successful sign-ins. Only somebody holding a PIN can produce one,
        #      so this is the record that must survive a flood -- it is what
        #      "last signed in" and "never signed in" are read from, and those
        #      being wrong is worse than a failure count being short.
        #   2. Failures and lockouts against a real account. Cheap to produce in
        #      bulk (a locked-out attempt is refused before any hash is checked),
        #      but they name an account somebody is working on, which is worth
        #      keeping over the alternative.
        #   3. Attempts on names matching no account -- unlimited and anonymous.
        #
        # Within each group the newest rows are kept. A deployment whose own
        # sign-ins fill the cap loses its oldest ones, which is what a cap means.
        #
        # Only once the history has run LOGIN_EVENT_PRUNE_SLACK rows past the
        # cap, and then back to the cap in one go: this ordering is a full scan
        # and a temporary sort, and running it on every attempt would hand
        # anyone who filled the cap a way to slow every later sign-in down. The
        # count itself is cheap enough to check each time -- it reads an index,
        # not the table.
        if conn.execute("SELECT COUNT(*) FROM login_events").fetchone()[0] > LOGIN_EVENT_ROW_CAP + LOGIN_EVENT_PRUNE_SLACK:
            # Ordered by when the attempt happened, not by the order rows were
            # written. They agree until the clock is put back -- an NTP step, a
            # corrected timezone -- after which a row inserted later carries an
            # earlier timestamp. Evicting by id would then discard an attempt
            # newer than ones it kept, while the retention panel reports the
            # oldest surviving *timestamp* as a clean cutoff: a window starting
            # after that cutoff would look complete with an event missing from
            # inside it. Id remains the tie-break, so rows sharing a second keep
            # their insertion order.
            doomed = f"""SELECT id FROM login_events
                          ORDER BY (outcome = '{LOGIN_SUCCESS}') DESC, (user_id IS NOT NULL) DESC,
                                   occurred_at DESC, id DESC
                          LIMIT -1 OFFSET {int(LOGIN_EVENT_ROW_CAP)}"""
            # Counted per tier before they go, because afterwards there is
            # nothing left to count. A tier trimmed away entirely would
            # otherwise be silent -- the page would show "0 attempts on
            # unrecognised names" with nothing to say they had been dropped,
            # which reads as "none happened".
            # The newest timestamp going in each tier comes out in the same
            # pass, for the same reason the counts do: once the rows are gone
            # nothing can say how far the trim reached. The surviving rows are
            # not a substitute -- a clock put back lets a later attempt land
            # dated below this line, and then the oldest surviving row is no
            # longer where the history becomes complete.
            by_tier = conn.execute(
                f"""SELECT SUM(outcome = '{LOGIN_SUCCESS}') AS success,
                           SUM(outcome <> '{LOGIN_SUCCESS}' AND user_id IS NOT NULL) AS attributed,
                           SUM(user_id IS NULL) AS unattributed,
                           COUNT(*) AS total,
                           -- A second later than the newest attempt dropped,
                           -- because that second is not intact and this value
                           -- is read as one that is. Timestamps here are whole
                           -- seconds, and the trim splits on id within one --
                           -- two sign-ins in the same second land on opposite
                           -- sides of the boundary -- so the dropped row's own
                           -- timestamp is also a surviving row's. Reporting
                           -- history as complete from it would cover a second
                           -- an attempt was deleted from, and an account
                           -- created in exactly that second would be called
                           -- never used on the strength of it. One second on
                           -- is the tightest boundary this column can express
                           -- and it errs towards claiming less.
                           datetime(MAX(CASE WHEN outcome = '{LOGIN_SUCCESS}'
                                    THEN occurred_at END), '+1 second') AS cutoff_success,
                           datetime(MAX(CASE WHEN outcome <> '{LOGIN_SUCCESS}' AND user_id IS NOT NULL
                                    THEN occurred_at END), '+1 second') AS cutoff_attributed,
                           datetime(MAX(CASE WHEN user_id IS NULL
                                    THEN occurred_at END), '+1 second') AS cutoff_unattributed
                      FROM login_events WHERE id IN ({doomed})"""
            ).fetchone()
            conn.execute(f"DELETE FROM login_events WHERE id IN ({doomed})")
            # Written here, where it is known, rather than inferred later from a
            # row count that cannot tell a trimmed file from a full one.
            # Each cutoff only ever moves forward. A prune that dropped
            # nothing in a tier carries NULL for it and must leave the stored
            # one alone, and a prune that ran after the clock went back can
            # produce a cutoff earlier than the one already recorded -- the
            # earlier trim still happened, so the later date is the true one.
            # '' stands in for "none recorded yet" inside the comparison and
            # sorts below every timestamp, so a first cutoff always wins over
            # it; NULLIF puts the NULL back.
            conn.execute(
                """INSERT INTO login_event_pruning(id, last_pruned_at, rows_dropped,
                                                   dropped_success, dropped_attributed, dropped_unattributed,
                                                   cutoff_success, cutoff_attributed, cutoff_unattributed)
                   VALUES (1, CURRENT_TIMESTAMP, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET
                       last_pruned_at = CURRENT_TIMESTAMP,
                       rows_dropped = rows_dropped + excluded.rows_dropped,
                       dropped_success = dropped_success + excluded.dropped_success,
                       dropped_attributed = dropped_attributed + excluded.dropped_attributed,
                       dropped_unattributed = dropped_unattributed + excluded.dropped_unattributed,
                       cutoff_success = NULLIF(MAX(COALESCE(cutoff_success, ''),
                                                   COALESCE(excluded.cutoff_success, '')), ''),
                       cutoff_attributed = NULLIF(MAX(COALESCE(cutoff_attributed, ''),
                                                      COALESCE(excluded.cutoff_attributed, '')), ''),
                       cutoff_unattributed = NULLIF(MAX(COALESCE(cutoff_unattributed, ''),
                                                        COALESCE(excluded.cutoff_unattributed, '')), '')""",
                (
                    self._count(by_tier["total"]),
                    self._count(by_tier["success"]),
                    self._count(by_tier["attributed"]),
                    self._count(by_tier["unattributed"]),
                    by_tier["cutoff_success"],
                    by_tier["cutoff_attributed"],
                    by_tier["cutoff_unattributed"],
                ),
            )

    def list_users(self) -> list[dict]:
        with self._connect() as conn:
            return [
                dict(row)
                for row in conn.execute(
                    "SELECT id,username,role,department,staff_level,created_at,updated_at"
                    " FROM users ORDER BY username"
                )
            ]

    def save_user(
        self,
        user_id: int | None,
        username: str,
        pin: str,
        role: str,
        department: str = DEFAULT_DEPARTMENT,
        staff_level: str = DEFAULT_STAFF_LEVEL,
    ) -> None:
        """Create or update one account, including where its holder works.

        ``department`` and ``staff_level`` default to the unrestricted values, so
        a caller that has no opinion -- the bootstrap admin, a test -- creates an
        account narrowed to nothing. They are validated rather than coerced: the
        two defaults are the *widest* values, and quietly substituting one for a
        misspelling would hand an account more of GREMLIN than was asked for
        once these do filter pages.
        """

        username = username.strip()
        if not username or role not in ROLES or (user_id is None and not pin):
            raise ValueError("Username, a valid role, and a PIN for new users are required.")
        if department not in DEPARTMENTS:
            raise ValueError(f"Department must be one of: {', '.join(DEPARTMENTS)}.")
        if staff_level not in STAFF_LEVELS:
            raise ValueError(f"Level must be one of: {', '.join(STAFF_LEVELS)}.")
        _check_credential_bounds(username, pin)
        with self._connect() as conn:
            # The last-admin count and the mutation are one serialized decision.
            # Without taking the writer slot first, two concurrent demotions can
            # both observe two admins and then leave the database with none.
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute("SELECT role FROM users WHERE id=?", (user_id,)).fetchone() if user_id else None
            if user_id and existing is None:
                raise ValueError("That user no longer exists.")
            if existing and existing["role"] == "admin" and role != "admin":
                if conn.execute("SELECT COUNT(*) FROM users WHERE role='admin'").fetchone()[0] <= 1:
                    raise ValueError("The last administrator cannot be demoted.")
            if user_id is None:
                conn.execute(
                    "INSERT INTO users(username,password_hash,role,department,staff_level) VALUES (?,?,?,?,?)",
                    (username, generate_password_hash(pin), role, department, staff_level),
                )
            elif pin:
                conn.execute("""UPDATE users SET username=?,password_hash=?,role=?,department=?,staff_level=?,
                             credential_version=credential_version+1,updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                             (username, generate_password_hash(pin), role, department, staff_level, user_id))
            else:
                # No credential_version bump: department and staff level are
                # re-read from this table on every request, so a change to
                # either reaches the account on its next page load without
                # ending the session the way a replaced PIN does.
                conn.execute(
                    """UPDATE users SET username=?,role=?,department=?,staff_level=?,
                       updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                    (username, role, department, staff_level, user_id),
                )

    def delete_user(self, user_id: int, current_user_id: int) -> None:
        if user_id == current_user_id:
            raise ValueError("You cannot remove the account currently in use.")
        with self._connect() as conn:
            # Serialize the invariant check with DELETE for the same reason as
            # save_user's admin-demotion path.
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT role FROM users WHERE id=?", (user_id,)).fetchone()
            if row and row["role"] == "admin" and conn.execute("SELECT COUNT(*) FROM users WHERE role='admin'").fetchone()[0] <= 1:
                raise ValueError("The last administrator cannot be removed.")
            conn.execute("DELETE FROM users WHERE id=?", (user_id,))

    def record_change(self, user: dict, action: str) -> None:
        """Stamp who performed a protected write and when it occurred."""
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO audit_log(user_id,username,action) VALUES (?,?,?)",
                (user["id"], user["username"], action),
            )

    # -- activity ------------------------------------------------------------
    #
    # What the developer area's user-activity page reads. Two tables answer it:
    # login_events, which says who arrived and when, and audit_log, which says
    # what they changed once inside. Both are already here -- nothing about
    # activity is stored in GREMLIN.db, so a plant with an empty analysis
    # database still has a complete account of its users.
    #
    # Every timestamp in both tables is SQLite's CURRENT_TIMESTAMP, which is UTC
    # with no zone suffix. Days and hours are therefore bucketed in UTC; the page
    # says so, and shifts the hour-of-day chart into the reader's own zone.

    def activity_report(
        self,
        days: int = ACTIVITY_DEFAULT_DAYS,
        *,
        recent_limit: int = ACTIVITY_RECENT_LIMIT,
    ) -> dict:
        """Sign-in and change activity over the last ``days`` calendar days.

        One connection answers the whole page: a summary, a row per account, a
        daily and an hour-of-day distribution, the most recent sign-ins and
        changes, and which protected endpoints were used. Read-only.
        """

        days = self._activity_days(days)
        recent_limit = max(1, min(int(recent_limit), 200))
        with self._connect() as conn:
            # One snapshot for the whole report. Each section below is its own
            # SELECT, and a login committing between two of them would otherwise
            # be counted by one and not the other -- a summary that does not add
            # up to the table beneath it reads as a bug in the page rather than
            # as a request that overlapped a sign-in. Deferred rather than
            # IMMEDIATE: this only reads, and taking the writer slot would make
            # opening the page block somebody trying to sign in.
            conn.execute("BEGIN")
            # The cutoff is midnight UTC of the first day in the window, so the
            # window is exactly `days` calendar days ending with today rather
            # than a rolling interval that cuts today's morning in half. Both
            # timestamp columns are "YYYY-MM-DD HH:MM:SS", so a string
            # comparison against this is a date comparison.
            first_day = conn.execute("SELECT date('now', ?)", (f"-{days - 1} days",)).fetchone()[0]
            window_start = f"{first_day} 00:00:00"
            today = conn.execute("SELECT date('now')").fetchone()[0]
            # Bounded at both ends, not just the near one. A row can carry a
            # timestamp past today if the clock was put back after it was
            # written, and a lower bound alone counts it everywhere except the
            # daily series -- which only emits buckets up to today. The summary
            # would then disagree with the chart beneath it, about days the
            # window does not claim to cover. Exclusive, at the midnight that
            # ends today.
            tomorrow = conn.execute("SELECT date('now', '+1 day')").fetchone()[0]
            window = (window_start, f"{tomorrow} 00:00:00")
            # Read first, because how far back the history reaches decides what
            # the per-account rows are entitled to claim about an account that
            # has no sign-in in it.
            retention = self._activity_retention(conn)
            return {
                "window": {
                    "days": days,
                    "start": window_start,
                    "first_day": first_day,
                    "last_day": today,
                    "timezone": "UTC",
                },
                "summary": self._activity_summary(conn, *window),
                "users": self._activity_users(conn, *window, retention["sign_in_history_from"]),
                "daily": self._activity_daily(conn, *window, first_day, today),
                "hourly": self._activity_hourly(conn, *window),
                "recent_logins": self._activity_recent_logins(conn, *window, recent_limit),
                "recent_changes": self._activity_recent_changes(conn, *window, recent_limit),
                "top_actions": self._activity_top_actions(conn, *window),
                "retention": {"login_event_cap": LOGIN_EVENT_ROW_CAP, **retention},
            }

    @staticmethod
    def _activity_days(days: int) -> int:
        """Bound the window a caller may ask for."""

        try:
            days = int(days)
        except (TypeError, ValueError):
            raise ValueError("The activity window must be a whole number of days.") from None
        if not 1 <= days <= ACTIVITY_MAX_DAYS:
            raise ValueError(f"The activity window must be between 1 and {ACTIVITY_MAX_DAYS} days.")
        return days

    @staticmethod
    def _count(value) -> int:
        """SUM over no rows is NULL; every count this page shows is a number."""

        return int(value or 0)

    def _activity_summary(self, conn: sqlite3.Connection, window_start: str, window_end: str) -> dict:
        logins = conn.execute(
            """SELECT SUM(outcome='success') AS successes,
                      SUM(outcome='failure') AS failures,
                      SUM(outcome='blocked') AS blocked,
                      -- Only accounts that still exist, because this is shown
                      -- against the number of accounts configured: counting a
                      -- since-removed account here would let the page report
                      -- "2 of 1 accounts used". Their sign-ins are not lost --
                      -- logins_by_removed_accounts below is where they are said.
                      COUNT(DISTINCT CASE WHEN outcome='success'
                            AND user_id IN (SELECT id FROM users) THEN user_id END) AS users_seen,
                      COUNT(DISTINCT CASE WHEN outcome='success' THEN client_digest END) AS sources,
                      COUNT(DISTINCT CASE WHEN outcome='success' THEN date(occurred_at) END) AS days_used,
                      SUM(outcome<>'success' AND user_id IS NULL) AS unknown_account_attempts
                 FROM login_events WHERE occurred_at >= ? AND occurred_at < ?""",
            (window_start, window_end),
        ).fetchone()
        changes = conn.execute(
            """SELECT COUNT(*) AS changes,
                      -- Current accounts only, for the same reason as users_seen
                      -- above: this is shown as "by N accounts" beside a roster
                      -- of the accounts that exist, and the per-account table
                      -- below is built from that roster. Counting an author
                      -- whose account has since been removed would report more
                      -- authors than there are accounts, and leave the column
                      -- of change totals not adding up to this number.
                      COUNT(DISTINCT CASE WHEN user_id IN (SELECT id FROM users)
                            THEN user_id END) AS authors,
                      SUM(user_id NOT IN (SELECT id FROM users)) AS by_removed
                 FROM audit_log WHERE changed_at >= ? AND changed_at < ?""",
            (window_start, window_end),
        ).fetchone()
        accounts = conn.execute(
            """SELECT COUNT(*) AS accounts,
                      -- Bounded at the report's end for the same reason the
                      -- per-account last_login is, and it has to be the same
                      -- bound: this tile is read directly against that column.
                      -- A sign-in dated after the last day of the window comes
                      -- from a clock that was put back, and counting it here
                      -- while the account's own row still shows no sign-in
                      -- would have the page say "every account has signed in"
                      -- above a row reading "Never signed in".
                      SUM(id NOT IN (SELECT user_id FROM login_events
                                      WHERE user_id IS NOT NULL AND outcome='success'
                                        AND occurred_at < ?)) AS never_signed_in
                 FROM users""",
            (window_end,),
        ).fetchone()
        # Sign-ins by an account that no longer exists. The history outlives the
        # user row on purpose, and an administrator looking at a window that
        # includes somebody's last week should see that the activity was theirs
        # rather than find it missing from every per-account row.
        # The same treatment for every kind of event a removed account can leave
        # behind. Sign-ins and changes were already reported this way; refusals
        # were not, so the Wrong PIN totals counted attempts on an account that
        # appears in no row of the table below -- and once those attempts fall
        # out of the recent list, nothing said which account had been targeted.
        departed = conn.execute(
            f"""SELECT SUM(outcome='{LOGIN_SUCCESS}') AS logins,
                       SUM(outcome<>'{LOGIN_SUCCESS}') AS refusals
                  FROM login_events e
                 WHERE e.occurred_at >= ? AND e.occurred_at < ? AND e.user_id IS NOT NULL
                   AND NOT EXISTS (SELECT 1 FROM users u WHERE u.id = e.user_id)""",
            (window_start, window_end),
        ).fetchone()
        return {
            "successful_logins": self._count(logins["successes"]),
            "failed_logins": self._count(logins["failures"]),
            "blocked_logins": self._count(logins["blocked"]),
            "users_seen": self._count(logins["users_seen"]),
            "distinct_sources": self._count(logins["sources"]),
            "days_with_logins": self._count(logins["days_used"]),
            "unknown_account_attempts": self._count(logins["unknown_account_attempts"]),
            "changes": self._count(changes["changes"]),
            "change_authors": self._count(changes["authors"]),
            "changes_by_removed_accounts": self._count(changes["by_removed"]),
            "accounts": self._count(accounts["accounts"]),
            "never_signed_in": self._count(accounts["never_signed_in"]),
            "logins_by_removed_accounts": self._count(departed["logins"]),
            "refusals_by_removed_accounts": self._count(departed["refusals"]),
        }

    def _activity_users(
        self, conn: sqlite3.Connection, window_start: str, window_end: str, history_start: str | None
    ) -> list[dict]:
        """One row per account: how often, how recently, and what they changed.

        Accounts with nothing in the window are still listed, at zero. "Who has
        not signed in" is the question this page is most often opened to answer,
        and an account that is absent from the table cannot answer it.

        ``history_start`` is the point from which the record of successful
        sign-ins is complete -- when recording began, or the last trim line if
        sign-ins have been dropped since -- and it is what separates "never
        signed in" from "not seen since we started looking". On a deployment
        upgrading to this feature the history starts empty -- nothing can reconstruct last year's sign-ins -- so every
        existing account has no recorded sign-in on the first day, including the
        ones in use every shift. Calling those "never signed in" would be a
        false statement about real people, and the sort an administrator might
        act on by removing the account. Each row therefore carries whether the
        history covers the whole life of that account, and the page says only
        what that supports.
        """


        rows = conn.execute(
            """SELECT u.id, u.username, u.role, u.created_at,
                      COALESCE(w.logins, 0) AS logins,
                      COALESCE(w.failures, 0) AS failed_logins,
                      COALESCE(w.blocked, 0) AS blocked_logins,
                      COALESCE(w.active_days, 0) AS active_days,
                      COALESCE(w.sources, 0) AS distinct_sources,
                      COALESCE(c.changes, 0) AS changes,
                      c.last_change,
                      a.last_login,
                      COALESCE(a.total_logins, 0) AS total_logins
                 FROM users u
                 LEFT JOIN (
                      SELECT user_id,
                             SUM(outcome='success') AS logins,
                             SUM(outcome='failure') AS failures,
                             SUM(outcome='blocked') AS blocked,
                             COUNT(DISTINCT CASE WHEN outcome='success' THEN date(occurred_at) END) AS active_days,
                             COUNT(DISTINCT CASE WHEN outcome='success' THEN client_digest END) AS sources
                        FROM login_events WHERE occurred_at >= ? AND occurred_at < ? AND user_id IS NOT NULL
                       GROUP BY user_id
                 ) w ON w.user_id = u.id
                 LEFT JOIN (
                      -- Unbounded at the start, because "last signed in" means
                      -- ever, not within the window. Bounded at the end all the
                      -- same: a sign-in dated after the report's own last day
                      -- is one the clock produced by being put back, and a
                      -- table reporting a sign-in from the future is wrong
                      -- whatever the window is.
                      SELECT user_id, COUNT(*) AS total_logins, MAX(occurred_at) AS last_login
                        FROM login_events
                       WHERE outcome='success' AND user_id IS NOT NULL AND occurred_at < ?
                       GROUP BY user_id
                 ) a ON a.user_id = u.id
                 LEFT JOIN (
                      SELECT user_id, COUNT(*) AS changes, MAX(changed_at) AS last_change
                        FROM audit_log WHERE changed_at >= ? AND changed_at < ? GROUP BY user_id
                 ) c ON c.user_id = u.id
                ORDER BY logins DESC, changes DESC, u.username COLLATE NOCASE""",
            (window_start, window_end, window_end, window_start, window_end),
        ).fetchall()
        users = []
        for row in rows:
            entry = dict(row)
            # True only when the history was already being recorded when this
            # account was created, so an absence of sign-ins in it really does
            # mean the account has never been used. Both timestamps are UTC
            # CURRENT_TIMESTAMP text, so they compare directly; a legacy row
            # with no created_at is treated as the older of the two, which is
            # the answer that claims less.
            entry["tracked_since_created"] = bool(
                history_start and entry["created_at"] and history_start <= entry["created_at"]
            )
            # How concentrated the use was: five sign-ins across five days is a
            # daily user, five in one day is somebody who kept being signed out.
            entry["logins_per_active_day"] = (
                round(entry["logins"] / entry["active_days"], 1) if entry["active_days"] else 0.0
            )
            users.append(entry)
        return users

    def _activity_daily(
        self, conn: sqlite3.Connection, window_start: str, window_end: str, first_day: str, last_day: str
    ) -> list[dict]:
        """Sign-ins per UTC day, with quiet days present as zeroes.

        Gaps are filled here rather than in the browser so the trend is read as
        a calendar: a week nobody used GREMLIN should be a flat stretch, not two
        adjacent bars.
        """


        counted = {
            row["day"]: row
            for row in conn.execute(
                """SELECT date(occurred_at) AS day,
                          SUM(outcome='success') AS logins,
                          SUM(outcome<>'success') AS refused,
                          COUNT(DISTINCT CASE WHEN outcome='success' THEN user_id END) AS users
                     FROM login_events WHERE occurred_at >= ? AND occurred_at < ?
                    GROUP BY day""",
                (window_start, window_end),
            )
        }
        changes = {
            row["day"]: row["changes"]
            for row in conn.execute(
                "SELECT date(changed_at) AS day, COUNT(*) AS changes FROM audit_log WHERE changed_at >= ? AND changed_at < ? GROUP BY day",
                (window_start, window_end),
            )
        }
        series = []
        day = date.fromisoformat(first_day)
        end = date.fromisoformat(last_day)
        while day <= end:
            key = day.isoformat()
            row = counted.get(key)
            series.append({
                "day": key,
                "logins": self._count(row["logins"]) if row else 0,
                "refused": self._count(row["refused"]) if row else 0,
                "users": self._count(row["users"]) if row else 0,
                "changes": self._count(changes.get(key)),
            })
            day += timedelta(days=1)
        return series

    def _activity_hourly(self, conn: sqlite3.Connection, window_start: str, window_end: str) -> list[dict]:
        """Successful sign-ins in quarter-hour buckets, each carrying its date.

        The page draws these as one 24-hour distribution in the reader's local
        time, and the browser is what converts them -- which is the only way to
        get the rules right, since an hour of the year is not a fixed distance
        from UTC and a window spanning a daylight-saving change would otherwise
        put half its sign-ins in the wrong local hour.

        Quarter-hours rather than hours because not every zone is a whole number
        of hours from UTC. In UTC+05:30 an hour bucket straddles two local
        hours, so converting the hour's start put 00:45 UTC -- 06:15 there -- in
        the 05:00 column. Every offset in use is a multiple of fifteen minutes,
        so bucketing to that makes the conversion exact everywhere rather than
        for most of the world.

        No larger than before in practice: only buckets with a sign-in in them
        are returned, so the row count follows how many sign-ins there are, not
        how finely the day is divided.
        """


        return [
            {
                "day": row["day"],
                "hour": int(row["hour"]),
                "minute": int(row["quarter"]) * 15,
                "logins": self._count(row["logins"]),
            }
            for row in conn.execute(
                f"""SELECT date(occurred_at) AS day,
                          CAST(strftime('%H', occurred_at) AS INTEGER) AS hour,
                          CAST(strftime('%M', occurred_at) AS INTEGER) / 15 AS quarter,
                          COUNT(*) AS logins
                     FROM login_events
                    WHERE occurred_at >= ? AND occurred_at < ? AND outcome='{LOGIN_SUCCESS}'
                    GROUP BY day, hour, quarter ORDER BY day, hour, quarter""",
                (window_start, window_end),
            )
            if row["hour"] is not None
        ]

    def _activity_recent_logins(
        self, conn: sqlite3.Connection, window_start: str, window_end: str, limit: int
    ) -> list[dict]:
        return [
            {
                "username": row["username"],
                "outcome": row["outcome"],
                "occurred_at": row["occurred_at"],
                # False means the typed name matched no account, which is why
                # there is no username to show alongside it.
                "known_account": bool(row["known_account"]),
            }
            for row in conn.execute(
                # By when the attempt happened, not by the order rows landed.
                # They agree until the clock is put back, after which a row
                # written later carries an earlier time -- and then an id sort
                # puts an older attempt above a newer one in a list whose whole
                # claim is that it is the latest. The cap evicts on this same
                # order, so the two agree about which attempts are recent.
                """SELECT username, outcome, occurred_at, user_id IS NOT NULL AS known_account
                     FROM login_events WHERE occurred_at >= ? AND occurred_at < ?
                    ORDER BY occurred_at DESC, id DESC LIMIT ?""",

                (window_start, window_end, limit),
            )
        ]

    def _activity_recent_changes(
        self, conn: sqlite3.Connection, window_start: str, window_end: str, limit: int
    ) -> list[dict]:
        return [
            dict(row)
            for row in conn.execute(
                # Newest by when the change was made, id as the tie-break, for
                # the reason given on the sign-in feed above.
                """SELECT username, action, changed_at FROM audit_log
                    WHERE changed_at >= ? AND changed_at < ?
                    ORDER BY changed_at DESC, id DESC LIMIT ?""",

                (window_start, window_end, limit),
            )
        ]

    def _activity_top_actions(
        self, conn: sqlite3.Connection, window_start: str, window_end: str, limit: int = 10
    ) -> list[dict]:
        """Which protected endpoints the window's writes went through."""


        return [
            dict(row)
            for row in conn.execute(
                """SELECT action, COUNT(*) AS count, COUNT(DISTINCT user_id) AS users,
                          MAX(changed_at) AS last_used
                     FROM audit_log WHERE changed_at >= ? AND changed_at < ?
                    GROUP BY action ORDER BY count DESC, action LIMIT ?""",
                (window_start, window_end, limit),
            )
        ]

    def _activity_retention(self, conn: sqlite3.Connection) -> dict:
        """How much history there is, so a window can be read against its limits.

        Reported per tier, and there are three of them, because that is how many
        the cap evicts in: successful sign-ins, then failures and lockouts
        against a real account, then attempts on names matching nothing. One
        cutoff for "refusals" would span the last two, and those do not run out
        together -- the anonymous tier goes first, so a single date would date
        the surviving attributed refusals and quietly claim the unrecognised-name
        history is complete that far back too, which is the count somebody
        watching for an attack is reading.

        Within a tier eviction *is* chronological -- each is pruned newest-first,
        so what survives is an unbroken run ending at the present. That is what
        makes one date per tier a complete description of it, rather than a
        warning that something somewhere is missing.
        """

        row = conn.execute(
            f"""SELECT COUNT(*) AS events,
                      MIN(occurred_at) AS first_event,
                      MAX(occurred_at) AS last_event,
                      MIN(CASE WHEN outcome='{LOGIN_SUCCESS}' THEN occurred_at END) AS first_success,
                      MIN(CASE WHEN outcome<>'{LOGIN_SUCCESS}' AND user_id IS NOT NULL
                               THEN occurred_at END) AS first_attributed_refusal,
                      MIN(CASE WHEN user_id IS NULL THEN occurred_at END) AS first_unattributed_refusal
                 FROM login_events"""
        ).fetchone()
        pruning = conn.execute(
            """SELECT last_pruned_at, rows_dropped, dropped_success,
                      dropped_attributed, dropped_unattributed, dropped_unclassified,
                      cutoff_success, cutoff_attributed, cutoff_unattributed
                 FROM login_event_pruning WHERE id = 1"""
        ).fetchone()
        events = self._count(row["events"])

        def tier(kind: str, first_kept: str | None, dropped: str, cutoff: str) -> dict:
            """One tier's oldest surviving attempt, and where it is complete from.

            Usually the same date. They part after the clock is put back: an
            attempt written then can be dated below the trim line and survive,
            so the tier holds a row older than a stretch that was dropped.
            Dating the tier from that row would claim the gap above it is
            covered, which is the one thing this panel exists to be right
            about, so completeness is taken from the recorded trim line
            whenever it reaches further forward. Both are reported -- the
            oldest row held is still a true statement about the file, and it
            is what a count of stored attempts is spread over.
            """

            trimmed = pruning[cutoff] if pruning else None
            if first_kept and trimmed:
                complete_since = max(first_kept, trimmed)
            else:
                complete_since = first_kept or trimmed
            return {
                "kind": kind,
                "first_kept": first_kept,
                # The first instant the trim left intact -- inclusive, like
                # every other date here, so the two compose by taking whichever
                # is later. None where nothing of this kind was ever dropped,
                # and also where it was dropped by a version that did not
                # record the line: in both cases the surviving rows are all
                # there is to go on.
                "intact_from": trimmed,
                "complete_since": complete_since,
                "dropped": self._count(pruning[dropped]) if pruning else 0,
            }
        return {
            "login_events": events,
            "first_event": row["first_event"],
            "last_event": row["last_event"],
            # From here on, the record of successful sign-ins is complete, and
            # it is the only date "never signed in" may rest on. Two facts have
            # to hold for that claim, and first_event carries only one of them:
            # that something was being recorded (nothing before the first row
            # of any kind can be said either way), and that no sign-in from the
            # period has since been dropped. They agree until a clock is put
            # back, after which a back-dated *refusal* can become the oldest
            # row in the file and pull first_event below a trim line that took
            # real sign-ins with it -- and an account created in between would
            # then be called never used on the strength of sign-ins that were
            # deleted. Both halves are inclusive -- the trim line is stored a
            # second past what it removed, so an account created exactly on it
            # is covered -- and the later of the two is the answer.
            # The success tier's own oldest surviving row is the wrong
            # correction for this: with nothing pruned it can sit long after
            # recording began, on a deployment where the first week was all
            # wrong PINs, and an account created in that week has genuinely
            # never signed in. The trim line is the piece that carries the
            # pruning, so it is the piece that belongs here.
            "sign_in_history_from": max(
                filter(None, (row["first_event"], pruning["cutoff_success"] if pruning else None)),
                default=None,
            ),
            # Per tier: the oldest attempt still held, and how many of that kind
            # have been dropped. A tier trimmed away completely has no surviving
            # row to date, so the count is the only thing left that says it
            # happened -- and "0 recorded, 1,204 dropped" is a different fact
            # from "0 recorded" on its own.
            "tiers": [
                tier("success", row["first_success"], "dropped_success", "cutoff_success"),
                tier(
                    "attributed_refusal",
                    row["first_attributed_refusal"],
                    "dropped_attributed",
                    "cutoff_attributed",
                ),
                tier(
                    "unattributed_refusal",
                    row["first_unattributed_refusal"],
                    "dropped_unattributed",
                    "cutoff_unattributed",
                ),
            ],
            # Full, but not necessarily short of anything: trimming starts once
            # the history runs LOGIN_EVENT_PRUNE_SLACK rows past the cap.
            "capped": events >= LOGIN_EVENT_ROW_CAP,
            # Dropped by a version that did not yet count per tier, so which
            # kind they were is not knowable. Reported rather than folded into
            # one of the tiers, because either choice would be a guess.
            "dropped_unclassified": self._count(pruning["dropped_unclassified"]) if pruning else 0,
            # Whether anything has actually been dropped, which is the question
            # "is this history complete?" really asks.
            "pruned": pruning is not None,
            "last_pruned_at": pruning["last_pruned_at"] if pruning else None,
            "rows_dropped": self._count(pruning["rows_dropped"]) if pruning else 0,
        }
