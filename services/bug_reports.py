"""Bug reports, stored beside GREMLIN rather than inside it.

Anyone who uses GREMLIN can file one from the footer's "Report a Bug" page, and
administrators work through them on the developer area's own page. Two states
and nothing more -- a report is open until somebody resolves it, and a resolved
one can be reopened -- because the value here is that a report is not lost, not
that it can be routed through a workflow.

The file lives on the shared drive rather than next to GREMLIN.db. Reports are
about the program itself, so they are the same set of reports whichever machine
files them and whichever copy of the app is being run; keeping them in the
analysis database would give every deployment its own private list, and would
also mean the page that says "GREMLIN is broken" writes to the very database it
may be reporting broken.

That share can be unreachable -- a laptop off the plant network, a drive letter
that never got mapped. Every method here raises BugReportStoreError for that
rather than letting sqlite3.OperationalError out, so the pages above can say
which path could not be opened instead of returning a 500. Nothing in this
module runs at import, so an unreachable share never stops GREMLIN starting.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import sqlite3
import time
from functools import lru_cache
from datetime import datetime, timezone
from pathlib import Path

# The one location for auxillary.db: the GREMLIN Program folder on the shared
# drive, alongside the other global databases. Override with the
# GREMLIN_BUGS_DB_PATH environment variable -- which is what the tests do, and
# what a deployment that maps the share to a different letter would set.
DEFAULT_BUG_DB_PATH = Path(
    r"Z:\FACIL\MAIN-ENG\901 Reliability Projects\901 Reliability Projects"
    r"\GREMLIN Program\GREMLIN Global DB\auxillary.db"
)

# A share is a slower writer than a local file and several people may file at
# once, so wait for the lock rather than failing the report.
DB_BUSY_TIMEOUT_SECONDS = 30

STATUS_OPEN = "open"
STATUS_RESOLVED = "resolved"
STATUSES = (STATUS_OPEN, STATUS_RESOLVED)

# Ordered by how much attention the report is asking for, so the dashboard can
# sort by it. Worded for the person filing, who knows what it stopped them doing
# but not how the code is arranged.
SEVERITIES = ("blocking", "major", "minor")
SEVERITY_RANK = {name: rank for rank, name in enumerate(SEVERITIES)}
DEFAULT_SEVERITY = "minor"

# The schema, as the statements that make it. Named up here rather than written
# inline in ensure_schema because the status check derives what a complete
# database looks like by running these into an empty in-memory one: a single
# source of truth, so a check can never come to disagree with what is actually
# created. Without that, a file that is present but damaged -- an empty file
# made by hand, a copy taken before the limiter's table existed, a table someone
# has altered -- opens perfectly well, passes the check, and then fails on the
# first query that needs what is missing.
#
# `revision` is bumped by every change, and is what the dashboard sends back to
# prove the row it is acting on is the row it drew -- see set_status. A status
# alone cannot do that job, because open -> resolved -> open reads as though
# nothing happened.
#
# The comments live out here rather than inside the statements: sqlite stores
# statement text verbatim and re-parses it for ALTER TABLE, where a trailing
# comment on the last column makes a later DROP COLUMN fail to parse.
SCHEMA_DDL = {
    "bug_reports": f"""
        CREATE TABLE IF NOT EXISTS bug_reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            description TEXT NOT NULL,
            area TEXT NOT NULL DEFAULT '',
            severity TEXT NOT NULL DEFAULT '{DEFAULT_SEVERITY}'
                CHECK(severity IN {SEVERITIES!r}),
            reporter TEXT NOT NULL DEFAULT '',
            reporter_user_id INTEGER,
            page_url TEXT NOT NULL DEFAULT '',
            user_agent TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT '{STATUS_OPEN}'
                CHECK(status IN {STATUSES!r}),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            resolved_at TEXT,
            resolved_by TEXT NOT NULL DEFAULT '',
            resolution_note TEXT NOT NULL DEFAULT '',
            revision INTEGER NOT NULL DEFAULT 1
        )
    """,
    # The submission limiter's working state. Separate from the reports
    # themselves because it is disposable: rows here expire and are swept,
    # whereas a report is kept until somebody deletes it. Nothing filed is ever
    # removed to make room.
    "submission_attempts": """
        CREATE TABLE IF NOT EXISTS submission_attempts (
            scope TEXT NOT NULL,
            filed_at REAL NOT NULL
        )
    """,
}
SCHEMA_TABLES = tuple(SCHEMA_DDL)

# The indexes are not part of the completeness check -- a database missing one
# is slow, not broken -- so they are kept apart from the tables above.
SCHEMA_INDEXES = (
    # The dashboard's default view is "open, newest first", and its counts group
    # by status; both read this rather than the table.
    "CREATE INDEX IF NOT EXISTS idx_bug_reports_status_created "
    "ON bug_reports(status, created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_submission_attempts_scope "
    "ON submission_attempts(scope, filed_at)",
)

# Columns ensure_schema can add to a database that predates them, each with the
# definition to add it with -- rather than a bare list of names and one ALTER
# that assumes they are all the same shape. Everything else in the schema
# arrives with the CREATE TABLE, so a database missing one of those is damaged
# rather than old, and running the schema again will not mend it: a different
# thing to tell an administrator, and the reason this is a separate list.
MIGRATED_COLUMNS = {"revision": "INTEGER NOT NULL DEFAULT 1"}

# The columns _summarise reads. Checked before it runs, so that a database
# missing one of them is reported as missing it rather than as unreadable.
_SUMMARY_COLUMNS = ("status", "severity", "created_at")

# Bounds on what one report may store. The form is open to anyone who can reach
# GREMLIN, including anonymously, so nothing it writes is allowed to be
# unbounded: sqlite never returns freed pages to the filesystem, and this file
# is on a share other people's work depends on.
MAX_TITLE_CHARS = 200
MAX_DESCRIPTION_CHARS = 8000
MAX_AREA_CHARS = 120
MAX_REPORTER_CHARS = 128
MAX_NOTE_CHARS = 2000
MAX_USER_AGENT_CHARS = 400

# How many reports a listing will return at once. The dashboard filters and
# searches server-side, so this bounds one response rather than the file; the
# dashboard pages past it with an offset rather than losing what is beyond.
LIST_LIMIT = 200
MAX_LIST_LIMIT = 500

# What one client may file in one window. The form is open to anyone who can
# reach GREMLIN, including anonymously, so without this a loop -- a stuck retry
# as easily as an attack -- could grow a file on a shared drive until the drive
# is full, and bury the genuine reports in what it wrote.
#
# Deliberately generous. Behind a reverse proxy with GREMLIN_TRUSTED_PROXY_HOPS
# unset, every anonymous reporter shares one address, so a tight limit would let
# one runaway client stop the whole plant filing. Twenty an hour is far past what
# a person files and still bounds a loop to a few hundred KB an hour.
SUBMISSION_LIMIT_PER_WINDOW = 20
SUBMISSION_WINDOW_SECONDS = 3600

# Same reasoning as the caps above: rotating client addresses must not grow the
# limiter's own table forever. Expired rows are swept on every submission; this
# bounds what a burst can leave behind between sweeps.
SUBMISSION_ATTEMPT_ROW_CAP = 10_000


class BugReportStoreError(RuntimeError):
    """The reports database could not be reached or read."""


class BugReportValidationError(ValueError):
    """The submitted report is missing something, or says something impossible."""


class BugReportConflictError(RuntimeError):
    """The report moved on before this change could be applied.

    Two administrators working the same queue can each have a page that still
    shows a report as open. Without this, the second click would overwrite the
    first one's resolution -- who resolved it, when, and the note saying what was
    done -- with its own, and that history cannot be recovered.
    """

    def __init__(self, report_id: int, expected: int, actual: int, actual_status: str) -> None:
        self.report_id = report_id
        self.expected = expected
        self.actual = actual
        self.actual_status = actual_status
        super().__init__(
            f"Report #{report_id} has changed since this page drew it, and is "
            f"now {actual_status}. Refresh to see where it stands before "
            "changing it."
        )


class BugReportRateLimitError(RuntimeError):
    """This client has filed its allowance of reports for now.

    Carries ``retry_after`` in seconds so the caller can tell the reporter when
    to come back, rather than only that it refused.
    """

    def __init__(self, retry_after: int) -> None:
        self.retry_after = max(1, int(retry_after))
        minutes = max(1, round(self.retry_after / 60))
        super().__init__(
            "Thanks \u2014 that is as many reports as we can take from you just now. "
            f"Please try again in about {minutes} minute{'s' if minutes != 1 else ''}. "
            "If this is urgent, tell the reliability team directly."
        )


# The queue's ordering, as SQL. Defined once because two things depend on it
# being identical: the ORDER BY that produces a page, and the cursor comparison
# that says where the next page starts. If those ever disagreed, paging would
# skip or repeat rows -- which is the failure the cursor exists to prevent.
_STATUS_RANK_SQL = f"CASE status WHEN '{STATUS_OPEN}' THEN 0 ELSE 1 END"
_SEVERITY_RANK_SQL = (
    "CASE severity "
    + " ".join(f"WHEN '{name}' THEN {rank}" for name, rank in SEVERITY_RANK.items())
    + f" ELSE {len(SEVERITIES)} END"
)
_ORDER_BY_SQL = (
    f"{_STATUS_RANK_SQL} ASC, {_SEVERITY_RANK_SQL} ASC, created_at DESC, id DESC"
)


def _encode_cursor(row: dict) -> str:
    """Where a page ended, as the full sort key of its last row.

    A position rather than a count. An offset says "skip 200 rows", so anything
    that leaves the result set between two requests shifts the rest forward and
    the next page steps over a report nobody ever saw. This says "resume after
    exactly this row", which no insertion or deletion elsewhere can move.
    """

    key = "|".join(
        str(part)
        for part in (
            0 if row["status"] == STATUS_OPEN else 1,
            SEVERITY_RANK.get(row["severity"], len(SEVERITIES)),
            row["created_at"],
            row["id"],
        )
    )
    return base64.urlsafe_b64encode(key.encode("utf-8")).decode("ascii")


def _decode_cursor(token: str) -> tuple[int, int, str, int]:
    """The sort key a cursor stands for, or a refusal if it is not one."""

    try:
        raw = base64.urlsafe_b64decode(token.encode("ascii")).decode("utf-8")
        status_rank, severity_rank, created_at, report_id = raw.split("|", 3)
        return int(status_rank), int(severity_rank), created_at, int(report_id)
    except (ValueError, TypeError, binascii.Error) as exc:
        raise BugReportValidationError(
            "That listing cursor is not one this page issued. Reload the list."
        ) from exc


def _limiter_scope(client_key: str) -> str:
    """The limiter's key for one caller, at a fixed storage cost.

    Hashed for the same reason the login limiter hashes its own: the caller does
    not control how much space its identifier takes in the table, and these rows
    are never displayed, so there is nothing here a plain digest fails to do.
    """

    return hashlib.sha256(str(client_key).encode("utf-8")).hexdigest()


def _now() -> str:
    """UTC, in the shape the rest of GREMLIN's timestamps take.

    Reports arrive from machines in one plant, but the reader may be anywhere,
    so what is stored is unambiguous and the browser renders it in local time.
    """

    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _clean(value: object, limit: int) -> str:
    """One submitted field: text, trimmed, and never longer than ``limit``."""

    text = "" if value is None else str(value).strip()
    return text[:limit]


def _sqlite_file_uri(uri: str) -> str:
    """A file: URI pathlib produced, in the form sqlite will accept.

    The two disagree about UNC paths, which is how half the plant reaches the
    share. ``as_uri()`` renders ``\\\\server\\share\\auxillary.db`` as
    ``file://server/share/auxillary.db``, putting the host in the URI's
    authority; sqlite rejects any authority that is not empty or ``localhost``
    -- "invalid uri authority: server" -- so a deployment that reaches the share
    by name rather than by a mapped drive letter would be told its perfectly
    readable database could not be opened.

    Moving the host down into the path leaves the authority empty, which is the
    form sqlite takes. A drive letter and a POSIX path both already come back
    with an empty authority (``file:///``) and are left alone.
    """

    if uri.startswith("file://") and not uri.startswith("file:///"):
        return "file:////" + uri[len("file://") :]
    return uri


@lru_cache(maxsize=1)
def _expected_columns() -> dict[str, tuple[str, ...]]:
    """Which columns each table should have, in the order they are declared.

    Read back off SCHEMA_DDL by running it into an empty in-memory database and
    asking sqlite what it made, rather than by listing the column names a second
    time here. A hand-kept list would drift from the CREATE TABLE, and a check
    that disagrees with what is actually created is worse than no check at all:
    it would call a healthy database incomplete, and nothing would be able to
    mend what it claims is wrong.

    Cached because it is the same answer every time, and computed on first use
    rather than at import, so this module still does nothing when it is loaded.
    """

    conn = sqlite3.connect(":memory:")
    try:
        conn.row_factory = sqlite3.Row
        for statement in SCHEMA_DDL.values():
            conn.execute(statement)
        return {
            table: tuple(
                row["name"]
                for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
            )
            for table in SCHEMA_DDL
        }
    finally:
        conn.close()


def _summarise(conn: sqlite3.Connection) -> dict:
    """The dashboard's tile counts, in one pass over the table.

    A function rather than a method because the status check reads it through a
    read-only connection while the dashboard reads it through the ordinary one.
    Defined once so the two can never come to disagree about what "open" counts.
    """

    counts = conn.execute(
        """
        SELECT
            COUNT(*) AS total,
            SUM(CASE WHEN status = ? THEN 1 ELSE 0 END) AS open_count,
            SUM(CASE WHEN status = ? THEN 1 ELSE 0 END) AS resolved_count,
            SUM(CASE WHEN status = ? AND severity = 'blocking' THEN 1 ELSE 0 END)
                AS open_blocking,
            MAX(created_at) AS latest_report
        FROM bug_reports
        """,
        (STATUS_OPEN, STATUS_RESOLVED, STATUS_OPEN),
    ).fetchone()
    return {
        "total": int(counts["total"] or 0),
        "open": int(counts["open_count"] or 0),
        "resolved": int(counts["resolved_count"] or 0),
        "open_blocking": int(counts["open_blocking"] or 0),
        "latest_report": counts["latest_report"],
    }


class BugReportStore:
    """Every read and write of auxillary.db."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._schema_ready = False

    # -- connection -------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        try:
            conn = sqlite3.connect(self.path, timeout=DB_BUSY_TIMEOUT_SECONDS)
        except sqlite3.Error as exc:
            raise BugReportStoreError(self._unreachable(exc)) from exc
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout = {DB_BUSY_TIMEOUT_SECONDS * 1000}")
        return conn

    def _unreachable(self, exc: Exception) -> str:
        return (
            f"The bug reports database at {self.path} could not be opened. "
            f"Check that the drive is mapped and reachable, or set "
            f"GREMLIN_BUGS_DB_PATH to another location. Details: {exc}"
        )

    def ensure_schema(self) -> None:
        """Create the folder, the file and the table, the first time one is needed.

        Called at the top of every public method rather than at startup: the
        share may be down when GREMLIN starts and up by the time somebody files
        a report, and the reverse. ``_schema_ready`` only caches success, so a
        failed attempt is retried on the next call.
        """

        if self._schema_ready:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise BugReportStoreError(self._unreachable(exc)) from exc
        try:
            with self._connect() as conn:
                for statement in SCHEMA_DDL.values():
                    conn.execute(statement)
                # A database created before revisions existed has the rest of
                # the table but not this column. Added rather than rebuilt, so
                # nothing filed is disturbed; existing rows start at revision 1.
                columns = {
                    row["name"]
                    for row in conn.execute("PRAGMA table_info(bug_reports)").fetchall()
                }
                for name, definition in MIGRATED_COLUMNS.items():
                    if name not in columns:
                        conn.execute(
                            f"ALTER TABLE bug_reports ADD COLUMN {name} {definition}"
                        )
                for statement in SCHEMA_INDEXES:
                    conn.execute(statement)
        except sqlite3.Error as exc:
            raise BugReportStoreError(self._unreachable(exc)) from exc
        self._schema_ready = True

    # -- writes -----------------------------------------------------------

    def submit(
        self,
        *,
        title: str,
        description: str,
        area: str = "",
        severity: str = DEFAULT_SEVERITY,
        reporter: str = "",
        reporter_user_id: int | None = None,
        page_url: str = "",
        user_agent: str = "",
        client_key: str | None = None,
    ) -> int:
        """Store one report and return its id.

        Only the title and the description are required. Everything else is
        context that helps whoever picks the report up, and a report filed
        without it is still worth having -- the alternative is a form somebody
        abandons, which stores nothing at all.

        ``client_key`` identifies who is filing, for the submission limit. Pass
        None to skip the limit entirely -- which is what the tests and any
        internal caller do; every path reachable from the public form passes one.
        Raises BugReportRateLimitError when that caller has filed its allowance.
        """

        title = _clean(title, MAX_TITLE_CHARS)
        description = _clean(description, MAX_DESCRIPTION_CHARS)
        if not title:
            raise BugReportValidationError("Give the report a short title.")
        if not description:
            raise BugReportValidationError("Describe what happened.")

        severity = _clean(severity, 32).lower() or DEFAULT_SEVERITY
        if severity not in SEVERITY_RANK:
            raise BugReportValidationError(
                f"Severity must be one of: {', '.join(SEVERITIES)}."
            )

        self.ensure_schema()
        stamp = _now()
        try:
            with self._connect() as conn:
                # BEGIN IMMEDIATE so the check and the insert are one step. A
                # deferred transaction would let parallel submissions all read
                # the same count and each conclude it was under the limit, which
                # is exactly the burst the limit exists to stop.
                conn.execute("BEGIN IMMEDIATE")
                if client_key is not None:
                    self._charge_submission(conn, client_key)
                cursor = conn.execute(
                    """
                    INSERT INTO bug_reports (
                        title, description, area, severity, reporter, reporter_user_id,
                        page_url, user_agent, status, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        title,
                        description,
                        _clean(area, MAX_AREA_CHARS),
                        severity,
                        _clean(reporter, MAX_REPORTER_CHARS),
                        reporter_user_id,
                        _clean(page_url, MAX_AREA_CHARS),
                        _clean(user_agent, MAX_USER_AGENT_CHARS),
                        STATUS_OPEN,
                        stamp,
                        stamp,
                    ),
                )
                return int(cursor.lastrowid)
        except BugReportRateLimitError:
            # Nothing was written, so give the budget back by rolling the
            # transaction the limiter opened. Raised past the sqlite handler
            # below, which would otherwise report it as an unreachable share.
            raise
        except sqlite3.Error as exc:
            raise BugReportStoreError(self._unreachable(exc)) from exc

    def _charge_submission(self, conn: sqlite3.Connection, client_key: str) -> None:
        """Spend one of this caller's allowance, or refuse the submission.

        Runs inside the caller's BEGIN IMMEDIATE, so the sweep, the count and the
        charge are serialized against every other submission.
        """

        now = time.time()
        cutoff = now - SUBMISSION_WINDOW_SECONDS
        scope = _limiter_scope(client_key)

        # Rows outside every live window are disposable; sweep them while the
        # writer slot is already held rather than in a job of their own.
        conn.execute("DELETE FROM submission_attempts WHERE filed_at < ?", (cutoff,))
        # The sweep above only reaches expired rows, so a fast enough burst
        # outruns it inside the window. Cap what is left, oldest first.
        overflow = conn.execute("SELECT COUNT(*) FROM submission_attempts").fetchone()[0]
        if overflow > SUBMISSION_ATTEMPT_ROW_CAP:
            conn.execute(
                "DELETE FROM submission_attempts WHERE rowid IN ("
                "  SELECT rowid FROM submission_attempts ORDER BY filed_at LIMIT ?"
                ")",
                (overflow - SUBMISSION_ATTEMPT_ROW_CAP,),
            )

        recent = conn.execute(
            "SELECT COUNT(*), MIN(filed_at) FROM submission_attempts "
            "WHERE scope = ? AND filed_at >= ?",
            (scope, cutoff),
        ).fetchone()
        if recent[0] >= SUBMISSION_LIMIT_PER_WINDOW:
            # The allowance frees up when the oldest attempt in the window ages
            # out, so that is what the reporter is told to wait for.
            conn.rollback()
            raise BugReportRateLimitError(recent[1] + SUBMISSION_WINDOW_SECONDS - now)

        conn.execute(
            "INSERT INTO submission_attempts (scope, filed_at) VALUES (?, ?)", (scope, now)
        )

    def set_status(
        self,
        report_id: int,
        status: str,
        *,
        actor: str = "",
        note: str = "",
        expected_revision: int | None = None,
    ) -> dict:
        """Resolve a report, or reopen one, and return it as it now stands.

        Reopening clears who resolved it and when, so the columns never describe
        a resolution that no longer holds. The note is kept either way: the
        reason a report was reopened is worth as much as the reason it was
        closed.

        ``expected_revision`` is the row's revision as the caller last saw it.
        Give it and the change is refused with BugReportConflictError if the
        stored row has moved on since; omit it and the change is applied
        unconditionally.

        A revision rather than the status it replaced, because a status cannot
        detect the case that matters most: A opens the page, B resolves the
        report and then reopens it, and A's stale click now finds the status it
        expected and overwrites B's reopening. The revision only ever goes up,
        so any change at all between the draw and the click is caught.

        BEGIN IMMEDIATE is not an alternative to this -- it orders the two
        writes but still lets the later one land on top of the earlier one.
        """

        status = _clean(status, 32).lower()
        if status not in STATUSES:
            raise BugReportValidationError(
                f"Status must be one of: {', '.join(STATUSES)}."
            )

        self.ensure_schema()
        stamp = _now()
        note = _clean(note, MAX_NOTE_CHARS)
        actor = _clean(actor, MAX_REPORTER_CHARS)
        try:
            with self._connect() as conn:
                # BEGIN IMMEDIATE so the read and the write are one step. It
                # orders concurrent changes but does not by itself stop a stale
                # one landing -- expected_revision above is what does that.
                conn.execute("BEGIN IMMEDIATE")
                existing = conn.execute(
                    "SELECT id, status, revision FROM bug_reports WHERE id = ?", (report_id,)
                ).fetchone()
                if existing is None:
                    conn.rollback()
                    raise BugReportValidationError(f"No bug report has id {report_id}.")
                if expected_revision is not None and existing["revision"] != expected_revision:
                    conn.rollback()
                    raise BugReportConflictError(
                        report_id,
                        expected_revision,
                        int(existing["revision"]),
                        existing["status"],
                    )
                if status == STATUS_RESOLVED:
                    conn.execute(
                        """
                        UPDATE bug_reports
                           SET status = ?, updated_at = ?, resolved_at = ?,
                               resolved_by = ?, resolution_note = ?,
                               revision = revision + 1
                         WHERE id = ?
                        """,
                        (STATUS_RESOLVED, stamp, stamp, actor, note, report_id),
                    )
                else:
                    conn.execute(
                        """
                        UPDATE bug_reports
                           SET status = ?, updated_at = ?, resolved_at = NULL,
                               resolved_by = '', resolution_note = ?,
                               revision = revision + 1
                         WHERE id = ?
                        """,
                        (STATUS_OPEN, stamp, note, report_id),
                    )
                row = conn.execute(
                    "SELECT * FROM bug_reports WHERE id = ?", (report_id,)
                ).fetchone()
        except (BugReportConflictError, BugReportValidationError):
            # Raised past the sqlite handler below, which would otherwise report
            # a refused change as an unreachable share.
            raise
        except sqlite3.Error as exc:
            raise BugReportStoreError(self._unreachable(exc)) from exc
        return dict(row)

    def delete(self, report_id: int) -> None:
        """Remove a report outright -- for duplicates and accidental submissions."""

        self.ensure_schema()
        try:
            with self._connect() as conn:
                cursor = conn.execute("DELETE FROM bug_reports WHERE id = ?", (report_id,))
                if not cursor.rowcount:
                    raise BugReportValidationError(f"No bug report has id {report_id}.")
        except sqlite3.Error as exc:
            raise BugReportStoreError(self._unreachable(exc)) from exc

    # -- reads ------------------------------------------------------------

    def list_reports(
        self,
        *,
        status: str = "all",
        search: str = "",
        limit: int = LIST_LIMIT,
        cursor: str | None = None,
    ) -> dict:
        """One page of the reports matching a status filter and a free-text search.

        Open reports sort above resolved ones whichever filter is in force, and
        within a status the more urgent severity leads -- so the default view is
        already the queue to work through rather than a list to re-sort.

        Returns the page together with how many match in total, whether more
        remain, and the cursor that continues it. A triage list that silently
        stopped at its limit would drop exactly the oldest reports -- the ones
        most likely to have been forgotten, in a feature whose point is that
        nothing filed gets lost.

        Paging is by cursor rather than offset because this queue is being
        worked while it is being read. An offset says "skip 200 rows", so when
        another administrator resolves something on the first page, every row
        after it shifts forward and the second page steps straight over a report
        nobody has seen -- and the count can even report that there is no more
        to fetch. The cursor names the last row's position in the ordering
        instead, which nothing happening elsewhere in the table can move.
        """

        status = _clean(status, 32).lower() or "all"
        if status not in (*STATUSES, "all"):
            raise BugReportValidationError(
                f"Status filter must be one of: all, {', '.join(STATUSES)}."
            )

        self.ensure_schema()
        # The filter is what the administrator chose; the cursor is only where
        # this page resumes. Kept apart so the total can be counted over the
        # filter alone -- folded together, it would shrink with every page.
        clauses: list[str] = []
        params: list[object] = []
        if status != "all":
            clauses.append("status = ?")
            params.append(status)
        needle = _clean(search, MAX_TITLE_CHARS)
        if needle:
            # LIKE with an escape, so a search for "100%" finds that and not
            # every report. Case-insensitive over the fields a reader would
            # search by name or symptom.
            pattern = f"%{needle.replace('!', '!!').replace('%', '!%').replace('_', '!_')}%"
            clauses.append(
                "(title LIKE ? ESCAPE '!' COLLATE NOCASE"
                " OR description LIKE ? ESCAPE '!' COLLATE NOCASE"
                " OR area LIKE ? ESCAPE '!' COLLATE NOCASE"
                " OR reporter LIKE ? ESCAPE '!' COLLATE NOCASE)"
            )
            params.extend([pattern] * 4)

        count_where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        count_params = tuple(params)

        # Everything strictly after the cursor's position in the ordering. The
        # comparison walks the same key ORDER BY sorts on, most significant
        # first, so it lands exactly where the previous page stopped -- no
        # matter what has been added or removed in between.
        page_clauses = list(clauses)
        page_params = list(params)
        if cursor:
            status_rank, severity_rank, created_at, last_id = _decode_cursor(cursor)
            page_clauses.append(
                f"("
                f"  {_STATUS_RANK_SQL} > ?"
                f"  OR ({_STATUS_RANK_SQL} = ? AND {_SEVERITY_RANK_SQL} > ?)"
                f"  OR ({_STATUS_RANK_SQL} = ? AND {_SEVERITY_RANK_SQL} = ? AND created_at < ?)"
                f"  OR ({_STATUS_RANK_SQL} = ? AND {_SEVERITY_RANK_SQL} = ? AND created_at = ?"
                f"      AND id < ?)"
                f")"
            )
            page_params.extend(
                [
                    status_rank,
                    status_rank, severity_rank,
                    status_rank, severity_rank, created_at,
                    status_rank, severity_rank, created_at, last_id,
                ]
            )
        page_where = f"WHERE {' AND '.join(page_clauses)}" if page_clauses else ""
        page_size = max(1, min(int(limit), MAX_LIST_LIMIT))
        try:
            with self._connect() as conn:
                total = conn.execute(
                    f"SELECT COUNT(*) FROM bug_reports {count_where}", count_params
                ).fetchone()[0]
                rows = conn.execute(
                    f"""
                    SELECT * FROM bug_reports
                    {page_where}
                    ORDER BY {_ORDER_BY_SQL}
                    LIMIT ?
                    """,
                    (*page_params, page_size + 1),
                ).fetchall()
        except sqlite3.Error as exc:
            raise BugReportStoreError(self._unreachable(exc)) from exc

        # One row beyond the page was fetched purely to answer "is there more?"
        # without a second query, and without inferring it from a count that
        # another administrator may already have changed.
        has_more = len(rows) > page_size
        reports = [dict(row) for row in rows[:page_size]]
        return {
            "reports": reports,
            "total": int(total),
            "has_more": has_more,
            "next_cursor": _encode_cursor(reports[-1]) if has_more and reports else None,
        }

    def summary(self) -> dict:
        """The counts the dashboard's tiles show, in one pass over the table."""

        self.ensure_schema()
        try:
            with self._connect() as conn:
                return _summarise(conn)
        except sqlite3.Error as exc:
            raise BugReportStoreError(self._unreachable(exc)) from exc

    # -- status -----------------------------------------------------------

    def _connect_readonly(self) -> sqlite3.Connection:
        """A connection that cannot write, for the status check below.

        Read-only rather than the ordinary connection for two reasons. Opening a
        path read-write creates it, and a check that makes the file it was asked
        to look for cannot report that it was missing. And the share may be
        mounted read-only on the machine asking -- which is exactly the machine
        whose administrator wants to know where things stand before finding out
        by having somebody's report refused.
        """

        uri = self._readonly_uri()
        if uri is not None:
            try:
                conn = sqlite3.connect(uri, uri=True, timeout=DB_BUSY_TIMEOUT_SECONDS)
            except sqlite3.Error:
                # Read-only is worth having but not worth failing over: the file
                # is known to exist by the time this is called, and nothing here
                # writes to it. So a URI sqlite will not take falls back to the
                # ordinary connection, which answers the question either way,
                # rather than reporting a reachable database as unreachable.
                uri = None
        if uri is None:
            try:
                conn = sqlite3.connect(self.path, timeout=DB_BUSY_TIMEOUT_SECONDS)
            except sqlite3.Error as exc:
                raise BugReportStoreError(self._unreachable(exc)) from exc
        conn.row_factory = sqlite3.Row
        return conn

    def _readonly_uri(self) -> str | None:
        """``self.path`` as a read-only sqlite URI, or None if it cannot be one."""

        try:
            uri = self.path.resolve().as_uri()
        except ValueError:
            # as_uri() refuses a path that is not absolute on this platform --
            # the Windows default read on a POSIX runner, for one.
            return None
        return f"{_sqlite_file_uri(uri)}?mode=ro"

    def describe(self) -> dict:
        """Where the database stands right now, creating nothing.

        The one method here that does not call ensure_schema first. Every other
        method exists to serve a report, and making the file in order to serve
        one is the right answer. This exists to answer "is it there, and is it
        whole" -- for the CLI that sets a deployment up, and for anyone who has
        looked on the share and not found it -- and a check that creates what it
        was sent to look for cannot answer that.

        A missing file is not an error: it is the state before anybody has filed
        anything, and the honest report of it. BugReportStoreError is raised only
        when a file that is there cannot be read, which is the case an
        administrator has to act on.
        """

        info: dict = {
            "path": str(self.path),
            "exists": self.path.is_file(),
            "parent_exists": self.path.parent.is_dir(),
            "size_bytes": None,
            "tables": [],
            "missing_tables": list(SCHEMA_TABLES),
            # Left empty when the tables are missing whole, for the same reason
            # the loop below skips them: missing_tables already says everything
            # is absent, and every column listed under it would bury that.
            "missing_columns": [],
            "schema_ready": False,
            "summary": None,
        }
        if not info["exists"]:
            return info

        try:
            info["size_bytes"] = self.path.stat().st_size
        except OSError as exc:
            raise BugReportStoreError(self._unreachable(exc)) from exc

        conn = self._connect_readonly()
        try:
            # sqlite_sequence and friends are excluded the same way
            # schema_service excludes them: AUTOINCREMENT creates one, and an
            # administrator reading this wants GREMLIN's tables, not sqlite's.
            tables = sorted(
                row["name"]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
                ).fetchall()
            )
            info["tables"] = tables
            info["missing_tables"] = [name for name in SCHEMA_TABLES if name not in tables]

            # Every column the schema declares, not only the ones ensure_schema
            # migrates. A table that is present but has had a column taken off
            # it opens, lists, and counts exactly like a healthy one, and then
            # refuses the first report filed against it -- with a message about
            # the drive, because that is what a failed write looks like from the
            # outside. Reported here instead, while somebody is asking.
            missing_columns: list[str] = []
            present: dict[str, set[str]] = {}
            for table in SCHEMA_TABLES:
                if table not in tables:
                    # Already reported whole in missing_tables; listing each of
                    # its columns underneath would bury that.
                    continue
                present[table] = {
                    row["name"]
                    for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
                }
                missing_columns += [
                    f"{table}.{name}"
                    for name in _expected_columns()[table]
                    if name not in present[table]
                ]
            info["missing_columns"] = missing_columns
            info["schema_ready"] = not info["missing_tables"] and not info["missing_columns"]

            # Counted last, and only when the columns it reads are all there.
            # Otherwise the count is what fails, and "unreadable" is what gets
            # reported -- hiding the far more useful answer, which is exactly
            # which column has gone.
            if all(name in present.get("bug_reports", ()) for name in _SUMMARY_COLUMNS):
                info["summary"] = _summarise(conn)
        except sqlite3.Error as exc:
            raise BugReportStoreError(self._unreachable(exc)) from exc
        finally:
            conn.close()
        return info
