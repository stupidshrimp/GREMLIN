"""Tests for the API-first raw import tables owned by ``LifeDataService``.

GREMLIN creates ``import_batch`` and ``raw_cmms_record`` as part of
``ensure_schema`` so a fresh database has the modern, API-first shape: the
essential columns are present (and the core ones required), while the legacy
file/row metadata columns are retained but nullable for backward compatibility
with historical Excel/CSV rows.
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from services.life_data_service import LifeDataService

LEGACY_RAW_CMMS_COLUMNS = ("source_row_number", "row_hash", "source_record_uid", "source_work_order")
LEGACY_IMPORT_BATCH_COLUMNS = ("source_file_name", "source_file_path", "source_file_hash")


def _column_info(conn: sqlite3.Connection, table: str) -> dict[str, sqlite3.Row]:
    conn.row_factory = sqlite3.Row
    return {row["name"]: row for row in conn.execute(f"PRAGMA table_info({table})")}


def _table_names(conn: sqlite3.Connection) -> set[str]:
    return {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


class ModernSchemaFreshDatabaseTests(unittest.TestCase):
    """A brand-new database should be created with the modern API-first shape."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "GREMLIN.db"
        # refresh_on_startup=False keeps the test focused on schema creation.
        LifeDataService(self.db_path, refresh_on_startup=False)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_tables_exist(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            tables = _table_names(conn)
        self.assertIn("import_batch", tables)
        self.assertIn("raw_cmms_record", tables)

    def test_legacy_columns_are_nullable(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            raw_cols = _column_info(conn, "raw_cmms_record")
            batch_cols = _column_info(conn, "import_batch")
        for column in LEGACY_RAW_CMMS_COLUMNS:
            self.assertIn(column, raw_cols)
            self.assertEqual(raw_cols[column]["notnull"], 0, f"{column} should be nullable")
        for column in LEGACY_IMPORT_BATCH_COLUMNS:
            self.assertIn(column, batch_cols)
            self.assertEqual(batch_cols[column]["notnull"], 0, f"{column} should be nullable")

    def test_essential_columns_kept_and_constrained(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            raw_cols = _column_info(conn, "raw_cmms_record")
            batch_cols = _column_info(conn, "import_batch")
        for column in ("raw_record_id", "import_batch_id", "source_system", "source_record_id",
                       "raw_json", "raw_content_hash", "imported_at", "updated_at"):
            self.assertIn(column, raw_cols)
        for column in ("import_batch_id", "source_system", "import_started_at",
                       "import_completed_at", "status", "notes", "raw_row_count"):
            self.assertIn(column, batch_cols)
        # Core API identity/payload columns stay required.
        self.assertEqual(raw_cols["import_batch_id"]["notnull"], 1)
        self.assertEqual(raw_cols["raw_json"]["notnull"], 1)

    def test_api_insert_without_legacy_values(self) -> None:
        """The whole point: an API sync can insert without fabricating legacy values."""

        with sqlite3.connect(self.db_path) as conn:
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("INSERT INTO import_batch (source_system) VALUES ('Limble')")
            conn.execute(
                "INSERT INTO raw_cmms_record (import_batch_id, source_record_id, raw_json) "
                "VALUES (1, 'TASK-1', '{\"taskID\": 1}')"
            )
            conn.commit()
            row = conn.execute(
                "SELECT source_row_number, row_hash, source_record_uid, source_work_order, status "
                "FROM raw_cmms_record JOIN import_batch USING (import_batch_id)"
            ).fetchone()
        self.assertEqual(tuple(row[:4]), (None, None, None, None))
        self.assertEqual(row[4], "PENDING")

    def test_mapping_pipeline_consumes_api_record(self) -> None:
        """A raw record inserted API-style flows through the existing mapping step."""

        service = LifeDataService(self.db_path, refresh_on_startup=False)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("INSERT INTO import_batch (source_system, status) VALUES ('Limble', 'COMPLETED')")
            conn.execute(
                "INSERT INTO raw_cmms_record (import_batch_id, source_record_id, raw_json) VALUES (?,?,?)",
                (1, "TASK-42", '{"taskID": 42, "name": "Replace bearing", "asset_number": "3101"}'),
            )
            conn.commit()

        self.assertEqual(service.refresh_mapped_cmms_records(), 1)
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            mapped = conn.execute("SELECT task_id, task_name FROM mapped_cmms_record").fetchone()
            violations = conn.execute("PRAGMA foreign_key_check").fetchall()
        self.assertEqual(mapped["task_id"], "42")
        self.assertEqual(mapped["task_name"], "Replace bearing")
        self.assertEqual(violations, [])


if __name__ == "__main__":
    unittest.main()
