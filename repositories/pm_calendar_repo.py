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
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

# Default location the app looks for the PM calendar database, matching the
# convention DEFAULT_DB_PATH uses for GREMLIN.db (services/life_data_service.py):
# a fixed path on the deployment machine, overridable by an environment
# variable so a dev/test run can point somewhere else entirely.
DEFAULT_PM_CALENDAR_DB_PATH = Path(r"C:\Users\billy.trinh\OneDrive - S & C Electric Company\Documents\GREMLINVM\PM_Calendar_local.db")

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

# Indexes speed up the lookups the calendar page actually does: "PMs for
# these assets" and "PMs due in this date range." They cost nothing to have
# and nothing to maintain -- SQLite keeps them up to date automatically.
_CREATE_INDEX_STATEMENTS = (
    "CREATE INDEX IF NOT EXISTS idx_pm_task_asset_id ON pm_task(asset_id)",
    "CREATE INDEX IF NOT EXISTS idx_pm_task_due_date ON pm_task(due_date)",
    "CREATE INDEX IF NOT EXISTS idx_pm_task_asset_due ON pm_task(asset_id, due_date)",
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


class PmCalendarRepository:
    """Reads and writes the pm_task table in its own SQLite database file."""

    def __init__(self, db_path: str | Path = DEFAULT_PM_CALENDAR_DB_PATH) -> None:
        self.db_path = Path(db_path)

    # ------------------------------------------------------------------
    # Connections
    # ------------------------------------------------------------------
    def connect(self) -> sqlite3.Connection:
        # SQLite creates the database *file* on first open, but not missing
        # parent folders -- create the folder ourselves so a fresh machine
        # (or a path override that doesn't exist yet) doesn't fail here.
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        conn = sqlite3.connect(self.db_path, timeout=DB_WRITE_TIMEOUT_SECONDS)
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout = {DB_WRITE_TIMEOUT_SECONDS * 1000}")
        return conn

    @contextmanager
    def write_connection(self) -> Iterator[sqlite3.Connection]:
        """A connection wrapped in one transaction: all writes land, or none do.

        If anything raises inside the `with` block, everything written so far
        in that block is rolled back rather than left half-applied.
        """

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
            for statement in _CREATE_INDEX_STATEMENTS:
                conn.execute(statement)

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

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------
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

        conn = self.connect()
        try:
            rows = conn.execute(sql, params).fetchall()
        finally:
            conn.close()
        return [_row_to_dict(row) for row in rows]

    def asset_options(self) -> list[dict[str, Any]]:
        """Distinct assets that currently have at least one stored PM task."""

        sql = (
            "SELECT DISTINCT asset_id, asset_number, asset_name "
            "FROM pm_task ORDER BY asset_name"
        )
        conn = self.connect()
        try:
            rows = conn.execute(sql).fetchall()
        finally:
            conn.close()
        return [_row_to_dict(row) for row in rows]


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    """Turn a sqlite3.Row into a plain dict (what jsonify() etc. expect)."""

    return {key: row[key] for key in row.keys()}