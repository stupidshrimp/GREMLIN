import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from services.schema_service import (
    MAX_CELL_CHARS,
    SchemaService,
    SchemaServiceError,
    reset_reference_schema_cache,
)
from services.life_data_service import LifeDataService
from repositories.raw_repo import RawRepository


def _build_database(path: Path) -> None:
    """Create a database the way the app does, then add legacy/orphan drift."""

    RawRepository(path).ensure_schema()
    LifeDataService(path, refresh_on_startup=False)
    with sqlite3.connect(path) as conn:
        # A table from the removed Availability Dashboard, as real files still have.
        conn.execute("CREATE TABLE availability_settings (setting_key TEXT PRIMARY KEY, setting_value TEXT)")
        # A legacy column the current schema code would not create.
        conn.execute("ALTER TABLE mapped_cmms_record ADD COLUMN legacy_scratch_column TEXT")
        conn.execute(
            "INSERT INTO import_batch (source_system, status, raw_row_count) VALUES ('Limble', 'COMPLETED', 2)"
        )
        conn.executemany(
            "INSERT INTO raw_cmms_record (import_batch_id, source_record_id, raw_json) VALUES (?, ?, ?)",
            [
                (1, "1001", json.dumps({"taskID": 1001, "Asset Number": "3101"})),
                (1, "1002", json.dumps({"taskID": 1002, "Asset Number": "3102"})),
            ],
        )


class SchemaServiceTests(unittest.TestCase):
    def setUp(self):
        reset_reference_schema_cache()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db_path = Path(self._tmp.name) / "gremlin.db"
        _build_database(self.db_path)
        self.service = SchemaService(self.db_path)

    # -- connection safety ------------------------------------------------
    def test_connection_rejects_writes(self):
        with self.service.connect() as conn:
            with self.assertRaises(sqlite3.DatabaseError) as ctx:
                conn.execute("CREATE TABLE should_not_exist (id INTEGER)")
        self.assertIn("not authorized", str(ctx.exception).lower())

    def test_source_database_is_still_opened_read_only(self):
        # The authorizer is the outer guard, but mode=ro must stay underneath it:
        # it is what keeps the dashboard from ever taking the writer lock that
        # every other GREMLIN process contends for. Check it without the
        # authorizer so this fails if the URI silently loses mode=ro.
        conn = sqlite3.connect(self.service._readonly_uri(), uri=True)
        try:
            with self.assertRaises(sqlite3.OperationalError) as ctx:
                conn.execute("CREATE TABLE should_not_exist (id INTEGER)")
            self.assertIn("readonly", str(ctx.exception).lower())
        finally:
            conn.close()

    def test_write_query_is_rejected_with_a_clear_message(self):
        with self.assertRaises(SchemaServiceError) as ctx:
            self.service.run_query("DELETE FROM raw_cmms_record")
        self.assertIn("only read statements", str(ctx.exception).lower())

    def test_vacuum_into_cannot_write_a_copy_of_the_database(self):
        # mode=ro protects the source file but still lets SQLite create new
        # files, which would put a full copy of GREMLIN.db wherever the process
        # can write -- including Flask's unauthenticated /static/ route.
        target = Path(self._tmp.name) / "exfiltrated.db"
        with self.assertRaises(SchemaServiceError):
            self.service.run_query(f"VACUUM INTO '{target}'")
        self.assertFalse(target.exists())

    def test_attach_database_is_blocked(self):
        target = Path(self._tmp.name) / "side.db"
        with self.assertRaises(SchemaServiceError):
            self.service.run_query(f"ATTACH DATABASE '{target}' AS side")
        self.assertFalse(target.exists())

    def test_ddl_and_pragma_writes_are_blocked(self):
        for statement in (
            "CREATE TABLE nope (id INTEGER)",
            "DROP TABLE raw_cmms_record",
            "UPDATE raw_cmms_record SET raw_json = '{}'",
            "PRAGMA user_version = 99",
            "PRAGMA writable_schema = ON",
        ):
            with self.subTest(statement=statement):
                with self.assertRaises(SchemaServiceError):
                    self.service.run_query(statement)

    def test_informational_pragmas_still_work(self):
        payload = self.service.run_query("PRAGMA table_info(raw_cmms_record)")
        self.assertTrue(payload["rows"])

    def test_connection_closes_on_context_exit(self):
        conn = self.service.connect()
        with conn:
            conn.execute("SELECT 1")
        with self.assertRaises(sqlite3.ProgrammingError):
            conn.execute("SELECT 1")

    def test_missing_database_reports_the_path(self):
        service = SchemaService(Path(self._tmp.name) / "absent.db")
        with self.assertRaises(SchemaServiceError) as ctx:
            service.connect()
        self.assertIn("absent.db", str(ctx.exception))

    # -- table listing / detail -------------------------------------------
    def test_tables_classify_origin(self):
        by_name = {table["name"]: table for table in self.service.tables()}
        self.assertEqual(by_name["mapped_cmms_record"]["origin"], "code")
        self.assertEqual(by_name["raw_cmms_record"]["origin"], "code")
        self.assertEqual(by_name["availability_settings"]["origin"], "orphaned")
        self.assertIn("Availability", by_name["availability_settings"]["note"])

    def test_table_row_counts(self):
        by_name = {table["name"]: table for table in self.service.tables()}
        self.assertEqual(by_name["raw_cmms_record"]["row_count"], 2)

    def test_table_detail_reports_columns_and_indexes(self):
        detail = self.service.table_detail("raw_cmms_record")
        column_names = {column["name"] for column in detail["columns"]}
        self.assertIn("raw_json", column_names)
        self.assertIn("import_batch_id", column_names)
        self.assertTrue(detail["ddl"].strip().upper().startswith("CREATE TABLE"))
        self.assertTrue(any(fk["references_table"] == "import_batch" for fk in detail["foreign_keys"]))
        self.assertTrue(detail["indexes"])

    def test_unknown_table_is_rejected(self):
        with self.assertRaises(SchemaServiceError):
            self.service.table_detail("no_such_table")

    def test_table_name_injection_is_rejected(self):
        # The name is not in sqlite_master, so it never reaches the SQL text.
        with self.assertRaises(SchemaServiceError):
            self.service.table_rows('raw_cmms_record"; DROP TABLE raw_cmms_record; --')
        with self.service.connect() as conn:
            still_there = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='raw_cmms_record'"
            ).fetchone()
        self.assertIsNotNone(still_there)

    # -- data browsing -----------------------------------------------------
    def test_table_rows_paginates(self):
        first = self.service.table_rows("raw_cmms_record", limit=1, offset=0)
        second = self.service.table_rows("raw_cmms_record", limit=1, offset=1)
        self.assertEqual(first["total_rows"], 2)
        self.assertEqual(len(first["rows"]), 1)
        self.assertNotEqual(first["rows"][0], second["rows"][0])

    def test_table_rows_rejects_unknown_order_column(self):
        with self.assertRaises(SchemaServiceError):
            self.service.table_rows("raw_cmms_record", order_by="not_a_column")

    def test_table_rows_accepts_known_order_column(self):
        payload = self.service.table_rows("raw_cmms_record", order_by="raw_record_id", descending=True)
        self.assertEqual(payload["order_by"], "raw_record_id")
        self.assertEqual(len(payload["rows"]), 2)

    def test_long_values_are_truncated(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT INTO raw_cmms_record (import_batch_id, source_record_id, raw_json) VALUES (?, ?, ?)",
                (1, "9999", "x" * (MAX_CELL_CHARS + 500)),
            )
        payload = self.service.run_query("SELECT raw_json FROM raw_cmms_record WHERE source_record_id = '9999'")
        self.assertIn("truncated", payload["rows"][0][0])
        self.assertLess(len(payload["rows"][0][0]), MAX_CELL_CHARS + 100)

    # -- query console -----------------------------------------------------
    def test_run_query_returns_rows(self):
        payload = self.service.run_query("SELECT COUNT(*) AS n FROM raw_cmms_record")
        self.assertEqual(payload["columns"], ["n"])
        self.assertEqual(payload["rows"], [[2]])

    def test_run_query_caps_rows(self):
        payload = self.service.run_query("SELECT * FROM raw_cmms_record", max_rows=1)
        self.assertEqual(payload["row_count"], 1)
        self.assertTrue(payload["truncated"])

    def test_empty_query_is_rejected(self):
        with self.assertRaises(SchemaServiceError):
            self.service.run_query("   ")

    def test_trailing_semicolon_is_accepted(self):
        payload = self.service.run_query("SELECT 1 AS one;")
        self.assertEqual(payload["rows"], [[1]])

    # -- overview / pipeline / drift ---------------------------------------
    def test_overview_reports_file_facts(self):
        overview = self.service.overview()
        self.assertTrue(overview["exists"])
        self.assertGreater(overview["size_bytes"], 0)
        self.assertGreaterEqual(overview["object_counts"].get("table", 0), 20)

    def test_pipeline_counts_stages(self):
        pipeline = self.service.pipeline()
        stages = {stage["table"]: stage for stage in pipeline["stages"]}
        self.assertTrue(stages["raw_cmms_record"]["present"])
        self.assertEqual(stages["raw_cmms_record"]["row_count"], 2)
        self.assertEqual(len(pipeline["recent_batches"]), 1)
        self.assertEqual(pipeline["recent_batches"][0]["status"], "COMPLETED")

    def test_drift_detects_orphan_table_and_legacy_column(self):
        drift = self.service.drift_report()
        self.assertTrue(drift["available"])
        extra_names = {entry["name"] for entry in drift["extra_tables"]}
        self.assertIn("availability_settings", extra_names)

        mapped = next(entry for entry in drift["column_drift"] if entry["table"] == "mapped_cmms_record")
        self.assertIn("legacy_scratch_column", mapped["extra_in_file"])

    def test_drift_reports_column_missing_from_file(self):
        with sqlite3.connect(self.db_path) as conn:
            try:
                conn.execute("ALTER TABLE mapped_cmms_record DROP COLUMN mapping_version")
            except sqlite3.OperationalError:
                self.skipTest("SQLite build does not support DROP COLUMN")
        drift = SchemaService(self.db_path).drift_report()
        mapped = next(entry for entry in drift["column_drift"] if entry["table"] == "mapped_cmms_record")
        self.assertIn("mapping_version", mapped["missing_in_file"])

    def test_table_detail_surfaces_per_table_drift(self):
        detail = self.service.table_detail("mapped_cmms_record")
        self.assertIn("legacy_scratch_column", detail["drift"]["extra_in_file"])


class DeveloperUnlockTests(unittest.TestCase):
    """The PIN gate must reject bad input, never crash on it."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        db_path = Path(self._tmp.name) / "gremlin.db"
        _build_database(db_path)
        os.environ["GREMLIN_DB_PATH"] = str(db_path)
        os.environ["GREMLIN_SECRET_KEY"] = "test-key"
        self.addCleanup(os.environ.pop, "GREMLIN_DB_PATH", None)
        self.addCleanup(os.environ.pop, "GREMLIN_SECRET_KEY", None)

        import app as app_module

        app_module.app.config["PROPAGATE_EXCEPTIONS"] = False
        self.app_module = app_module
        self.client = app_module.app.test_client()

    def test_correct_pin_unlocks(self):
        self.assertEqual(self.client.post("/developer/unlock", data={"pin": "1336"}).status_code, 302)
        self.assertEqual(self.client.get("/developer/api/tables").status_code, 200)

    def test_bad_pins_are_rejected_without_erroring(self):
        # compare_digest raises TypeError on non-ASCII str, which would turn a
        # mistyped PIN into a 500 with a traceback.
        for pin in ("0000", "", "   ", "13é6", "🔒", "1336" * 99):
            with self.subTest(pin=pin):
                response = self.client.post("/developer/unlock", data={"pin": pin})
                self.assertEqual(response.status_code, 403)

    def test_api_is_locked_until_unlocked(self):
        self.assertEqual(self.client.get("/developer/api/tables").status_code, 403)
        self.assertEqual(self.client.post("/developer/api/query", json={"sql": "SELECT 1"}).status_code, 403)

    def test_developer_page_is_not_linked_from_the_sidebar(self):
        # The page is meant to be reachable only by typing its URL.
        self.assertNotIn("/developer", [link["url"] for link in self.app_module.NAV_LINKS])
        self.assertNotIn(b'href="/developer"', self.client.get("/").data)


if __name__ == "__main__":
    unittest.main()
