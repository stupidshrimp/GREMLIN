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

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

# The one location for bugreports.db: the GREMLIN Program folder on the shared
# drive, alongside the other global databases. Override with the
# GREMLIN_BUGS_DB_PATH environment variable -- which is what the tests do, and
# what a deployment that maps the share to a different letter would set.
DEFAULT_BUG_DB_PATH = Path(
    r"Z:\FACIL\MAIN-ENG\901 Reliability Projects\901 Reliability Projects"
    r"\GREMLIN Program\GREMLIN Global DB\bugreports.db"
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
# searches server-side, so this bounds one response rather than the file.
LIST_LIMIT = 500


class BugReportStoreError(RuntimeError):
    """The reports database could not be reached or read."""


class BugReportValidationError(ValueError):
    """The submitted report is missing something, or says something impossible."""


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


class BugReportStore:
    """Every read and write of bugreports.db."""

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
                conn.execute(f"""
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
                        resolution_note TEXT NOT NULL DEFAULT ''
                    )
                """)
                # The dashboard's default view is "open, newest first", and its
                # counts group by status; both read this rather than the table.
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_bug_reports_status_created "
                    "ON bug_reports(status, created_at DESC)"
                )
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
    ) -> int:
        """Store one report and return its id.

        Only the title and the description are required. Everything else is
        context that helps whoever picks the report up, and a report filed
        without it is still worth having -- the alternative is a form somebody
        abandons, which stores nothing at all.
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
        except sqlite3.Error as exc:
            raise BugReportStoreError(self._unreachable(exc)) from exc

    def set_status(
        self,
        report_id: int,
        status: str,
        *,
        actor: str = "",
        note: str = "",
    ) -> dict:
        """Resolve a report, or reopen one, and return it as it now stands.

        Reopening clears who resolved it and when, so the columns never describe
        a resolution that no longer holds. The note is kept either way: the
        reason a report was reopened is worth as much as the reason it was
        closed.
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
                # BEGIN IMMEDIATE so the row cannot be resolved by two
                # administrators at once, each having read it as open.
                conn.execute("BEGIN IMMEDIATE")
                existing = conn.execute(
                    "SELECT id FROM bug_reports WHERE id = ?", (report_id,)
                ).fetchone()
                if existing is None:
                    conn.rollback()
                    raise BugReportValidationError(f"No bug report has id {report_id}.")
                if status == STATUS_RESOLVED:
                    conn.execute(
                        """
                        UPDATE bug_reports
                           SET status = ?, updated_at = ?, resolved_at = ?,
                               resolved_by = ?, resolution_note = ?
                         WHERE id = ?
                        """,
                        (STATUS_RESOLVED, stamp, stamp, actor, note, report_id),
                    )
                else:
                    conn.execute(
                        """
                        UPDATE bug_reports
                           SET status = ?, updated_at = ?, resolved_at = NULL,
                               resolved_by = '', resolution_note = ?
                         WHERE id = ?
                        """,
                        (STATUS_OPEN, stamp, note, report_id),
                    )
                row = conn.execute(
                    "SELECT * FROM bug_reports WHERE id = ?", (report_id,)
                ).fetchone()
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

    def list_reports(self, *, status: str = "all", search: str = "", limit: int = LIST_LIMIT) -> list[dict]:
        """Reports matching a status filter and a free-text search, newest first.

        Open reports sort above resolved ones whichever filter is in force, and
        within a status the more urgent severity leads -- so the default view is
        already the queue to work through rather than a list to re-sort.
        """

        status = _clean(status, 32).lower() or "all"
        if status not in (*STATUSES, "all"):
            raise BugReportValidationError(
                f"Status filter must be one of: all, {', '.join(STATUSES)}."
            )

        self.ensure_schema()
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

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    f"""
                    SELECT * FROM bug_reports
                    {where}
                    ORDER BY CASE status WHEN '{STATUS_OPEN}' THEN 0 ELSE 1 END,
                             CASE severity
                                 {" ".join(
                                     f"WHEN '{name}' THEN {rank}" for name, rank in SEVERITY_RANK.items()
                                 )}
                                 ELSE {len(SEVERITIES)}
                             END,
                             created_at DESC,
                             id DESC
                    LIMIT ?
                    """,
                    (*params, max(1, int(limit))),
                ).fetchall()
        except sqlite3.Error as exc:
            raise BugReportStoreError(self._unreachable(exc)) from exc
        return [dict(row) for row in rows]

    def summary(self) -> dict:
        """The counts the dashboard's tiles show, in one pass over the table."""

        self.ensure_schema()
        try:
            with self._connect() as conn:
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
        except sqlite3.Error as exc:
            raise BugReportStoreError(self._unreachable(exc)) from exc
        return {
            "total": int(counts["total"] or 0),
            "open": int(counts["open_count"] or 0),
            "resolved": int(counts["resolved_count"] or 0),
            "open_blocking": int(counts["open_blocking"] or 0),
            "latest_report": counts["latest_report"],
        }
