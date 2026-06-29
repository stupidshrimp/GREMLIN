"""Raw-data repository: writes immutable Limble payloads into GREMLIN.db.

This owns the two "raw import" tables that historically came from the legacy
Excel/CSV importer:

* ``import_batch``     - one row per sync run (provenance + status)
* ``raw_cmms_record``  - one row per Limble task, with the payload in ``raw_json``

It is deliberately schema-aware: on a fresh database it creates an API-first
shape, but on an existing GREMLIN.db it adapts to whatever columns are already
there (including legacy ``NOT NULL`` columns such as ``source_row_number``),
so a sync never fails because of a column the application no longer cares about.

Records are upserted keyed on the Limble ``taskID`` so re-running a sync updates
changed tasks in place instead of creating duplicates, and unchanged tasks are
skipped via a content hash. Keeping the ``raw_record_id`` stable on update means
``mapped_cmms_record.raw_record_id`` foreign keys stay valid.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

DB_WRITE_TIMEOUT_SECONDS = 30

# Canonical, API-first definitions used only when the tables do not already
# exist (fresh or test databases). Existing databases keep their own definition.
_MODERN_TABLE_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS import_batch (
        import_batch_id INTEGER PRIMARY KEY,
        source_system TEXT NOT NULL DEFAULT 'Limble',
        import_started_at TEXT NOT NULL DEFAULT (datetime('now')),
        import_completed_at TEXT,
        status TEXT NOT NULL DEFAULT 'PENDING',
        notes TEXT,
        raw_row_count INTEGER,
        source_file_name TEXT,
        source_file_path TEXT,
        source_file_hash TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS raw_cmms_record (
        raw_record_id INTEGER PRIMARY KEY,
        import_batch_id INTEGER NOT NULL,
        source_system TEXT NOT NULL DEFAULT 'Limble',
        source_record_id TEXT,
        raw_json TEXT NOT NULL,
        raw_content_hash TEXT,
        imported_at TEXT NOT NULL DEFAULT (datetime('now')),
        updated_at TEXT,
        source_row_number INTEGER,
        row_hash TEXT,
        source_record_uid TEXT,
        source_work_order TEXT,
        FOREIGN KEY (import_batch_id) REFERENCES import_batch(import_batch_id) ON UPDATE CASCADE ON DELETE RESTRICT
    )
    """,
)

# Best-effort helper indexes; created only when every referenced column exists.
_MODERN_INDEXES = (
    ("idx_import_batch_status", "import_batch", ("status",)),
    ("idx_raw_cmms_record_import_batch", "raw_cmms_record", ("import_batch_id",)),
    ("idx_raw_cmms_record_source", "raw_cmms_record", ("source_system", "source_record_id")),
    ("idx_raw_cmms_record_content_hash", "raw_cmms_record", ("raw_content_hash",)),
)


class RawRepository:
    """Stores immutable raw Limble payloads in GREMLIN.db."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)

    # ------------------------------------------------------------------
    # Connections
    # ------------------------------------------------------------------
    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=DB_WRITE_TIMEOUT_SECONDS)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(f"PRAGMA busy_timeout = {DB_WRITE_TIMEOUT_SECONDS * 1000}")
        return conn

    @contextmanager
    def write_connection(self) -> Iterator[sqlite3.Connection]:
        """Serialized write transaction (one SQLite writer at a time)."""

        conn = self.connect()
        try:
            # Rollback-journal mode: safe on a shared network drive where WAL is not.
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
        """Create the raw import tables when missing; never alter existing ones."""

        with self.write_connection() as conn:
            for statement in _MODERN_TABLE_STATEMENTS:
                conn.execute(statement)
            for index_name, table, columns in _MODERN_INDEXES:
                existing = self._column_names(conn, table)
                if existing and all(column in existing for column in columns):
                    conn.execute(
                        f"CREATE INDEX IF NOT EXISTS {index_name} ON {table} ({', '.join(columns)})"
                    )

    # ------------------------------------------------------------------
    # Batch lifecycle
    # ------------------------------------------------------------------
    def start_batch(self, notes: str | None = None) -> int:
        """Open an import_batch row and return its id."""

        now = _utc_now_text()
        desired = {
            "source_system": "Limble",
            "import_started_at": now,
            "status": "RUNNING",
            "notes": notes,
        }
        with self.write_connection() as conn:
            row_id = self._insert_schema_aware(conn, "import_batch", desired, pk_column="import_batch_id")
        return row_id

    def complete_batch(self, batch_id: int, *, status: str, raw_row_count: int) -> None:
        with self.write_connection() as conn:
            columns = self._column_names(conn, "import_batch")
            sets = ["status = ?"]
            params: list[Any] = [status]
            if "import_completed_at" in columns:
                sets.append("import_completed_at = ?")
                params.append(_utc_now_text())
            if "raw_row_count" in columns:
                sets.append("raw_row_count = ?")
                params.append(raw_row_count)
            params.append(batch_id)
            conn.execute(f"UPDATE import_batch SET {', '.join(sets)} WHERE import_batch_id = ?", params)

    # ------------------------------------------------------------------
    # Raw record upsert
    # ------------------------------------------------------------------
    def upsert_records(self, batch_id: int, records: list[dict[str, Any]]) -> dict[str, int]:
        """Insert new tasks, update changed tasks, skip unchanged ones.

        ``records`` is a list of raw_json dictionaries; each must contain a
        ``taskID`` used as the natural key. Returns counts of
        ``{"inserted", "updated", "skipped"}``.
        """

        inserted = updated = skipped = 0
        with self.write_connection() as conn:
            columns = self._column_names(conn, "raw_cmms_record")
            if not columns:
                raise RuntimeError("raw_cmms_record table is missing; call ensure_schema() first.")
            existing = self._index_existing(conn)
            next_row_number = self._next_source_row_number(conn, columns)

            for record in records:
                task_id = _task_key(record)
                raw_text = json.dumps(record, ensure_ascii=False, sort_keys=True)
                raw_hash = hashlib.sha256(raw_text.encode("utf-8", errors="replace")).hexdigest()
                match = existing.get(task_id) if task_id else None

                if match is None:
                    desired = self._raw_record_values(batch_id, task_id, record, raw_text, raw_hash)
                    if "source_row_number" in columns:
                        desired.setdefault("source_row_number", next_row_number)
                        next_row_number += 1
                    self._insert_schema_aware(conn, "raw_cmms_record", desired, pk_column="raw_record_id")
                    inserted += 1
                elif match["hash"] != raw_hash:
                    self._update_raw_record(conn, columns, match["ids"], batch_id, task_id, raw_text, raw_hash)
                    updated += 1
                else:
                    skipped += 1

        return {"inserted": inserted, "updated": updated, "skipped": skipped}

    def raw_record_count(self) -> int:
        with self.connect() as conn:
            if not self._column_names(conn, "raw_cmms_record"):
                return 0
            return int(conn.execute("SELECT COUNT(*) FROM raw_cmms_record").fetchone()[0] or 0)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _raw_record_values(
        self, batch_id: int, task_id: str | None, record: dict[str, Any], raw_text: str, raw_hash: str
    ) -> dict[str, Any]:
        now = _utc_now_text()
        work_order = record.get("workOrderNumber") or record.get("woNumber") or task_id
        return {
            "import_batch_id": batch_id,
            "source_system": "Limble",
            "source_record_id": task_id,
            "raw_json": raw_text,
            "raw_content_hash": raw_hash,
            "imported_at": now,
            "updated_at": now,
            # Legacy columns: harmless, sensible values so NOT NULL constraints pass.
            "row_hash": raw_hash,
            "source_record_uid": task_id,
            "source_work_order": work_order,
        }

    def _update_raw_record(
        self,
        conn: sqlite3.Connection,
        columns: set[str],
        ids: list[int],
        batch_id: int,
        task_id: str | None,
        raw_text: str,
        raw_hash: str,
    ) -> None:
        sets = ["raw_json = ?"]
        params: list[Any] = [raw_text]
        optional = {
            "raw_content_hash": raw_hash,
            "row_hash": raw_hash,
            "updated_at": _utc_now_text(),
            "import_batch_id": batch_id,
            "source_record_id": task_id,
        }
        for column, value in optional.items():
            if column in columns:
                sets.append(f"{column} = ?")
                params.append(value)
        assignment = ", ".join(sets)
        for raw_record_id in ids:
            conn.execute(
                f"UPDATE raw_cmms_record SET {assignment} WHERE raw_record_id = ?",
                [*params, raw_record_id],
            )

    def _index_existing(self, conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
        """Map Limble taskID -> {ids: [...], hash: <hash of first row>}."""

        has_source_id = "source_record_id" in self._column_names(conn, "raw_cmms_record")
        select = "SELECT raw_record_id, raw_json" + (", source_record_id" if has_source_id else "") + " FROM raw_cmms_record"
        index: dict[str, dict[str, Any]] = {}
        for row in conn.execute(select):
            try:
                parsed = json.loads(row["raw_json"] or "{}")
            except (json.JSONDecodeError, TypeError):
                parsed = {}
            key = _task_key(parsed)
            if not key and has_source_id and row["source_record_id"] not in (None, ""):
                key = str(row["source_record_id"]).strip()
            if not key:
                continue
            entry = index.get(key)
            if entry is None:
                # Recompute the hash exactly the way upsert does so equality is
                # meaningful even for legacy rows that stored a different hash.
                computed_hash = hashlib.sha256(
                    json.dumps(parsed, ensure_ascii=False, sort_keys=True).encode("utf-8", errors="replace")
                ).hexdigest()
                index[key] = {"ids": [row["raw_record_id"]], "hash": computed_hash}
            else:
                entry["ids"].append(row["raw_record_id"])
        return index

    def _next_source_row_number(self, conn: sqlite3.Connection, columns: set[str]) -> int:
        if "source_row_number" not in columns:
            return 1
        row = conn.execute("SELECT MAX(source_row_number) FROM raw_cmms_record").fetchone()
        return int(row[0] or 0) + 1

    def _insert_schema_aware(
        self, conn: sqlite3.Connection, table: str, desired: dict[str, Any], *, pk_column: str
    ) -> int:
        """INSERT a row, providing safe defaults for any NOT NULL column we missed."""

        info = self._table_info(conn, table)
        values: dict[str, Any] = {}
        for name, value in desired.items():
            if name in info:
                values[name] = value
        # Fill NOT NULL columns (no default, not the integer PK) we did not set.
        for name, meta in info.items():
            if name == pk_column or name in values:
                continue
            if meta["notnull"] and meta["dflt_value"] is None:
                values[name] = _zero_value_for(meta["type"])
        column_names = list(values)
        placeholders = ", ".join("?" for _ in column_names)
        conn.execute(
            f"INSERT INTO {table} ({', '.join(column_names)}) VALUES ({placeholders})",
            [values[name] for name in column_names],
        )
        return int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])

    @staticmethod
    def _table_info(conn: sqlite3.Connection, table: str) -> dict[str, dict[str, Any]]:
        info: dict[str, dict[str, Any]] = {}
        for row in conn.execute(f"PRAGMA table_info({table})"):
            info[row[1]] = {"type": (row[2] or "").upper(), "notnull": bool(row[3]), "dflt_value": row[4], "pk": row[5]}
        return info

    @staticmethod
    def _column_names(conn: sqlite3.Connection, table: str) -> set[str]:
        return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _zero_value_for(column_type: str) -> Any:
    column_type = (column_type or "").upper()
    if "INT" in column_type or "REAL" in column_type or "NUM" in column_type or "FLOA" in column_type or "DOUB" in column_type:
        return 0
    return ""


def _task_key(record: dict[str, Any]) -> str | None:
    for key in ("taskID", "task_id", "id"):
        value = record.get(key)
        if value not in (None, ""):
            return str(value).strip()
    return None


def _utc_now_text() -> str:
    # Matches SQLite's datetime('now') format (UTC, no timezone suffix).
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
