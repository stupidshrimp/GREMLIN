import json
import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from services import schema_service

from services.schema_service import (
    MAX_CELL_CHARS,
    MAX_ROW_LIMIT,
    SchemaService,
    SchemaServiceError,
    reset_reference_schema_cache,
)
from services.life_data_service import LifeDataService
from repositories.availability_repo import AvailabilityRepository
from repositories.raw_repo import RawRepository


def _build_database(path: Path) -> None:
    """Create a database the way the app does, then add legacy/orphan drift."""

    RawRepository(path).ensure_schema()
    LifeDataService(path, refresh_on_startup=False)
    AvailabilityRepository(path).ensure_schema()
    with sqlite3.connect(path) as conn:
        # A leftover from the earlier Availability Dashboard attempt, as real
        # files still have. Availability's config tables are live again and are
        # built by AvailabilityRepository; only the stored-results table is
        # genuinely orphaned now that results are recomputed on request.
        conn.execute("CREATE TABLE availability_results (id INTEGER PRIMARY KEY, availability_percent REAL)")
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

    def test_oversized_values_are_refused_before_materialising(self):
        # MAX_CELL_CHARS truncates only after Python holds the value, and the
        # progress handler does not fire during a single allocation opcode, so
        # SQLite has to refuse to build the value in the first place.
        if not hasattr(sqlite3.Connection, "setlimit"):
            self.skipTest("sqlite3.Connection.setlimit requires Python 3.11+")
        with self.assertRaises(SchemaServiceError) as ctx:
            self.service.run_query("SELECT randomblob(1000000000)")
        self.assertIn("too large to return", str(ctx.exception).lower())
        # A normal-sized value is unaffected.
        self.assertEqual(len(self.service.run_query("SELECT randomblob(4096)")["rows"]), 1)

    def test_aggregate_result_size_is_bounded(self):
        # Values individually under MAX_VALUE_BYTES still add up: 25 rows of a
        # 4 MB blob grew the process by ~100 MB before this was bounded.
        if not hasattr(sqlite3.Connection, "setlimit"):
            self.skipTest("sqlite3.Connection.setlimit requires Python 3.11+")
        payload = self.service.run_query(
            "WITH RECURSIVE r(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM r WHERE i < 200) "
            "SELECT i, randomblob(4000000) FROM r",
            max_rows=200,
        )
        self.assertTrue(payload["truncated"])
        # 32 MB budget / 4 MB per row -> a handful of rows, nowhere near 200.
        self.assertLess(payload["row_count"], 20)
        self.assertTrue(all(str(row[1]).startswith("<BLOB") for row in payload["rows"]))

    def test_value_reading_refuses_without_a_value_limit(self):
        # On Python 3.10 there is no setlimit and therefore no pre-materialisation
        # bound. Every path that reads arbitrary stored values must refuse --
        # scoping this to the console alone still left the row browser exposed to
        # a legacy table holding one huge TEXT or BLOB.
        real_connect = self.service.connect

        class _NoSetlimit:
            def __init__(self, conn):
                self._conn = conn

            def __getattr__(self, name):
                if name == "setlimit":
                    raise AttributeError(name)
                return getattr(self._conn, name)

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return self._conn.__exit__(*exc)

        with mock.patch.object(self.service, "connect", lambda: _NoSetlimit(real_connect())):
            for label, call in (
                ("run_query", lambda: self.service.run_query("SELECT 1")),
                ("table_rows", lambda: self.service.table_rows("raw_cmms_record")),
            ):
                with self.subTest(path=label):
                    with self.assertRaises(SchemaServiceError) as ctx:
                        call()
                    self.assertIn("python 3.11", str(ctx.exception).lower())

            # Metadata panels read no arbitrary values, so they stay available.
            self.assertTrue(self.service.tables())
            self.assertTrue(self.service.pipeline()["stages"])
            self.assertTrue(self.service.table_detail("raw_cmms_record")["columns"])

    def test_one_wide_row_is_bounded(self):
        # Each value is under MAX_VALUE_BYTES and there is only one row, so
        # neither the per-value cap nor the between-rows budget applies: 20
        # columns of a 4 MB blob took the process from 12 MB to 165 MB.
        if not hasattr(sqlite3.Connection, "setlimit"):
            self.skipTest("sqlite3.Connection.setlimit requires Python 3.11+")
        columns = ", ".join(f"randomblob(4000000) AS c{i}" for i in range(20))
        with self.assertRaises(SchemaServiceError) as ctx:
            self.service.run_query(f"SELECT {columns}")
        self.assertIn("too large to return", str(ctx.exception).lower())

    def test_narrow_results_keep_the_full_per_value_allowance(self):
        # Scaling the cap by width must not punish ordinary single-column reads.
        if not hasattr(sqlite3.Connection, "setlimit"):
            self.skipTest("sqlite3.Connection.setlimit requires Python 3.11+")
        payload = self.service.run_query("SELECT randomblob(2000000) AS one")
        self.assertEqual(payload["row_count"], 1)
        self.assertTrue(str(payload["rows"][0][0]).startswith("<BLOB"))

    def test_pragma_still_runs_when_its_shape_cannot_be_probed(self):
        # PRAGMA does not nest inside SELECT * FROM (...), so the width probe
        # fails and the caller must assume a width rather than erroring.
        payload = self.service.run_query("PRAGMA table_info(raw_cmms_record)")
        self.assertTrue(payload["rows"])

    def test_every_connection_carries_the_query_deadline(self):
        # The catalogue COUNT(*) helper has no deadline of its own, so connect()
        # must install one -- otherwise opening the Schema or Pipeline panel on a
        # huge table holds a read lock against GREMLIN's writers indefinitely.
        # Checked behaviourally: a long statement on a plain connection must be
        # aborted once the deadline is short.
        slow = (
            "WITH RECURSIVE counter(x) AS ("
            "  SELECT 1 UNION ALL SELECT x + 1 FROM counter WHERE x < 100000000"
            ") SELECT COUNT(*) FROM counter"
        )
        with mock.patch.object(schema_service, "QUERY_TIMEOUT_SECONDS", 0.05):
            started = time.monotonic()
            with self.service.connect() as conn:
                with self.assertRaises(sqlite3.OperationalError) as ctx:
                    conn.execute(slow).fetchone()
            elapsed = time.monotonic() - started
        self.assertIn("interrupted", str(ctx.exception).lower())
        self.assertLess(elapsed, 10, "the deadline did not abort the statement")

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
        self.assertEqual(by_name["availability_results"]["origin"], "orphaned")
        self.assertIn("Availability", by_name["availability_results"]["note"])
        # The availability config tables are created by code again, so they must
        # no longer be reported as leftovers of a removed feature.
        self.assertEqual(by_name["availability_asset_groups"]["origin"], "code")
        self.assertEqual(by_name["availability_settings"]["origin"], "code")

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

    def test_oversized_pagination_is_clamped_not_fatal(self):
        # An offset beyond SQLite's INTEGER range raises OverflowError, which is
        # not an sqlite3.Error and would otherwise surface as a 500.
        payload = self.service.table_rows("raw_cmms_record", offset=10**30)
        self.assertEqual(payload["rows"], [])
        self.assertLessEqual(payload["offset"], 2**63 - 1)
        self.assertEqual(self.service.table_rows("raw_cmms_record", limit=10**30)["limit"], MAX_ROW_LIMIT)
        self.assertEqual(self.service.table_rows("raw_cmms_record", limit=-5)["limit"], 1)
        self.assertEqual(self.service.table_rows("raw_cmms_record", offset=-5)["offset"], 0)

    def test_pagination_reaches_every_row_when_pages_end_early(self):
        # The byte budget can end a page before `limit` rows. Advancing by the
        # requested limit then skips everything the budget cut -- on this table
        # it left 25 of 30 rows permanently unreachable.
        # 2 MB each: under the width-scaled per-value cap for this 12-column
        # table (~2.7 MB) but enough that the 32 MB page budget ends a page early.
        with sqlite3.connect(self.db_path) as conn:
            for i in range(30):
                conn.execute(
                    "INSERT INTO raw_cmms_record (import_batch_id, source_record_id, raw_json) "
                    "VALUES (1, ?, ?)",
                    (f"big{i}", "x" * 2_000_000),
                )
        seen: list[str] = []
        offset = 0
        for _ in range(50):  # generous ceiling; the loop should end well before this
            payload = self.service.table_rows("raw_cmms_record", limit=50, offset=offset)
            seen.extend(str(row[3]) for row in payload["rows"])
            self.assertGreater(payload["next_offset"], offset, "pagination made no progress")
            offset = payload["next_offset"]
            if not payload["has_more"]:
                break
        else:
            self.fail("pagination did not terminate")
        self.assertEqual(len(seen), payload["total_rows"])
        self.assertEqual(len(set(seen)), len(seen), "a row was returned twice")

    def test_materialising_sorts_are_refused(self):
        # A sort, DISTINCT or GROUP BY no index covers fills SQLite's sorter
        # before the first row is yielded, so no cap here can bound it; measured
        # at 11 -> 80 MB for a single fetched row. Index-satisfied sorts stream
        # and stay allowed.
        for statement in (
            "SELECT raw_record_id, raw_json FROM raw_cmms_record ORDER BY raw_json",
            "SELECT DISTINCT raw_json FROM raw_cmms_record",
            "SELECT raw_json, COUNT(*) FROM raw_cmms_record GROUP BY raw_json",
            "SELECT * FROM (SELECT raw_record_id FROM raw_cmms_record ORDER BY raw_json)",
        ):
            with self.subTest(statement=statement):
                with self.assertRaises(SchemaServiceError) as ctx:
                    self.service.run_query(statement)
                self.assertIn("sort, group or de-duplicate", str(ctx.exception).lower())

    def test_pipeline_bounds_oversized_batch_values(self):
        # import_batch columns are nominally scalars, but SQLite types are
        # dynamic: a legacy or hand-edited row can hold a huge string, and this
        # read does not go through _collect_rows. Truncating in SQL bounds it on
        # every runtime, including one without setlimit.
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT INTO import_batch (source_system, status, raw_row_count) VALUES (?, ?, ?)",
                ("L" * 3_000_000, "COMPLETED", 7),
            )
        batches = self.service.pipeline()["recent_batches"]
        self.assertTrue(batches)
        longest = max(len(str(value)) for batch in batches for value in batch.values())
        self.assertLessEqual(longest, MAX_CELL_CHARS + 100)
        # Numeric columns keep their type through the CASE/substr wrapper.
        self.assertIsInstance(batches[0]["import_batch_id"], int)
        self.assertIsInstance(batches[0]["raw_row_count"], int)

    def test_view_row_count_skips_materialising_plans(self):
        # A view whose definition sorts makes COUNT(*) run that sort, which
        # materialises its values -- and it would fire just from opening the
        # Schema panel. Counting a plain table or view is unaffected.
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("CREATE VIEW v_sorted AS SELECT raw_record_id, raw_json FROM raw_cmms_record ORDER BY raw_json")
            conn.execute("CREATE VIEW v_plain AS SELECT raw_record_id FROM raw_cmms_record")
        by_name = {entry["name"]: entry for entry in self.service.tables()}
        self.assertIsNone(by_name["v_sorted"]["row_count"], "a materialising view was counted anyway")
        self.assertEqual(by_name["v_plain"]["row_count"], 2)
        self.assertEqual(by_name["raw_cmms_record"]["row_count"], 2)

    def test_index_satisfied_sorts_are_allowed(self):
        payload = self.service.run_query(
            "SELECT raw_record_id FROM raw_cmms_record ORDER BY raw_record_id"
        )
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
    def test_invalid_utf8_text_does_not_break_a_table(self):
        # sqlite3 decodes TEXT strictly by default and fails the whole statement,
        # so one legacy-encoded row would make the table unbrowsable. The raw
        # tables came from an Excel/CSV importer, so this is realistic data.
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT INTO raw_cmms_record (import_batch_id, source_record_id, raw_json) "
                "VALUES (1, 'latin1', CAST(? AS TEXT))",
                (b'{"name":"Pumpe f\xe4r K\xfchlung"}',),
            )
        payload = self.service.table_rows("raw_cmms_record")
        self.assertEqual(len(payload["rows"]), 3)
        text = self.service.run_query(
            "SELECT raw_json FROM raw_cmms_record WHERE source_record_id = 'latin1'"
        )["rows"][0][0]
        self.assertIn("�", text)
        self.assertIn("Pumpe", text)

    def test_run_query_returns_rows(self):
        payload = self.service.run_query("SELECT COUNT(*) AS n FROM raw_cmms_record")
        self.assertEqual(payload["columns"], ["n"])
        self.assertEqual(payload["rows"], [[2]])

    def test_run_query_caps_rows(self):
        payload = self.service.run_query("SELECT * FROM raw_cmms_record", max_rows=1)
        self.assertEqual(payload["row_count"], 1)
        self.assertTrue(payload["truncated"])

    def test_non_finite_floats_are_rendered_as_text(self):
        # Bare Infinity/NaN tokens are not valid JSON, so the browser's strict
        # JSON.parse would reject the entire successful response.
        self.assertEqual(self.service.run_query("SELECT 1e999 AS v")["rows"], [["Infinity"]])
        self.assertEqual(self.service.run_query("SELECT -1e999 AS v")["rows"], [["-Infinity"]])
        self.assertEqual(self.service.run_query("SELECT 1.5 AS v")["rows"], [[1.5]])

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
        self.assertIn("availability_results", extra_names)
        self.assertNotIn("availability_asset_groups", extra_names)

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

    def test_failed_reference_build_is_retried_but_rate_limited(self):
        # A transient failure (unwritable temp dir, full disk) must not leave the
        # Drift panel degraded until someone restarts GREMLIN -- but retrying on
        # every request would make a persistent failure pay the full bootstrap
        # cost each time.
        schema_service.reset_reference_schema_cache()
        self.addCleanup(schema_service.reset_reference_schema_cache)

        calls = []
        real_mkdtemp = schema_service.tempfile.mkdtemp

        def _failing_mkdtemp(*args, **kwargs):
            calls.append(1)
            raise OSError("simulated: read-only filesystem")

        with mock.patch.object(schema_service.tempfile, "mkdtemp", _failing_mkdtemp):
            first = schema_service._reference_schema()
            second = schema_service._reference_schema()
        self.assertTrue(first["error"])
        self.assertEqual(len(calls), 1, "a failure inside the retry window was rebuilt")

        # Past the retry window it tries again, and a now-healthy build succeeds.
        with mock.patch.object(schema_service, "_REFERENCE_RETRY_SECONDS", 0.0):
            with mock.patch.object(schema_service.tempfile, "mkdtemp", real_mkdtemp):
                recovered = schema_service._reference_schema()
        self.assertIsNone(recovered["error"])
        self.assertIn("raw_cmms_record", recovered["tables"])

    def test_failed_reference_build_does_not_report_false_orphans(self):
        # An empty reference table list must not be read as "nothing is in the
        # code", which would brand core tables like raw_cmms_record as orphaned.
        import services.schema_service as module

        module.reset_reference_schema_cache()
        module._REFERENCE_CACHE = {"tables": [], "columns": {}, "error": "simulated build failure"}
        # Inside the retry window, so the cached failure is not rebuilt.
        module._REFERENCE_FAILED_AT = time.monotonic()
        self.addCleanup(module.reset_reference_schema_cache)

        by_name = {table["name"]: table for table in self.service.tables()}
        self.assertEqual(by_name["raw_cmms_record"]["origin"], "unknown")
        self.assertEqual(by_name["mapped_cmms_record"]["origin"], "unknown")
        self.assertNotIn("orphaned", {table["origin"] for table in by_name.values()})
        self.assertIn("could not be compared", by_name["raw_cmms_record"]["note"].lower())

        drift = self.service.drift_report()
        self.assertFalse(drift["available"])


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
