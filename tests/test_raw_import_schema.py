"""Tests for the API-first modernization of the raw import tables.

Covers GREMLIN taking ownership of ``import_batch`` and ``raw_cmms_record``:
fresh databases get the modern (legacy-nullable) shape, and existing
databases have the obsolete ``NOT NULL`` constraints on legacy file/row
metadata columns relaxed in place without losing data or breaking the
``mapped_cmms_record`` foreign keys.
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from services.life_data_service import (
    LEGACY_IMPORT_BATCH_COLUMNS,
    LEGACY_RAW_CMMS_COLUMNS,
    LifeDataService,
)


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

    def test_precheck_reports_no_write_needed_once_modern(self) -> None:
        service = LifeDataService.__new__(LifeDataService)
        service.db_path = self.db_path
        self.assertFalse(service._raw_import_schema_needs_write())

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


class LegacyDatabaseMigrationTests(unittest.TestCase):
    """An existing legacy-style database should be modernized in place."""

    def _create_legacy_database(self, db_path: Path) -> None:
        with sqlite3.connect(db_path) as conn:
            conn.executescript(
                """
                CREATE TABLE import_batch (
                    import_batch_id INTEGER PRIMARY KEY,
                    source_system TEXT NOT NULL,
                    import_started_at TEXT NOT NULL DEFAULT (datetime('now')),
                    import_completed_at TEXT,
                    status TEXT NOT NULL DEFAULT 'COMPLETED',
                    notes TEXT,
                    raw_row_count INTEGER,
                    source_file_name TEXT NOT NULL,
                    source_file_path TEXT NOT NULL,
                    source_file_hash TEXT NOT NULL
                );
                CREATE TABLE raw_cmms_record (
                    raw_record_id INTEGER PRIMARY KEY,
                    import_batch_id INTEGER NOT NULL,
                    source_system TEXT NOT NULL DEFAULT 'Limble',
                    source_record_id TEXT,
                    raw_json TEXT NOT NULL,
                    raw_content_hash TEXT,
                    imported_at TEXT NOT NULL DEFAULT (datetime('now')),
                    updated_at TEXT,
                    source_row_number INTEGER NOT NULL,
                    row_hash TEXT NOT NULL,
                    source_record_uid TEXT NOT NULL,
                    source_work_order TEXT NOT NULL,
                    UNIQUE (source_system, source_record_id),
                    FOREIGN KEY (import_batch_id) REFERENCES import_batch(import_batch_id)
                );
                CREATE INDEX idx_legacy_raw_source_uid ON raw_cmms_record(source_record_uid);
                INSERT INTO import_batch
                    (import_batch_id, source_system, source_file_name, source_file_path, source_file_hash, raw_row_count)
                    VALUES (1, 'ExcelImport', 'wos.xlsx', 'C:/data/wos.xlsx', 'abc123', 1);
                INSERT INTO raw_cmms_record
                    (raw_record_id, import_batch_id, source_record_id, raw_json,
                     source_row_number, row_hash, source_record_uid, source_work_order)
                    VALUES (1, 1, 'TASK-1', '{"taskID": 1, "name": "Pump fix"}',
                            7, 'rowhash-1', 'uid-1', 'WO-1');
                """
            )
            conn.commit()

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "GREMLIN.db"
        self._create_legacy_database(self.db_path)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_migration_relaxes_not_null_and_preserves_data(self) -> None:
        # Sanity check: legacy columns start out NOT NULL.
        with sqlite3.connect(self.db_path) as conn:
            before = _column_info(conn, "raw_cmms_record")
        self.assertEqual(before["source_row_number"]["notnull"], 1)

        # The precheck must see that a legacy database needs the write path.
        precheck_service = LifeDataService.__new__(LifeDataService)
        precheck_service.db_path = self.db_path
        self.assertTrue(precheck_service._raw_import_schema_needs_write())

        LifeDataService(self.db_path, refresh_on_startup=False)

        with sqlite3.connect(self.db_path) as conn:
            raw_cols = _column_info(conn, "raw_cmms_record")
            batch_cols = _column_info(conn, "import_batch")
            raw_row = tuple(conn.execute(
                "SELECT raw_record_id, import_batch_id, source_record_id, raw_json, "
                "source_row_number, row_hash, source_record_uid, source_work_order "
                "FROM raw_cmms_record"
            ).fetchone())
            batch_row = tuple(conn.execute(
                "SELECT source_file_name, source_file_path, source_file_hash, raw_row_count "
                "FROM import_batch"
            ).fetchone())
            indexes = {row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='raw_cmms_record'"
            )}

        for column in LEGACY_RAW_CMMS_COLUMNS:
            self.assertEqual(raw_cols[column]["notnull"], 0, f"{column} should now be nullable")
        for column in LEGACY_IMPORT_BATCH_COLUMNS:
            self.assertEqual(batch_cols[column]["notnull"], 0, f"{column} should now be nullable")
        # Required columns must remain required.
        self.assertEqual(raw_cols["raw_json"]["notnull"], 1)
        self.assertEqual(raw_cols["import_batch_id"]["notnull"], 1)
        # Existing data (including legacy values) is preserved verbatim.
        self.assertEqual(raw_row, (1, 1, "TASK-1", '{"taskID": 1, "name": "Pump fix"}', 7, "rowhash-1", "uid-1", "WO-1"))
        self.assertEqual(batch_row, ("wos.xlsx", "C:/data/wos.xlsx", "abc123", 1))
        # The user-defined index survives the rebuild.
        self.assertIn("idx_legacy_raw_source_uid", indexes)

    def test_unique_constraint_is_preserved(self) -> None:
        LifeDataService(self.db_path, refresh_on_startup=False)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("INSERT INTO import_batch (import_batch_id, source_system) VALUES (2, 'Limble')")
            with self.assertRaises(sqlite3.IntegrityError):
                # Same (source_system, source_record_id) as the preserved row.
                conn.execute(
                    "INSERT INTO raw_cmms_record (import_batch_id, source_system, source_record_id, raw_json) "
                    "VALUES (2, 'Limble', 'TASK-1', '{}')"
                )

    def test_foreign_key_targets_survive_for_mapped_records(self) -> None:
        LifeDataService(self.db_path, refresh_on_startup=False)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("PRAGMA foreign_keys = ON")
            # mapped_cmms_record references the rebuilt parents by their preserved ids.
            conn.execute(
                "INSERT INTO mapped_cmms_record (raw_record_id, import_batch_id) VALUES (1, 1)"
            )
            conn.commit()
            count = conn.execute("SELECT COUNT(*) FROM mapped_cmms_record").fetchone()[0]
        self.assertEqual(count, 1)

    def test_existing_mapped_child_rows_survive_parent_rebuild(self) -> None:
        # A pre-existing child row referencing the legacy parents (with ON DELETE
        # RESTRICT) must survive the rebuild and still resolve afterwards. This
        # drives the raw-import modernization directly so the test is not coupled
        # to the rest of the downstream schema.
        with sqlite3.connect(self.db_path) as conn:
            conn.executescript(
                """
                CREATE TABLE mapped_cmms_record (
                    mapped_record_id INTEGER PRIMARY KEY,
                    raw_record_id INTEGER NOT NULL,
                    import_batch_id INTEGER NOT NULL,
                    FOREIGN KEY (raw_record_id) REFERENCES raw_cmms_record(raw_record_id) ON DELETE RESTRICT,
                    FOREIGN KEY (import_batch_id) REFERENCES import_batch(import_batch_id) ON DELETE RESTRICT
                );
                INSERT INTO mapped_cmms_record (mapped_record_id, raw_record_id, import_batch_id)
                    VALUES (10, 1, 1);
                """
            )
            conn.commit()

        service = LifeDataService.__new__(LifeDataService)
        service.db_path = self.db_path
        service._ensure_raw_import_schema()

        with sqlite3.connect(self.db_path) as conn:
            conn.execute("PRAGMA foreign_keys = ON")
            child = tuple(conn.execute(
                "SELECT mapped_record_id, raw_record_id, import_batch_id FROM mapped_cmms_record"
            ).fetchone())
            raw_cols = _column_info(conn, "raw_cmms_record")
            violations = conn.execute("PRAGMA foreign_key_check").fetchall()
        self.assertEqual(child, (10, 1, 1))
        self.assertEqual(raw_cols["source_row_number"]["notnull"], 0)
        self.assertEqual(violations, [])

    def test_migration_is_idempotent(self) -> None:
        LifeDataService(self.db_path, refresh_on_startup=False)
        with sqlite3.connect(self.db_path) as conn:
            sql_after_first = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='raw_cmms_record'"
            ).fetchone()[0]
        # A second startup must not rebuild an already-modern table.
        LifeDataService(self.db_path, refresh_on_startup=False)
        with sqlite3.connect(self.db_path) as conn:
            sql_after_second = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='raw_cmms_record'"
            ).fetchone()[0]
            leftovers = [r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE name LIKE '%__modernized'"
            )]
        self.assertEqual(sql_after_first, sql_after_second)
        self.assertEqual(leftovers, [])


class DependentObjectSafetyTests(unittest.TestCase):
    """A table referenced by a trigger/view is left untouched (verify-before-change)."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "GREMLIN.db"
        with sqlite3.connect(self.db_path) as conn:
            conn.executescript(
                """
                CREATE TABLE import_batch (import_batch_id INTEGER PRIMARY KEY, source_system TEXT NOT NULL);
                CREATE TABLE raw_cmms_record (
                    raw_record_id INTEGER PRIMARY KEY,
                    import_batch_id INTEGER NOT NULL,
                    raw_json TEXT NOT NULL,
                    source_row_number INTEGER NOT NULL,
                    row_hash TEXT NOT NULL,
                    source_record_uid TEXT NOT NULL,
                    source_work_order TEXT NOT NULL
                );
                CREATE VIEW raw_cmms_record_view AS SELECT raw_record_id FROM raw_cmms_record;
                """
            )
            conn.commit()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_table_with_dependent_view_is_not_rewritten(self) -> None:
        LifeDataService(self.db_path, refresh_on_startup=False)
        with sqlite3.connect(self.db_path) as conn:
            raw_cols = _column_info(conn, "raw_cmms_record")
        # Constraints left as-is because a view depends on the table.
        self.assertEqual(raw_cols["source_row_number"]["notnull"], 1)


class DdlTransformUnitTests(unittest.TestCase):
    """Direct tests of the NOT NULL DDL transform without touching a database."""

    def setUp(self) -> None:
        # Build the service without running __init__ (no database needed).
        self.svc = LifeDataService.__new__(LifeDataService)

    def _relax(self, ddl: str, legacy: set[str]) -> str | None:
        return self.svc._relax_not_null_in_ddl(ddl, "t__modernized", legacy)

    def test_strips_only_legacy_columns(self) -> None:
        ddl = (
            "CREATE TABLE raw_cmms_record (\n"
            "  raw_record_id INTEGER PRIMARY KEY,\n"
            "  import_batch_id INTEGER NOT NULL,\n"
            "  raw_json TEXT NOT NULL,\n"
            "  source_row_number INTEGER NOT NULL,\n"
            "  source_work_order TEXT NOT NULL DEFAULT '',\n"
            "  UNIQUE (raw_record_id),\n"
            "  FOREIGN KEY (import_batch_id) REFERENCES import_batch(import_batch_id)\n"
            ")"
        )
        new_ddl = self._relax(ddl, set(LEGACY_RAW_CMMS_COLUMNS))
        self.assertIsNotNone(new_ddl)
        # Execute it to confirm validity, then inspect constraints.
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute(new_ddl)
        cols = {row["name"]: row for row in conn.execute("PRAGMA table_info(t__modernized)")}
        self.assertEqual(cols["source_row_number"]["notnull"], 0)
        self.assertEqual(cols["source_work_order"]["notnull"], 0)
        self.assertEqual(cols["source_work_order"]["dflt_value"], "''")
        # Non-legacy required columns are untouched.
        self.assertEqual(cols["import_batch_id"]["notnull"], 1)
        self.assertEqual(cols["raw_json"]["notnull"], 1)
        # FK and UNIQUE survive.
        fks = conn.execute("PRAGMA foreign_key_list(t__modernized)").fetchall()
        self.assertEqual(len(fks), 1)
        idx = conn.execute("PRAGMA index_list(t__modernized)").fetchall()
        self.assertTrue(idx)

    def test_returns_none_when_nothing_to_change(self) -> None:
        ddl = "CREATE TABLE import_batch (import_batch_id INTEGER PRIMARY KEY, source_system TEXT NOT NULL)"
        self.assertIsNone(self._relax(ddl, set(LEGACY_IMPORT_BATCH_COLUMNS)))

    def test_quoted_identifier_column(self) -> None:
        ddl = (
            'CREATE TABLE "raw_cmms_record" ('
            '"raw_record_id" INTEGER PRIMARY KEY, '
            '"source_record_uid" TEXT NOT NULL)'
        )
        new_ddl = self._relax(ddl, set(LEGACY_RAW_CMMS_COLUMNS))
        self.assertIsNotNone(new_ddl)
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute(new_ddl)
        cols = {row["name"]: row for row in conn.execute("PRAGMA table_info(t__modernized)")}
        self.assertEqual(cols["source_record_uid"]["notnull"], 0)

    def test_not_null_inside_check_and_string_is_preserved(self) -> None:
        item = "source_work_order TEXT NOT NULL CHECK (source_work_order <> 'NOT NULL')"
        stripped = self.svc._strip_not_null(item)
        # The standalone column constraint is removed...
        self.assertNotIn("NOT NULL CHECK", stripped)
        # ...but the CHECK body and its quoted literal are intact.
        self.assertIn("CHECK (source_work_order <> 'NOT NULL')", stripped)

    def test_quoted_default_whitespace_is_preserved(self) -> None:
        # Removing NOT NULL must not collapse double spaces inside a quoted default.
        item = "source_work_order TEXT NOT NULL DEFAULT 'WO  X'"
        stripped = self.svc._strip_not_null(item)
        self.assertEqual(stripped, "source_work_order TEXT DEFAULT 'WO  X'")

    def test_strip_not_null_at_end_of_definition(self) -> None:
        self.assertEqual(
            self.svc._strip_not_null("source_row_number INTEGER NOT NULL"),
            "source_row_number INTEGER",
        )

    def test_preserves_table_options_tail(self) -> None:
        ddl = (
            "CREATE TABLE raw_cmms_record ("
            "raw_record_id INTEGER PRIMARY KEY, "
            "source_row_number INTEGER NOT NULL) STRICT"
        )
        new_ddl = self._relax(ddl, set(LEGACY_RAW_CMMS_COLUMNS))
        self.assertIsNotNone(new_ddl)
        self.assertTrue(new_ddl.rstrip().endswith("STRICT"))


if __name__ == "__main__":
    unittest.main()
