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
            # Existing GREMLIN databases constrain import_batch.status to
            # STARTED/COMPLETED/FAILED/CANCELLED, so use the legacy-compatible
            # in-progress value instead of a newer synonym.
            "status": "STARTED",
            "notes": notes,
        }
        with self.write_connection() as conn:
            row_id = self._insert_schema_aware(conn, "import_batch", desired, pk_column="import_batch_id")
        return row_id

    def complete_batch(self, batch_id: int, *, status: str, raw_row_count: int) -> None:
        with self.write_connection() as conn:
            columns = self._column_names(conn, "import_batch")
            # Only update columns that actually exist, so a minimal legacy
            # import_batch (e.g. without a status column) does not abort the sync
            # after the raw rows were already imported.
            sets: list[str] = []
            params: list[Any] = []
            for column, value in (
                ("status", status),
                ("import_completed_at", _utc_now_text()),
                ("raw_row_count", raw_row_count),
            ):
                if column in columns:
                    sets.append(f"{column} = ?")
                    params.append(value)
            if not sets:
                return
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
                match = existing.get(task_id) if task_id else None
                # Only merge legacy asset identity into a true one-to-one update.
                # Duplicate task IDs are intentionally preserved as historical rows;
                # a new current API row must keep its own incoming asset fields rather
                # than inheriting whichever duplicate the index happened to see first.
                merge_existing = match and len(match["ids"]) == 1
                effective_record = _merge_preserved_fields(match.get("raw") if merge_existing else None, record)
                raw_text = json.dumps(effective_record, ensure_ascii=False, sort_keys=True)
                raw_hash = hashlib.sha256(raw_text.encode("utf-8", errors="replace")).hexdigest()

                if match is None:
                    desired = self._raw_record_values(batch_id, task_id, effective_record, raw_text, raw_hash)
                    if "source_row_number" in columns:
                        desired.setdefault("source_row_number", next_row_number)
                        next_row_number += 1
                    self._insert_schema_aware(conn, "raw_cmms_record", desired, pk_column="raw_record_id")
                    inserted += 1
                elif len(match["ids"]) > 1:
                    # A duplicated natural key means the existing rows are not a
                    # safe one-to-one representation of the Limble task. Updating
                    # every duplicate would overwrite historical/legacy rows with
                    # one current payload, which looks like data loss to users.
                    # Preserve those rows; if this exact payload is already among
                    # them, treat it as unchanged, otherwise append the current
                    # API record as a new raw row for later cleanup/deduping.
                    if raw_hash in match.get("hashes", set()):
                        skipped += 1
                    else:
                        desired = self._raw_record_values(batch_id, task_id, effective_record, raw_text, raw_hash)
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
        # Legacy tables may lack an explicit raw_record_id; fall back to rowid,
        # the same way the application mapper does.
        pk_expr = "raw_record_id" if "raw_record_id" in columns else "rowid"
        for raw_record_id in ids:
            conn.execute(
                f"UPDATE raw_cmms_record SET {assignment} WHERE {pk_expr} = ?",
                [*params, raw_record_id],
            )

    def _index_existing(self, conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
        """Map Limble taskID -> {ids: [...], hash: <hash of first row>}."""

        columns = self._column_names(conn, "raw_cmms_record")
        has_source_id = "source_record_id" in columns
        # Legacy tables may only have raw_json (no raw_record_id); fall back to
        # rowid, aliased so downstream row access is uniform.
        pk_expr = "raw_record_id" if "raw_record_id" in columns else "rowid AS raw_record_id"
        select = f"SELECT {pk_expr}, raw_json" + (", source_record_id" if has_source_id else "") + " FROM raw_cmms_record"
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
            # Recompute the hash exactly the way upsert does so equality is
            # meaningful even for legacy rows that stored a different hash.
            computed_hash = hashlib.sha256(
                json.dumps(parsed, ensure_ascii=False, sort_keys=True).encode("utf-8", errors="replace")
            ).hexdigest()
            entry = index.get(key)
            if entry is None:
                index[key] = {
                    "ids": [row["raw_record_id"]],
                    "hash": computed_hash,
                    "hashes": {computed_hash},
                    "raw": parsed,
                }
            else:
                entry["ids"].append(row["raw_record_id"])
                entry.setdefault("hashes", set()).add(computed_hash)
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
        if not column_names:
            # A table with only its integer PK has nothing to set; an empty
            # column/value list is invalid SQL, so insert a default row instead.
            conn.execute(f"INSERT INTO {table} DEFAULT VALUES")
        else:
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


_PINNED_ASSET_IDENTITY_FIELDS = (
    # Legacy/import-enriched asset identity fields are what the analysis screens
    # group by. Limble API sync payloads may use a narrower numeric assetID or
    # omit display fields, so do not let a routine refresh move historical work
    # orders out from under the asset the user already dispositioned/analyzed:
    # these stay pinned to the existing value even when the incoming payload
    # carries its own (narrower) asset identity.
    "Asset Number",
    "Asset Name",
    "Immediate Parent Asset ID",
    "Immediate Parent Asset Name",
    "Root Asset ID",
    "Root Asset Name",
    "WO Asset Level",
    "Asset Has Children",
)

# Derived ISO date fields mirror a source Unix field that IngestionService.transform
# turns into the ``*_Final``/``*DateTime`` values the mapper reads. They are computed,
# not curated, so they must always track their source rather than being preserved on
# their own: when a refresh carries the source field but it has been cleared (e.g. a
# completed task is reopened and ``dateCompleted`` drops to 0), transform emits no
# derived value, and a stale derived value left over from the previous payload would
# keep the row looking completed. Each derived field is mapped to the source key(s)
# whose presence in the incoming payload means "this date was refreshed".
_SOURCE_LINKED_DATE_FIELDS = {
    "completedDate_Final": ("dateCompleted",),
    "completedDateTime": ("dateCompleted",),
    "createdDate_Final": ("createdDate",),
    "createdDateTime": ("createdDate",),
    "startDate_Final": ("startDate",),
    "startDateTime": ("startDate",),
    "dueDate_Final": ("dueDate", "due"),
}


def _merge_preserved_fields(existing: dict[str, Any] | None, incoming: dict[str, Any]) -> dict[str, Any]:
    """Merge an incoming Limble payload onto an existing row without dropping data.

    A Limble ``/tasks`` refresh routinely carries a *narrower* payload than the row
    already stored: the list endpoint omits fields that were present when the row
    was first imported (completion notes, completed/created dates, downtime, and
    other curated columns). The previous behaviour replaced the row with only the
    incoming keys, so every field the API left out was silently blanked. On a
    reliability database those omitted fields are the analysis (completed dates and
    downtime especially), so a single sync gutted almost every row even though the
    row count never changed.

    To make the sync additive rather than destructive we start from the existing
    payload and overlay the incoming fields. A sync can therefore add new fields and
    update fields the API actually returned, but it can never delete curated data
    the API simply did not include. An incoming empty value (``None``/``""``) never
    overwrites a non-empty stored value, and the asset-identity fields stay pinned to
    their existing values.
    """

    if not existing:
        return incoming
    merged = dict(existing)
    for key, value in incoming.items():
        # Only overlay values that carry data (or brand-new keys); a narrower
        # refresh must not blank a field the stored row already populated.
        if value in (None, "") and existing.get(key) not in (None, ""):
            continue
        merged[key] = value
    # Derived date fields must follow their source. If the refresh carried the
    # source date field but did not produce the derived value, the source was
    # cleared (e.g. a reopened task): drop the stale derived value instead of
    # preserving it. A refresh that simply omits the source (narrow API payload)
    # leaves the derived value untouched above.
    for derived, sources in _SOURCE_LINKED_DATE_FIELDS.items():
        if derived in incoming:
            continue
        if any(source in incoming for source in sources):
            merged.pop(derived, None)
    for key in _PINNED_ASSET_IDENTITY_FIELDS:
        value = existing.get(key)
        if value not in (None, ""):
            merged[key] = value
    return merged


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
