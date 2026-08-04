"""Read-only introspection of GREMLIN.db for the developer dashboard and CLI.

GREMLIN has no ``.sql`` schema file: the shape of ``GREMLIN.db`` is defined only
by Python, in :mod:`repositories.raw_repo` (the two raw import tables) and
:mod:`services.life_data_service` (the mapped/disposition/Weibull layers). Both
are deliberately *adaptive* -- ``ensure_schema`` only ever creates missing tables
and adds missing columns, and never drops or rewrites anything. A long-lived
GREMLIN.db therefore drifts away from the source:

* legacy columns the application stopped using are still physically present;
* tables from removed features (the ``availability_*`` set, which now lives only
  under ``Reference/``) are never cleaned up;
* a database predating a migration may still be missing newer columns.

So reading the source does not tell you what is actually in the file. This module
answers that question from the file itself, and reports the difference.

The reference schema is not hardcoded. It is built by pointing the real
``RawRepository``/``LifeDataService`` bootstrap at a throwaway database and
introspecting the result, so the drift report stays correct as the schema code
evolves instead of rotting against a duplicated table list.

Safety
------
Two independent mechanisms, because neither is sufficient alone.

``mode=ro`` URI connections stop any write to *the source database*. That
matters here: GREMLIN.db is expected to live on a shared drive, uses
rollback-journal mode, and serialises writers through ``BEGIN IMMEDIATE`` with a
30 second busy timeout (see :meth:`services.life_data_service.LifeDataService.write_connection`).
A dev-tools query that took the writer slot would stall every other GREMLIN user.
Read-only connections never acquire it.

``mode=ro`` does **not** stop SQLite from creating and writing *other* files.
``VACUUM INTO 'static/copy.db'`` and ``ATTACH DATABASE 'x.db' AS side`` both
succeed on a read-only connection, and either would let an unlocked user drop a
complete copy of GREMLIN.db somewhere Flask serves without a PIN. So every
connection also installs a default-deny SQLite authorizer that permits only
reads, function calls and a fixed list of informational pragmas. The authorizer
is enforced by SQLite while compiling each statement, so it cannot be dodged by
formatting or by nesting the write inside a subquery -- unlike a scan of the SQL
text, which is why no such scan is attempted.
"""

from __future__ import annotations

import math
import shutil
import sqlite3
import tempfile
import time
from pathlib import Path
from typing import Any, Iterable

from services.life_data_service import ClosingSqliteConnection

# A query that runs longer than this is aborted through the progress handler.
QUERY_TIMEOUT_SECONDS = 15.0
# Progress-handler granularity (VM instructions between deadline checks).
_PROGRESS_INSTRUCTIONS = 10_000

# Row/cell caps so a `SELECT * FROM raw_cmms_record` cannot serialise every
# stored Limble payload into one JSON response.
DEFAULT_ROW_LIMIT = 100
MAX_ROW_LIMIT = 1000
MAX_CELL_CHARS = 2000

# Largest value SQLite accepts as an INTEGER parameter. Binding anything beyond
# it raises OverflowError, which is not an sqlite3.Error and so would escape the
# service's error handling as an unhandled 500 rather than a bad-request.
_SQLITE_MAX_INT = 2**63 - 1

# Ceiling on the size of any single string or blob SQLite will produce.
#
# MAX_CELL_CHARS truncates a value only *after* Python already holds it, which is
# too late: `SELECT randomblob(1000000000)` is materialised in full first, and a
# 200 MB blob measured at ~394 MB of resident memory. The progress handler does
# not help either -- it is invoked between VM instructions and fires zero times
# while one allocation opcode runs, so the query deadline never triggers. This
# limit makes SQLite refuse to build the value at all ("string or blob too big").
# Generous next to any real GREMLIN value: the largest legitimate cell is a
# Limble raw_json payload, which is kilobytes.
MAX_VALUE_BYTES = 8 * 1024 * 1024

# Ceiling on the raw bytes one result page may draw from SQLite.
#
# MAX_VALUE_BYTES bounds a single value but not their sum: 25 rows of a 4 MB
# blob are each well under the per-value cap and still grew the process from
# 34 MB to 137 MB. Rows are therefore consumed one at a time, converted to their
# capped display form immediately, and the raw row dropped -- so peak memory is
# roughly one row rather than a whole page, and this budget stops the scan once
# a page has drawn too much in total.
MAX_RESULT_BYTES = 32 * 1024 * 1024

# Tables SQLite maintains for itself. They are neither app schema nor drift.
_INTERNAL_TABLE_PREFIX = "sqlite_"

# Authorizer actions that only ever read. SQLITE_FUNCTION covers aggregates such
# as COUNT(); extension loading is disabled by default in Python's sqlite3, so no
# file-touching function is reachable through it.
_READ_ACTIONS = frozenset(
    {
        sqlite3.SQLITE_SELECT,
        sqlite3.SQLITE_READ,
        sqlite3.SQLITE_FUNCTION,
        sqlite3.SQLITE_RECURSIVE,
    }
)

# Informational pragmas only. Everything this module needs for introspection is
# here; anything that could alter the file (journal_mode=, user_version=, …) is
# absent and therefore denied.
_ALLOWED_PRAGMAS = frozenset(
    {
        "application_id",
        "collation_list",
        "compile_options",
        "data_version",
        "database_list",
        "encoding",
        "foreign_key_list",
        "freelist_count",
        "function_list",
        "index_info",
        "index_list",
        "index_xinfo",
        "journal_mode",
        "page_count",
        "page_size",
        "table_info",
        "table_xinfo",
        "user_version",
    }
)


def _readonly_authorizer(action: int, arg1: str | None, _arg2: str | None, _db: str | None, _trigger: str | None) -> int:
    """Default-deny authorizer: reads and informational pragmas only.

    SQLite consults this while preparing each statement, so a denied action
    fails at compile time no matter how the SQL is written.
    """

    if action in _READ_ACTIONS:
        return sqlite3.SQLITE_OK
    if action == sqlite3.SQLITE_PRAGMA:
        return sqlite3.SQLITE_OK if (arg1 or "").lower() in _ALLOWED_PRAGMAS else sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_DENY

# Feature whose tables are still in real databases but whose code was removed
# from the app (it survives only under Reference/availability_dashboard/).
_KNOWN_ORPHAN_PREFIXES = {
    "availability_": "Removed Availability Dashboard feature (code now only under Reference/).",
}


class SchemaServiceError(RuntimeError):
    """Raised when the database cannot be opened or a request is not valid."""


class SchemaService:
    """Read-only schema, data and query access to a GREMLIN.db file."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)

    # ------------------------------------------------------------------
    # Connections
    # ------------------------------------------------------------------
    def _readonly_uri(self) -> str:
        """Build a ``mode=ro`` URI for the database path.

        ``as_uri`` percent-escapes the path, which keeps a Windows-style default
        path (``C:\\GREMLIN\\GREMLIN.db``) usable even when the app runs on Linux
        and that string is just an ordinary relative filename.
        """

        try:
            base = self.db_path.resolve().as_uri()
        except ValueError as exc:  # pragma: no cover - defensive
            raise SchemaServiceError(f"Could not build a database URI for {self.db_path}: {exc}") from exc
        return f"{base}?mode=ro"

    def connect(self) -> sqlite3.Connection:
        """Open a guarded read-only connection.

        ``ClosingSqliteConnection`` is reused from the life-data service so that
        ``with self.connect() as conn`` actually closes the handle: the stock
        ``sqlite3.Connection`` context manager only commits or rolls back, which
        would leak a descriptor per request until the collector ran.
        """

        if not self.db_path.is_file():
            raise SchemaServiceError(
                f"No database file at {self.db_path}. "
                "Set GREMLIN_DB_PATH to a reachable GREMLIN.db file."
            )
        try:
            conn = sqlite3.connect(
                self._readonly_uri(),
                uri=True,
                timeout=5.0,
                factory=ClosingSqliteConnection,
            )
        except sqlite3.Error as exc:
            raise SchemaServiceError(f"Could not open {self.db_path} read-only: {exc}") from exc
        conn.row_factory = sqlite3.Row
        # Decode TEXT leniently. sqlite3 decodes strictly by default and raises
        # "Could not decode to UTF-8" for the whole statement, so one row of
        # mis-encoded text makes an entire table unbrowsable. GREMLIN.db is
        # long-lived and its raw tables came from a legacy Excel/CSV importer,
        # so Latin-1 bytes in asset names are a realistic thing to find -- and a
        # tool whose job is to show what is actually in the file should show it,
        # damage included, rather than refuse the request. Undecodable bytes
        # render as U+FFFD, which is indistinguishable from a stored U+FFFD;
        # that trade is fine for inspection, but do not treat these strings as
        # byte-exact.
        conn.text_factory = lambda raw: raw.decode("utf-8", errors="replace")
        # Blocks VACUUM INTO / ATTACH, which mode=ro alone allows.
        conn.set_authorizer(_readonly_authorizer)
        # setlimit is Python 3.11+. Without it an oversized value is merely
        # truncated after the fact, which does not bound memory.
        if hasattr(conn, "setlimit"):
            conn.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, MAX_VALUE_BYTES)
        # Bound every connection, not just the query paths: the catalogue
        # COUNT(*) helper runs on a rollback-journal database, where a long read
        # holds a lock that delays GREMLIN's writers.
        self._guard_runtime(conn, QUERY_TIMEOUT_SECONDS)
        return conn

    @staticmethod
    def _require_value_limit(conn: sqlite3.Connection) -> None:
        """Refuse to read stored values when SQLite cannot cap their size.

        Guards every path that reads arbitrary values, not just the console.
        Without ``setlimit`` there is no pre-materialisation bound and nothing
        downstream can substitute for one: the value is built inside SQLite
        before Python sees it, row-at-a-time streaming still receives the first
        row whole, and the progress handler does not fire during a single
        allocation opcode. Reading a legacy table that happens to hold one huge
        TEXT or BLOB would exhaust the process just as an invented value would.

        The metadata panels do not go through here: ``pipeline`` selects six
        named scalar columns, and ``table_detail`` reads defaults out of PRAGMA
        output, so neither can encounter an unbounded value.
        """

        if not hasattr(conn, "setlimit"):
            raise SchemaServiceError(
                "Reading row values requires Python 3.11 or newer: on older versions SQLite "
                "cannot be told to refuse oversized values, so one large TEXT or BLOB could "
                "exhaust the server's memory. The Overview, Schema, Drift and Pipeline panels "
                "do not read row values and are unaffected."
            )

    @staticmethod
    def _collect_rows(cursor: sqlite3.Cursor, max_rows: int) -> tuple[list[list[Any]], bool]:
        """Read up to ``max_rows`` rows without ever holding a whole page raw.

        ``fetchall``/``fetchmany`` materialise every row before anything can cap
        them, so a page of individually-legal values still adds up. Converting
        each row as it arrives keeps only the capped display form, and the byte
        budget stops a scan whose values are large but under the per-value cap.

        Returns the rows and whether more were available than were returned.
        """

        rows: list[list[Any]] = []
        remaining = MAX_RESULT_BYTES
        truncated = False
        for raw in cursor:
            if len(rows) >= max_rows:
                truncated = True
                break
            remaining -= _raw_row_bytes(raw)
            rows.append([_cell(value) for value in raw])
            del raw
            if remaining <= 0:
                truncated = True
                break
        return rows, truncated

    @staticmethod
    def _guard_runtime(conn: sqlite3.Connection, timeout_seconds: float) -> None:
        """Abort any statement that outlives ``timeout_seconds``."""

        deadline = time.monotonic() + timeout_seconds

        def _abort() -> int:
            return 1 if time.monotonic() > deadline else 0

        conn.set_progress_handler(_abort, _PROGRESS_INSTRUCTIONS)

    # ------------------------------------------------------------------
    # Overview
    # ------------------------------------------------------------------
    def overview(self) -> dict[str, Any]:
        """File-level facts about the database: size, PRAGMAs, object counts."""

        info: dict[str, Any] = {
            "db_path": str(self.db_path),
            "resolved_path": str(self.db_path.resolve()) if self.db_path.exists() else None,
            "exists": self.db_path.is_file(),
            "sqlite_version": sqlite3.sqlite_version,
        }
        if not info["exists"]:
            info["error"] = f"No database file at {self.db_path}."
            return info

        stat = self.db_path.stat()
        info["size_bytes"] = stat.st_size
        info["modified_at"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(stat.st_mtime))

        with self.connect() as conn:
            for pragma in ("page_size", "page_count", "journal_mode", "encoding", "user_version", "freelist_count"):
                try:
                    info[pragma] = conn.execute(f"PRAGMA {pragma}").fetchone()[0]
                except (sqlite3.Error, TypeError, IndexError):
                    info[pragma] = None
            counts = conn.execute(
                """
                SELECT type, COUNT(*) AS count
                FROM sqlite_master
                WHERE name NOT LIKE 'sqlite_%'
                GROUP BY type
                """
            ).fetchall()
            info["object_counts"] = {row["type"]: row["count"] for row in counts}
        return info

    # ------------------------------------------------------------------
    # Table listing / detail
    # ------------------------------------------------------------------
    def _object_names(self, conn: sqlite3.Connection, kinds: Iterable[str] = ("table", "view")) -> list[str]:
        placeholders = ", ".join("?" for _ in kinds)
        rows = conn.execute(
            f"SELECT name FROM sqlite_master WHERE type IN ({placeholders}) ORDER BY name",
            tuple(kinds),
        ).fetchall()
        return [row["name"] for row in rows]

    def _assert_known_table(self, conn: sqlite3.Connection, table: str) -> str:
        """Whitelist ``table`` against the live catalogue.

        SQLite cannot parameterise an identifier, so a table name always ends up
        interpolated into the SQL text. Accepting only names that already exist
        in ``sqlite_master`` is what makes that safe; quoting is a second layer.
        """

        if table in self._object_names(conn):
            return table
        raise SchemaServiceError(f"Unknown table or view: {table!r}")

    @staticmethod
    def _quote(identifier: str) -> str:
        return '"' + identifier.replace('"', '""') + '"'

    def _row_count(self, conn: sqlite3.Connection, table: str) -> int | None:
        try:
            row = conn.execute(f"SELECT COUNT(*) FROM {self._quote(table)}").fetchone()
        except sqlite3.Error:
            return None
        return int(row[0]) if row else None

    def tables(self, *, include_row_counts: bool = True) -> list[dict[str, Any]]:
        """Every table and view in the file, annotated with its origin."""

        reference = _reference_schema()
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT name, type
                FROM sqlite_master
                WHERE type IN ('table', 'view')
                ORDER BY type, name
                """
            ).fetchall()
            results = []
            for row in rows:
                name = row["name"]
                entry: dict[str, Any] = {
                    "name": name,
                    "type": row["type"],
                    "origin": _classify_table(name, reference),
                    "note": _origin_note(name, reference),
                    "column_count": len(self._columns(conn, name)),
                    "row_count": self._row_count(conn, name) if include_row_counts else None,
                }
                results.append(entry)
        return results

    def _columns(self, conn: sqlite3.Connection, table: str) -> list[dict[str, Any]]:
        rows = conn.execute(f"PRAGMA table_info({self._quote(table)})").fetchall()
        return [
            {
                "position": row["cid"],
                "name": row["name"],
                "type": row["type"] or "",
                "not_null": bool(row["notnull"]),
                "default": row["dflt_value"],
                "primary_key": bool(row["pk"]),
            }
            for row in rows
        ]

    def table_detail(self, table: str) -> dict[str, Any]:
        """Columns, foreign keys, indexes, DDL and per-table drift."""

        reference = _reference_schema()
        with self.connect() as conn:
            table = self._assert_known_table(conn, table)
            quoted = self._quote(table)
            columns = self._columns(conn, table)

            foreign_keys = [
                {
                    "column": row["from"],
                    "references_table": row["table"],
                    "references_column": row["to"],
                    "on_update": row["on_update"],
                    "on_delete": row["on_delete"],
                }
                for row in conn.execute(f"PRAGMA foreign_key_list({quoted})").fetchall()
            ]

            indexes = []
            for row in conn.execute(f"PRAGMA index_list({quoted})").fetchall():
                index_columns = [
                    entry["name"]
                    for entry in conn.execute(f"PRAGMA index_info({self._quote(row['name'])})").fetchall()
                    if entry["name"] is not None
                ]
                indexes.append(
                    {
                        "name": row["name"],
                        "unique": bool(row["unique"]),
                        "origin": row["origin"],
                        "columns": index_columns,
                    }
                )

            ddl_row = conn.execute(
                "SELECT sql FROM sqlite_master WHERE name = ? AND type IN ('table', 'view')",
                (table,),
            ).fetchone()

            return {
                "name": table,
                "origin": _classify_table(table, reference),
                "note": _origin_note(table, reference),
                "row_count": self._row_count(conn, table),
                "columns": columns,
                "foreign_keys": foreign_keys,
                "indexes": indexes,
                "ddl": ddl_row["sql"] if ddl_row else None,
                "drift": _table_drift(table, [column["name"] for column in columns], reference),
            }

    # ------------------------------------------------------------------
    # Data browsing
    # ------------------------------------------------------------------
    def table_rows(
        self,
        table: str,
        *,
        limit: int = DEFAULT_ROW_LIMIT,
        offset: int = 0,
        order_by: str | None = None,
        descending: bool = False,
    ) -> dict[str, Any]:
        """A capped, paginated page of rows from one table."""

        limit = max(1, min(int(limit), MAX_ROW_LIMIT))
        # Clamped at both ends: an offset past SQLite's integer range cannot be
        # bound as a parameter at all, and simply returns no rows once clamped.
        offset = min(max(0, int(offset)), _SQLITE_MAX_INT)
        with self.connect() as conn:
            self._require_value_limit(conn)
            table = self._assert_known_table(conn, table)
            column_names = [column["name"] for column in self._columns(conn, table)]
            order_clause = ""
            if order_by:
                # Same rule as the table name: only a column that genuinely
                # exists is allowed anywhere near the SQL text.
                if order_by not in column_names:
                    raise SchemaServiceError(f"Unknown column {order_by!r} on {table}.")
                order_clause = f" ORDER BY {self._quote(order_by)} {'DESC' if descending else 'ASC'}"

            total = self._row_count(conn, table)
            sql = f"SELECT * FROM {self._quote(table)}{order_clause} LIMIT ? OFFSET ?"
            try:
                cursor = conn.execute(sql, (limit, offset))
                rows, _ = self._collect_rows(cursor, limit)
            except sqlite3.Error as exc:
                raise SchemaServiceError(_friendly_sqlite_error(exc)) from exc
            columns = [description[0] for description in cursor.description or []]

        # The byte budget can end a page early, so the next page does not begin at
        # offset + limit. Return where to resume rather than letting the caller
        # infer it: advancing by the requested limit would skip every row the
        # budget cut, and on a table of large values most rows became unreachable.
        # _collect_rows always yields at least one row when any remain, so this
        # always makes progress.
        next_offset = offset + len(rows)
        return {
            "table": table,
            "columns": columns or column_names,
            "rows": rows,
            "total_rows": total,
            "limit": limit,
            "offset": offset,
            "next_offset": next_offset,
            "has_more": bool(rows) and (total is None or next_offset < total),
            "order_by": order_by,
            "descending": descending,
        }

    # ------------------------------------------------------------------
    # Query console
    # ------------------------------------------------------------------
    def run_query(self, sql: str, *, max_rows: int = DEFAULT_ROW_LIMIT) -> dict[str, Any]:
        """Run one read-only statement and return capped results.

        No SQL parsing decides what is allowed: the connection is ``mode=ro``, so
        SQLite itself rejects anything that would write. Multi-statement input is
        rejected by the driver for the same reason -- one statement per execute.
        """

        statement = (sql or "").strip().rstrip(";").strip()
        if not statement:
            raise SchemaServiceError("Enter a SQL statement to run.")
        max_rows = max(1, min(int(max_rows), MAX_ROW_LIMIT))

        started = time.monotonic()
        with self.connect() as conn:
            self._require_value_limit(conn)
            try:
                cursor = conn.execute(statement)
                rows, truncated = self._collect_rows(cursor, max_rows)
            except sqlite3.Error as exc:
                raise SchemaServiceError(_friendly_sqlite_error(exc)) from exc
            columns = [description[0] for description in cursor.description or []]
        elapsed_ms = (time.monotonic() - started) * 1000.0

        return {
            "columns": columns,
            "rows": rows,
            "row_count": len(rows),
            "truncated": truncated,
            "max_rows": max_rows,
            "elapsed_ms": round(elapsed_ms, 1),
        }

    # ------------------------------------------------------------------
    # Pipeline + drift
    # ------------------------------------------------------------------
    def pipeline(self) -> dict[str, Any]:
        """Row counts along the ingestion -> analysis path, plus recent imports.

        This is the fastest way to see *where* a data problem starts: a healthy
        database shrinks monotonically down these stages, so the first stage that
        is unexpectedly zero is the one to investigate.
        """

        stages = [
            ("raw_cmms_record", "Raw Limble payloads"),
            ("mapped_cmms_record", "Mapped CMMS records"),
            ("event_disposition", "Event dispositions"),
            ("event_processing_record", "Event processing records"),
            ("weibull_observation", "Weibull observations"),
            ("analysis_dataset", "Analysis datasets"),
            ("weibull_result", "Weibull results"),
            ("approved_weibull_parameter", "Approved parameters"),
        ]
        with self.connect() as conn:
            present = set(self._object_names(conn, ("table",)))
            counts = [
                {
                    "table": name,
                    "label": label,
                    "present": name in present,
                    "row_count": self._row_count(conn, name) if name in present else None,
                }
                for name, label in stages
            ]
            batches: list[dict[str, Any]] = []
            if "import_batch" in present:
                columns = {column["name"] for column in self._columns(conn, "import_batch")}
                wanted = [
                    column
                    for column in (
                        "import_batch_id",
                        "source_system",
                        "status",
                        "import_started_at",
                        "import_completed_at",
                        "raw_row_count",
                    )
                    if column in columns
                ]
                if wanted:
                    select = ", ".join(self._quote(column) for column in wanted)
                    order = "import_batch_id" if "import_batch_id" in columns else wanted[0]
                    try:
                        rows = conn.execute(
                            f"SELECT {select} FROM import_batch ORDER BY {self._quote(order)} DESC LIMIT 10"
                        ).fetchall()
                        batches = [{key: _cell(row[key]) for key in wanted} for row in rows]
                    except sqlite3.Error:
                        batches = []
        return {"stages": counts, "recent_batches": batches}

    def drift_report(self) -> dict[str, Any]:
        """Differences between what the code builds and what the file contains."""

        reference = _reference_schema()
        if reference.get("error"):
            return {"available": False, "error": reference["error"]}

        with self.connect() as conn:
            live_tables = set(self._object_names(conn, ("table",)))
            live_columns = {
                table: {column["name"] for column in self._columns(conn, table)}
                for table in live_tables
            }

        expected_tables = set(reference["tables"])
        app_tables = {name for name in live_tables if not name.startswith(_INTERNAL_TABLE_PREFIX)}

        missing_tables = sorted(expected_tables - live_tables)
        extra_tables = sorted(app_tables - expected_tables)

        column_drift = []
        for table in sorted(expected_tables & live_tables):
            expected_columns = set(reference["columns"].get(table, set()))
            actual_columns = live_columns.get(table, set())
            missing = sorted(expected_columns - actual_columns)
            extra = sorted(actual_columns - expected_columns)
            if missing or extra:
                column_drift.append(
                    {
                        "table": table,
                        "missing_in_file": missing,
                        "extra_in_file": extra,
                    }
                )

        return {
            "available": True,
            "missing_tables": [
                {"name": name, "note": "Defined in code but absent from this file."}
                for name in missing_tables
            ],
            "extra_tables": [
                {"name": name, "note": _origin_note(name, reference) or "Not defined by current schema code."}
                for name in extra_tables
            ],
            "column_drift": column_drift,
            "reference_table_count": len(expected_tables),
            "live_table_count": len(app_tables),
        }


# ----------------------------------------------------------------------
# Reference schema (what the current code would build from scratch)
# ----------------------------------------------------------------------
_REFERENCE_CACHE: dict[str, Any] | None = None


def _reference_schema() -> dict[str, Any]:
    """Introspect a throwaway database built by the real bootstrap code.

    Running the actual ``ensure_schema`` paths -- rather than parsing DDL strings
    or keeping a duplicate table list here -- means the drift report cannot go
    stale when the schema code changes.
    """

    global _REFERENCE_CACHE
    if _REFERENCE_CACHE is not None:
        return _REFERENCE_CACHE

    tmpdir = tempfile.mkdtemp(prefix="gremlin_reference_schema_")
    reference_path = Path(tmpdir) / "reference.db"
    try:
        from repositories.raw_repo import RawRepository
        from services.life_data_service import LifeDataService

        RawRepository(reference_path).ensure_schema()
        LifeDataService(reference_path, refresh_on_startup=False)

        with sqlite3.connect(reference_path) as conn:
            conn.row_factory = sqlite3.Row
            names = [
                row["name"]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
                ).fetchall()
            ]
            columns = {
                name: {row["name"] for row in conn.execute(f'PRAGMA table_info("{name}")').fetchall()}
                for name in names
            }
        _REFERENCE_CACHE = {"tables": names, "columns": columns, "error": None}
    except Exception as exc:  # noqa: BLE001 - degrade to "drift unavailable"
        _REFERENCE_CACHE = {
            "tables": [],
            "columns": {},
            "error": f"Could not build the reference schema: {exc}",
        }
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    return _REFERENCE_CACHE


def reset_reference_schema_cache() -> None:
    """Drop the cached reference schema (used by tests)."""

    global _REFERENCE_CACHE
    _REFERENCE_CACHE = None


def _classify_table(name: str, reference: dict[str, Any]) -> str:
    if name.startswith(_INTERNAL_TABLE_PREFIX):
        return "internal"
    # A failed reference build yields an empty table list. Calling everything
    # "orphaned" on the back of that would libel core tables like
    # raw_cmms_record, so say the comparison could not be made instead.
    if reference.get("error"):
        return "unknown"
    if name in set(reference.get("tables", [])):
        return "code"
    return "orphaned"


def _origin_note(name: str, reference: dict[str, Any]) -> str | None:
    if name.startswith(_INTERNAL_TABLE_PREFIX):
        return "SQLite internal bookkeeping table."
    if reference.get("error"):
        return "Could not be compared: the reference schema failed to build."
    if name in set(reference.get("tables", [])):
        return None
    for prefix, note in _KNOWN_ORPHAN_PREFIXES.items():
        if name.startswith(prefix):
            return note
    return "Present in the file but not created by current schema code."


def _table_drift(table: str, actual_columns: list[str], reference: dict[str, Any]) -> dict[str, Any]:
    if reference.get("error") or table not in reference.get("columns", {}):
        return {"missing_in_file": [], "extra_in_file": []}
    expected = set(reference["columns"][table])
    actual = set(actual_columns)
    return {
        "missing_in_file": sorted(expected - actual),
        "extra_in_file": sorted(actual - expected),
    }


# ----------------------------------------------------------------------
# Value / error presentation
# ----------------------------------------------------------------------
def _raw_row_bytes(row: Any) -> int:
    """Approximate the bytes SQLite handed over for one row, before capping."""

    total = 0
    for value in row:
        if isinstance(value, (bytes, bytearray, memoryview)):
            total += len(value)
        elif isinstance(value, str):
            total += len(value)
    return total


def _cell(value: Any) -> Any:
    """Make one column value safe and small enough to JSON-serialise.

    ``raw_cmms_record.raw_json`` holds a full Limble payload per row, so
    untruncated values would dominate every response that touches it.
    """

    if value is None or isinstance(value, bool) or isinstance(value, int):
        return value
    if isinstance(value, float):
        # Infinity/NaN are not valid JSON. Python's encoder emits them as bare
        # `Infinity`/`NaN` tokens, which the browser's strict JSON.parse rejects
        # -- so one `SELECT 1e999` would fail the whole response rather than
        # showing an odd value. Render them as text instead.
        return value if math.isfinite(value) else repr(value).replace("inf", "Infinity").replace("nan", "NaN")
    if isinstance(value, (bytes, bytearray, memoryview)):
        return f"<BLOB {len(bytes(value))} bytes>"
    text = str(value)
    if len(text) > MAX_CELL_CHARS:
        return text[:MAX_CELL_CHARS] + f"… (truncated, {len(text)} chars)"
    return text


def _friendly_sqlite_error(exc: sqlite3.Error) -> str:
    """Turn SQLite's terser failures into something a developer can act on."""

    message = str(exc)
    lowered = message.lower()
    if "not authorized" in lowered:
        return (
            "Only read statements are allowed here. Anything that could write a file "
            "-- INSERT/UPDATE/DELETE, DDL, ATTACH, VACUUM INTO -- is blocked, as are "
            "pragmas other than the informational ones."
        )
    if "readonly" in lowered or "read-only" in lowered:
        return (
            "This console is read-only. GREMLIN.db is shared and serialises writers, "
            "so the dev dashboard never opens a writable connection."
        )
    if "interrupted" in lowered:
        return f"Query aborted after {QUERY_TIMEOUT_SECONDS:.0f}s. Narrow it with a WHERE clause or LIMIT."
    if "one statement at a time" in lowered:
        return "Run one statement at a time."
    if "too big" in lowered:
        return (
            f"A single value exceeded the {MAX_VALUE_BYTES // (1024 * 1024)} MB cap. "
            "Narrow the query (for example with substr()) rather than selecting the whole value."
        )
    return message
