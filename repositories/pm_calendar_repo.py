"""Repository for PM Calendar data.

Stores the preventive-maintenance tasks pulled from Limble into their own
SQLite database file (kept separate from GREMLIN.db, matching how
accesscontrol.db and the bug-reports database are also their own files).

This file only knows how to create the table and read/write plain
dictionaries into it -- it has no idea what Limble is. That split makes it
possible to test this file on its own with made-up rows, with nothing
Limble-related involved at all.
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

# Default location for the PM calendar database: beside GREMLIN.db, the same
# way accesscontrol.db sits beside it (see app.py). A deployment that already
# points GREMLIN_DB_PATH at its database therefore gets this file in the same
# folder with nothing else to configure, and app.py derives it from that
# override rather than from this constant. Set GREMLIN_PM_CALENDAR_DB_PATH to
# put it somewhere else entirely.
#
# This deliberately does not live in anyone's user profile. It used to point
# at one developer's OneDrive folder, which no other account can create or
# write to -- on every other machine that raised PermissionError from the
# mkdir in connect() below, during app startup, taking the whole app down
# rather than one page.
# The file name is named separately because app.py builds the real path by
# putting it beside the *configured* GREMLIN.db, rather than using the constant
# below. The constant is written out in full, in the same style as
# DEFAULT_DB_PATH, so the two read as the pair they are.
PM_CALENDAR_DB_FILENAME = "PM_Calendar_local.db"
DEFAULT_PM_CALENDAR_DB_PATH = Path(r"C:\GREMLIN\PM_Calendar_local.db")

# How long a write will wait for the database file to become free before
# giving up. SQLite only allows one writer at a time; this keeps a second
# write from failing instantly just because a sync was mid-write.
DB_WRITE_TIMEOUT_SECONDS = 30

# The table's shape. IF NOT EXISTS makes this safe to run every time the app
# starts -- it only actually creates anything the very first time.
_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS pm_task (
    task_id TEXT PRIMARY KEY,
    asset_id TEXT,
    asset_number TEXT,
    asset_name TEXT,
    task_name TEXT,
    status_raw TEXT,
    due_date TEXT,
    completed_date TEXT,
    is_completed INTEGER NOT NULL DEFAULT 0,
    synced_at TEXT NOT NULL DEFAULT (datetime('now'))
)
"""

# The asset hierarchy, as far as the calendar needs it: every asset that has
# PMs, plus every asset above one of those (a parent like 4002 can have no PMs
# of its own and still be the natural thing to pick). Its own table rather
# than a column on pm_task for exactly that reason -- a parent with no PMs has
# no pm_task row to hang a column on. Replaced wholesale on every sync, since
# /assets always comes back complete. Being a new table, CREATE TABLE IF NOT
# EXISTS is all an existing database needs; it stays empty until the next
# sync, and until then the calendar behaves exactly as it did before.
_CREATE_ASSET_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS pm_asset (
    asset_id TEXT PRIMARY KEY,
    asset_name TEXT,
    parent_asset_id TEXT,
    root_asset_id TEXT,
    building_asset_id TEXT,
    level INTEGER NOT NULL DEFAULT 0,
    has_children INTEGER NOT NULL DEFAULT 0
)
"""

# Columns added to pm_asset after it first shipped. CREATE TABLE IF NOT EXISTS
# leaves an existing table exactly as it is, and SQLite has no ADD COLUMN IF
# NOT EXISTS, so the current shape is read and anything missing is ALTERed in.
# The values arrive with the next sync, which rewrites every row anyway.
_ADDED_ASSET_COLUMNS: dict[str, str] = {
    "root_asset_id": "TEXT",
    "building_asset_id": "TEXT",
    "level": "INTEGER NOT NULL DEFAULT 0",
    "has_children": "INTEGER NOT NULL DEFAULT 0",
}

# Every column of pm_asset, in the order replace_assets writes them.
_ASSET_COLUMNS = (
    "asset_id",
    "asset_name",
    "parent_asset_id",
    "root_asset_id",
    "building_asset_id",
    "level",
    "has_children",
)

# Indexes speed up the lookups the calendar page actually does: "PMs for
# these assets" and "PMs due in this date range." They cost nothing to have
# and nothing to maintain -- SQLite keeps them up to date automatically.
_CREATE_INDEX_STATEMENTS = (
    "CREATE INDEX IF NOT EXISTS idx_pm_task_asset_id ON pm_task(asset_id)",
    "CREATE INDEX IF NOT EXISTS idx_pm_task_due_date ON pm_task(due_date)",
    "CREATE INDEX IF NOT EXISTS idx_pm_task_asset_due ON pm_task(asset_id, due_date)",
    "CREATE INDEX IF NOT EXISTS idx_pm_asset_parent ON pm_asset(parent_asset_id)",
)

# Every column in pm_task except the auto-filled synced_at. Used to keep the
# upsert statement and the "which keys does a row dict need" logic in one
# place, so the two can never quietly drift apart.
_TASK_COLUMNS = (
    "task_id",
    "asset_id",
    "asset_number",
    "asset_name",
    "task_name",
    "status_raw",
    "due_date",
    "completed_date",
    "is_completed",
)


class PmCalendarUnavailableError(RuntimeError):
    """The PM calendar database could not be opened.

    Raised instead of the underlying PermissionError/sqlite3.Error so a caller
    has one type to catch and a message that names the path it tried and the
    setting that moves it. The distinction that matters to a caller is that
    nothing is wrong with the code or the data -- the file is somewhere this
    account cannot reach -- so the page says so and the app keeps running.
    """


class PmCalendarRepository:
    """Reads and writes the pm_task table in its own SQLite database file."""

    def __init__(self, db_path: str | Path = DEFAULT_PM_CALENDAR_DB_PATH) -> None:
        self.db_path = Path(db_path)
        # fetch_assets() is read on every calendar request (to expand a picked
        # parent into its sub-assets) but only changes when replace_assets()
        # runs, once per sync -- so it's kept in memory between the two. The
        # generation stops a read that started before a replace from storing
        # the old tree after it.
        self._assets_lock = threading.Lock()
        self._assets_cache: list[dict[str, Any]] | None = None
        self._assets_generation = 0

    # ------------------------------------------------------------------
    # Connections
    # ------------------------------------------------------------------
    def connect(self) -> sqlite3.Connection:
        # SQLite creates the database *file* on first open, but not missing
        # parent folders -- create the folder ourselves so a fresh machine
        # (or a path override that doesn't exist yet) doesn't fail here.
        #
        # Both steps are wrapped. Creating the folder is the step that fails on
        # a path belonging to another machine's user profile, and it raises
        # PermissionError -- an OSError, not anything sqlite3 defines -- so
        # catching sqlite3.Error alone would let it through. Opening the file
        # then fails for the ordinary reasons: an unmapped drive, a read-only
        # share, a corrupt file.
        with self._reporting_failures():
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(self.db_path, timeout=DB_WRITE_TIMEOUT_SECONDS)
            try:
                conn.row_factory = sqlite3.Row
                # A connection-level setting: it configures how long this
                # connection waits for a lock and never reads the file, so a
                # corrupt database does *not* fail here. The first statement
                # that reads the header is PRAGMA journal_mode in
                # write_connection(), or the caller's own query on a read.
                conn.execute(f"PRAGMA busy_timeout = {DB_WRITE_TIMEOUT_SECONDS * 1000}")
            except BaseException:
                # Hand back a connection only when it is fully set up. Anything
                # that fails in between has to close what was opened, or the
                # handle is left to the garbage collector -- and the retry this
                # error path invites means one leak per attempt.
                conn.close()
                raise
        return conn

    def _unreachable(self, exc: Exception) -> str:
        return (
            f"The PM calendar database at {self.db_path} could not be opened. "
            f"Check that the folder exists, that the account GREMLIN runs as can "
            f"write to it, and that the file is a valid database -- or set "
            f"GREMLIN_PM_CALENDAR_DB_PATH to another location. Details: {exc}"
        )

    @contextmanager
    def _reporting_failures(self) -> Iterator[None]:
        """Report any failure to reach or use the database as one error type.

        Wrapping the open alone is not enough, because sqlite3.connect() is
        lazy: it does not read the file, so it succeeds against a corrupt
        database or one on a read-only share and the failure surfaces later --
        on the first PRAGMA, on BEGIN IMMEDIATE, or on a statement. Those are
        exactly the cases an operator most needs explained, and left untranslated
        they reach the endpoints as raw sqlite3.Error, which those handlers do
        not catch, so the page shows a 500 instead of the path and the setting.

        Every public method routes through here for that reason, rather than
        each one guarding the single call it happens to make.
        """

        try:
            yield
        except (OSError, sqlite3.Error) as exc:
            raise PmCalendarUnavailableError(self._unreachable(exc)) from exc

    @contextmanager
    def write_connection(self) -> Iterator[sqlite3.Connection]:
        """A connection wrapped in one transaction: all writes land, or none do.

        If anything raises inside the `with` block, everything written so far
        in that block is rolled back rather than left half-applied.
        """

        with self._reporting_failures():
            conn = self.connect()
            try:
                # Rollback-journal mode rather than WAL: safe on a shared network
                # drive, which is where this file is expected to live (same
                # reasoning as raw_repo.py's write_connection).
                conn.execute("PRAGMA journal_mode = DELETE")
                conn.execute("BEGIN IMMEDIATE")
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------
    def ensure_schema(self) -> None:
        """Create the pm_task table and its indexes if they don't exist yet.

        Call this once before using the repository (e.g. when the service
        that owns it starts up).
        """

        with self.write_connection() as conn:
            conn.execute(_CREATE_TABLE_SQL)
            conn.execute(_CREATE_ASSET_TABLE_SQL)
            self._add_missing_asset_columns(conn)
            for statement in _CREATE_INDEX_STATEMENTS:
                conn.execute(statement)

    @staticmethod
    def _add_missing_asset_columns(conn: sqlite3.Connection) -> None:
        """Add any pm_asset column this version expects that the file lacks.

        A no-op once they are all there, so it costs one PRAGMA per start.
        """

        existing = {row[1] for row in conn.execute("PRAGMA table_info(pm_asset)")}
        for column, declared_type in _ADDED_ASSET_COLUMNS.items():
            if column not in existing:
                conn.execute(f"ALTER TABLE pm_asset ADD COLUMN {column} {declared_type}")

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------
    def upsert_tasks(self, rows: list[dict[str, Any]]) -> dict[str, int]:
        """Insert new PM tasks and overwrite existing ones, keyed on task_id.

        Each dict in `rows` should have some or all of the keys in
        _TASK_COLUMNS; anything missing is stored as NULL. Returns
        {"upserted": <count>}.
        """

        if not rows:
            return {"upserted": 0}

        placeholders = ", ".join(f":{column}" for column in _TASK_COLUMNS)
        column_list = ", ".join(_TASK_COLUMNS)
        update_clause = ", ".join(
            f"{column} = excluded.{column}" for column in _TASK_COLUMNS if column != "task_id"
        )
        sql = (
            f"INSERT INTO pm_task ({column_list}) VALUES ({placeholders}) "
            f"ON CONFLICT(task_id) DO UPDATE SET {update_clause}"
        )

        with self.write_connection() as conn:
            for row in rows:
                # Fill in any column this row didn't provide with None, so the
                # named placeholders above always have a matching value.
                params = {column: row.get(column) for column in _TASK_COLUMNS}
                conn.execute(sql, params)

        return {"upserted": len(rows)}

    def replace_assets(self, rows: list[dict[str, Any]]) -> dict[str, int]:
        """Replace the whole asset hierarchy with `rows`, in one transaction.

        Each dict carries the columns in _ASSET_COLUMNS: the asset, its
        immediate parent (None at the top), the root of its branch, how many
        steps below that root it sits, and whether anything hangs under it --
        the same set of answers the Excel hierarchy sheet materialises, worked
        out once at sync time so no read has to walk the tree again. A row
        that leaves level, root or has_children out is stored with the column
        defaults (0, itself, 0), which is what a lone top-level asset looks
        like. Wholesale rather than upserted: an asset moved to
        another parent, or scrapped, in Limble must not leave its old link
        behind. Readers never see a half-written tree -- the delete and the
        inserts land together or not at all.
        """

        with self.write_connection() as conn:
            conn.execute("DELETE FROM pm_asset")
            placeholders = ", ".join(f":{column}" for column in _ASSET_COLUMNS)
            conn.executemany(
                f"INSERT OR REPLACE INTO pm_asset ({', '.join(_ASSET_COLUMNS)}) VALUES ({placeholders})",
                [
                    {
                        **{column: row.get(column) for column in _ASSET_COLUMNS},
                        "root_asset_id": row.get("root_asset_id") or row.get("asset_id"),
                        "level": row.get("level") or 0,
                        "has_children": row.get("has_children") or 0,
                    }
                    for row in rows
                ],
            )
        with self._assets_lock:
            self._assets_generation += 1
            self._assets_cache = None
        return {"assets": len(rows)}

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------
    def fetch_assets(self) -> list[dict[str, Any]]:
        """Every row of the asset hierarchy (empty until the first sync).

        Served from memory after the first read, until replace_assets() next
        runs -- the only thing that writes pm_asset. Each call gets its own
        copies, so a caller changing a row can't change the cached tree.
        """

        with self._assets_lock:
            if self._assets_cache is not None:
                return [dict(row) for row in self._assets_cache]
            generation = self._assets_generation

        with self._reporting_failures():
            conn = self.connect()
            try:
                rows = conn.execute(f"SELECT {', '.join(_ASSET_COLUMNS)} FROM pm_asset").fetchall()
            finally:
                conn.close()
        assets = [_row_to_dict(row) for row in rows]

        with self._assets_lock:
            if generation == self._assets_generation:
                self._assets_cache = assets
        return [dict(row) for row in assets]

    def fetch_tasks(
        self,
        asset_ids: list[str] | None = None,
        due_since: str | None = None,
        due_until: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return PM tasks, optionally filtered by asset and/or due-date range.

        `due_since`/`due_until` are inclusive "YYYY-MM-DD" (or full ISO
        datetime) strings. Leave either as None to leave that side open.
        `asset_ids` of None or [] means "every asset."
        """

        clauses: list[str] = []
        params: list[Any] = []

        if asset_ids:
            placeholders = ", ".join("?" for _ in asset_ids)
            clauses.append(f"asset_id IN ({placeholders})")
            params.extend(asset_ids)
        if due_since:
            clauses.append("due_date >= ?")
            params.append(due_since)
        if due_until:
            clauses.append("due_date <= ?")
            params.append(due_until)

        sql = "SELECT * FROM pm_task"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY due_date"

        with self._reporting_failures():
            conn = self.connect()
            try:
                rows = conn.execute(sql, params).fetchall()
            finally:
                conn.close()
        return [_row_to_dict(row) for row in rows]

    def fetch_last_completed(self, asset_ids: list[str]) -> dict[str, Any] | None:
        """The most recently completed PM among `asset_ids`, or None.

        "Most recent" is by completed_date -- when the work was actually
        signed off -- with due_date breaking a tie. Rows without a due_date
        are skipped: the calendar draws a PM on its due date, so one without
        has nowhere to be jumped to. One row, straight from the index on
        asset_id; this never reads more than the matching assets' rows.
        """

        if not asset_ids:
            return None
        placeholders = ", ".join("?" for _ in asset_ids)
        sql = (
            f"SELECT * FROM pm_task WHERE asset_id IN ({placeholders}) "
            "AND completed_date IS NOT NULL AND completed_date != '' "
            "AND due_date IS NOT NULL AND due_date != '' "
            "ORDER BY completed_date DESC, due_date DESC LIMIT 1"
        )
        with self._reporting_failures():
            conn = self.connect()
            try:
                row = conn.execute(sql, list(asset_ids)).fetchone()
            finally:
                conn.close()
        return _row_to_dict(row) if row else None

    def asset_options(self) -> list[dict[str, Any]]:
        """Distinct assets that currently have at least one stored PM task."""

        sql = (
            "SELECT DISTINCT asset_id, asset_number, asset_name "
            "FROM pm_task ORDER BY asset_name"
        )
        with self._reporting_failures():
            conn = self.connect()
            try:
                rows = conn.execute(sql).fetchall()
            finally:
                conn.close()
        return [_row_to_dict(row) for row in rows]


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    """Turn a sqlite3.Row into a plain dict (what jsonify() etc. expect)."""

    return {key: row[key] for key in row.keys()}