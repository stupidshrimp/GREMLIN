import hashlib
import json
import sqlite3
import tempfile
import zipfile
import unittest
from pathlib import Path
from unittest.mock import patch

from services.life_data_service import DEFAULT_DB_PATH, DEFAULT_DB_PATH_CANDIDATES, ExcelValidation, LifeDataService, resolve_default_db_path


class LifeDataExcelParsingTests(unittest.TestCase):
    def setUp(self):
        self.service = LifeDataService.__new__(LifeDataService)

    def test_optional_int_accepts_excel_numeric_strings(self):
        self.assertEqual(self.service._excel_optional_int("42"), 42)
        self.assertEqual(self.service._excel_optional_int("42.0"), 42)
        self.assertEqual(self.service._excel_optional_int(42.0), 42)
        self.assertEqual(self.service._excel_optional_int("1,234"), 1234)

    def test_optional_int_ignores_text_values(self):
        self.assertIsNone(self.service._excel_optional_int("Bearing Failure"))
        self.assertIsNone(self.service._excel_optional_int(""))
        self.assertIsNone(self.service._excel_optional_int(None))

    def test_required_int_reports_field_context(self):
        self.assertEqual(self.service._excel_required_int("7.0", "mapped_record_id", 2), 7)
        with self.assertRaisesRegex(ValueError, "row 3.*mapped_record_id.*whole number"):
            self.service._excel_required_int("ABC", "mapped_record_id", 3)

    def test_xlsx_numeric_cells_round_trip_as_numbers(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "workbook.xlsx"
            self.service._write_xlsx(path, [["mapped_record_id", "failure_mode"], [12, "Pump"]], "Test")
            rows = self.service._read_xlsx(path)
        self.assertEqual(rows[1][0], 12)
        self.assertEqual(rows[1][1], "Pump")

    def test_xlsx_writes_dropdown_validations_and_hidden_lookup_sheet(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "workbook.xlsx"
            self.service._write_xlsx(
                path,
                [["mapped_record_id", "disposition_category"], [12, "UNKNOWN"]],
                "Test",
                validations=[
                    ExcelValidation(
                        column_name="disposition_category",
                        validation_type="list",
                        formula1="'Lookup Lists'!$A$2:$A$3",
                        error="Select from the dropdown.",
                    ),
                ],
                lookup_rows=[["disposition_category"], ["UNKNOWN"], ["INCLUDED_FAILURE"]],
            )
            with zipfile.ZipFile(path) as workbook:
                sheet_xml = workbook.read("xl/worksheets/sheet1.xml").decode()
                workbook_xml = workbook.read("xl/workbook.xml").decode()

        self.assertIn("dataValidations", sheet_xml)
        self.assertIn("'Lookup Lists'!$A$2:$A$3", sheet_xml)
        self.assertIn('state="hidden"', workbook_xml)

    def test_xlsx_dropdown_workbook_still_reads_first_sheet_values(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "workbook.xlsx"
            self.service._write_xlsx(
                path,
                [["mapped_record_id", "disposition_category"], [12, "UNKNOWN"]],
                "Test",
                validations=[
                    ExcelValidation(
                        column_name="disposition_category",
                        validation_type="list",
                        formula1="'Lookup Lists'!$A$2:$A$2",
                        error="Select from the dropdown.",
                    ),
                ],
                lookup_rows=[["disposition_category"], ["UNKNOWN"]],
            )
            rows = self.service._read_xlsx(path)

        self.assertEqual(rows[1][0], 12)
        self.assertEqual(rows[1][1], "UNKNOWN")

    def test_default_database_candidates_try_known_z_paths_before_other_drives(self):
        self.assertEqual(
            [str(candidate) for candidate in DEFAULT_DB_PATH_CANDIDATES[:4]],
            [
                r"Z:\Facilities\FACIL\MAIN-ENG\901 Reliability Projects\901 Reliability Projects\Weibull Data\Database\GREMLIN.db",
                r"Z:\FACIL\MAIN-ENG\901 Reliability Projects\901 Reliability Projects\Weibull Data\Database\GREMLIN.db",
                r"A:\Facilities\FACIL\MAIN-ENG\901 Reliability Projects\901 Reliability Projects\Weibull Data\Database\GREMLIN.db",
                r"A:\FACIL\MAIN-ENG\901 Reliability Projects\901 Reliability Projects\Weibull Data\Database\GREMLIN.db",
            ],
        )
        self.assertEqual(
            str(DEFAULT_DB_PATH_CANDIDATES[-1]),
            r"\\sandc.ws\depts\Facilities\FACIL\MAIN-ENG\901 Reliability Projects\901 Reliability Projects\Weibull Data\Database\GREMLIN.db",
        )
        self.assertEqual(len(DEFAULT_DB_PATH_CANDIDATES), 53)

    def test_default_database_resolution_uses_first_reachable_mapped_drive(self):
        reachable_path = DEFAULT_DB_PATH_CANDIDATES[5]

        def fake_is_file(path: Path) -> bool:
            return path == reachable_path

        with patch.object(Path, "is_file", fake_is_file):
            self.assertEqual(resolve_default_db_path(DEFAULT_DB_PATH), reachable_path)

    def test_default_database_resolution_can_use_unc_path_when_mapped_drives_are_missing(self):
        reachable_path = DEFAULT_DB_PATH_CANDIDATES[-1]

        def fake_is_file(path: Path) -> bool:
            return path == reachable_path

        with patch.object(Path, "is_file", fake_is_file):
            self.assertEqual(resolve_default_db_path(DEFAULT_DB_PATH), reachable_path)

    def test_default_database_resolution_skips_inaccessible_mapped_drives(self):
        inaccessible_path = DEFAULT_DB_PATH_CANDIDATES[0]
        reachable_path = DEFAULT_DB_PATH_CANDIDATES[3]

        def fake_is_file(path: Path) -> bool:
            if path == inaccessible_path:
                raise PermissionError("mapped drive is not accessible")
            return path == reachable_path

        with patch.object(Path, "is_file", fake_is_file):
            self.assertEqual(resolve_default_db_path(DEFAULT_DB_PATH), reachable_path)

    def test_default_database_resolution_leaves_explicit_paths_unchanged(self):
        explicit_path = Path("custom") / "GREMLIN.db"

        with patch.object(Path, "is_file", return_value=True):
            self.assertEqual(resolve_default_db_path(explicit_path), explicit_path)

    def test_life_data_service_preserves_explicit_path_for_session_reuse(self):
        session_path = DEFAULT_DB_PATH_CANDIDATES[4]
        higher_priority_path = DEFAULT_DB_PATH_CANDIDATES[0]

        def session_path_is_file(path: Path) -> bool:
            return path == session_path

        def higher_priority_path_is_file(path: Path) -> bool:
            return path == higher_priority_path

        with patch.object(Path, "is_file", session_path_is_file), patch.object(LifeDataService, "ensure_schema"):
            startup_service = LifeDataService(refresh_on_startup=False)

        with patch.object(Path, "is_file", higher_priority_path_is_file), patch.object(LifeDataService, "ensure_schema"):
            reused_service = LifeDataService(startup_service.db_path, refresh_on_startup=False)

        self.assertEqual(startup_service.db_path, session_path)
        self.assertEqual(reused_service.db_path, session_path)

    def test_service_connections_close_after_context_exit(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            service = LifeDataService(Path(temp_dir) / "GREMLIN.db", refresh_on_startup=False)
            conn = service.connect()
            with conn:
                self.assertEqual(conn.execute("SELECT 1").fetchone()[0], 1)

            with self.assertRaises(sqlite3.ProgrammingError):
                conn.execute("SELECT 1")

    def test_refresh_mapped_cmms_records_invalidates_cached_asset_options_for_external_mappings(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            service = LifeDataService(Path(temp_dir) / "GREMLIN.db", refresh_on_startup=False)
            with service.connect() as conn:
                conn.executescript("""
                    CREATE TABLE IF NOT EXISTS import_batch (import_batch_id INTEGER PRIMARY KEY);
                    CREATE TABLE IF NOT EXISTS raw_cmms_record (raw_record_id INTEGER PRIMARY KEY, import_batch_id INTEGER, raw_json TEXT);
                    INSERT INTO import_batch(import_batch_id) VALUES (1);
                    INSERT INTO raw_cmms_record(raw_record_id, import_batch_id, raw_json) VALUES
                        (1, 1, '{"taskID":"T1","name":"Task 1","Asset Number":"A-100","Asset Name":"Asset A","type":"Work Order","status":"Complete"}');
                """)

            self.assertEqual(service.refresh_mapped_cmms_records(), 1)
            self.assertEqual([row["asset_number"] for row in service.asset_number_options()], ["A-100"])

            external_raw_json = '{"taskID":"T2","name":"Task 2","Asset Number":"B-200","Asset Name":"Asset B","type":"Work Order","status":"Complete"}'
            external_raw_hash = hashlib.sha256(external_raw_json.encode("utf-8", errors="replace")).hexdigest()
            with service.connect() as conn:
                conn.execute(
                    "INSERT INTO raw_cmms_record(raw_record_id, import_batch_id, raw_json) VALUES (?, ?, ?)",
                    (2, 1, external_raw_json),
                )
                conn.execute(
                    """
                    INSERT INTO mapped_cmms_record(raw_record_id, raw_content_hash, import_batch_id, asset_number, asset_name, mapping_version)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (2, external_raw_hash, 1, "B-200", "Asset B", "v1"),
                )

            self.assertEqual(service.refresh_mapped_cmms_records(), 0)
            self.assertEqual([row["asset_number"] for row in service.asset_number_options()], ["A-100", "B-200"])


    def test_refresh_mapped_cmms_records_skips_unchanged_raw_json(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            service = LifeDataService(Path(temp_dir) / "GREMLIN.db", refresh_on_startup=False)
            with service.connect() as conn:
                conn.executescript("""
                    CREATE TABLE IF NOT EXISTS import_batch (import_batch_id INTEGER PRIMARY KEY);
                    CREATE TABLE IF NOT EXISTS raw_cmms_record (raw_record_id INTEGER PRIMARY KEY, import_batch_id INTEGER, raw_json TEXT);
                    INSERT INTO import_batch(import_batch_id) VALUES (1);
                    INSERT INTO raw_cmms_record(raw_record_id, import_batch_id, raw_json) VALUES
                        (1, 1, '{"taskID":"T1","name":"Task 1","Asset Number":"A-100","type":"Work Order","status":"Complete"}');
                """)

            self.assertEqual(service.refresh_mapped_cmms_records(), 1)
            self.assertEqual(service.refresh_mapped_cmms_records(), 0)
            with service.connect() as conn:
                conn.execute(
                    "UPDATE raw_cmms_record SET raw_json = ? WHERE raw_record_id = 1",
                    ('{"taskID":"T1","name":"Task 1 changed","Asset Number":"A-100","type":"Work Order","status":"Complete"}',),
                )

            self.assertEqual(service.refresh_mapped_cmms_records(), 1)
            with service.connect() as conn:
                row = conn.execute("SELECT task_name, raw_content_hash FROM mapped_cmms_record WHERE raw_record_id = 1").fetchone()
                self.assertEqual(row["task_name"], "Task 1 changed")
                self.assertTrue(row["raw_content_hash"])


    def test_migration_backfills_downtime_for_existing_v1_mapped_records(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "GREMLIN.db"
            raw_payload = json.dumps({"taskID": "T1", "Asset Number": "A-100", "downtime": "2 hr"})
            raw_hash = hashlib.sha256(raw_payload.encode("utf-8")).hexdigest()
            raw_payload_without_downtime = json.dumps({"taskID": "T2", "Asset Number": "A-100"})
            raw_hash_without_downtime = hashlib.sha256(raw_payload_without_downtime.encode("utf-8")).hexdigest()
            with sqlite3.connect(db_path) as conn:
                conn.executescript("""
                    CREATE TABLE import_batch (import_batch_id INTEGER PRIMARY KEY);
                    CREATE TABLE raw_cmms_record (raw_record_id INTEGER PRIMARY KEY, raw_json TEXT);
                    CREATE TABLE mapped_cmms_record (
                        mapped_record_id INTEGER PRIMARY KEY,
                        raw_record_id INTEGER NOT NULL,
                        raw_content_hash TEXT,
                        import_batch_id INTEGER NOT NULL,
                        asset_number TEXT,
                        task_id TEXT,
                        record_class_auto TEXT NOT NULL DEFAULT 'UNKNOWN',
                        record_class_final TEXT,
                        is_pm_candidate INTEGER NOT NULL DEFAULT 0,
                        is_corrective_wo_candidate INTEGER NOT NULL DEFAULT 0,
                        completed_date_final TEXT,
                        start_date_final TEXT,
                        created_date_final TEXT,
                        mapping_version TEXT NOT NULL DEFAULT 'v1'
                    );
                    INSERT INTO import_batch(import_batch_id) VALUES (1);
                """)
                conn.execute("INSERT INTO raw_cmms_record(raw_record_id, raw_json) VALUES (1, ?)", (raw_payload,))
                conn.execute("INSERT INTO raw_cmms_record(raw_record_id, raw_json) VALUES (2, ?)", (raw_payload_without_downtime,))
                conn.execute(
                    """
                    INSERT INTO mapped_cmms_record(
                        mapped_record_id, raw_record_id, raw_content_hash, import_batch_id, asset_number, task_id,
                        record_class_auto, record_class_final, mapping_version
                    ) VALUES (1, 1, ?, 1, 'A-100', 'T1', 'CORRECTIVE_WO', 'CORRECTIVE_WO', 'v1')
                    """,
                    (raw_hash,),
                )
                conn.execute(
                    """
                    INSERT INTO mapped_cmms_record(
                        mapped_record_id, raw_record_id, raw_content_hash, import_batch_id, asset_number, task_id,
                        record_class_auto, record_class_final, mapping_version
                    ) VALUES (2, 2, ?, 1, 'A-100', 'T2', 'CORRECTIVE_WO', 'CORRECTIVE_WO', 'v1')
                    """,
                    (raw_hash_without_downtime,),
                )

            service = LifeDataService(db_path, refresh_on_startup=False)
            with service.connect() as conn:
                mapped_columns = {row["name"] for row in conn.execute("PRAGMA table_info(mapped_cmms_record)").fetchall()}
                row = conn.execute(
                    "SELECT downtime_raw, downtime_minutes, downtime_hours, downtime_backfill_attempted FROM mapped_cmms_record WHERE mapped_record_id = 1"
                ).fetchone()
                row_without_downtime = conn.execute(
                    "SELECT downtime_raw, downtime_minutes, downtime_hours, downtime_backfill_attempted FROM mapped_cmms_record WHERE mapped_record_id = 2"
                ).fetchone()
                failure_mode_id = int(conn.execute("INSERT INTO failure_mode(failure_mode_name) VALUES ('Pump')").lastrowid)
                failure_mechanism_id = int(
                    conn.execute(
                        "INSERT INTO failure_mechanism(failure_mechanism_name, failure_mode_id) VALUES ('Seal', ?)",
                        (failure_mode_id,),
                    ).lastrowid
                )
                conn.execute(
                    """
                    INSERT INTO event_disposition(
                        mapped_record_id, record_class_final, disposition_category, include_in_event_processing,
                        include_in_weibull_candidate, failure_mode_id, failure_mechanism_id, is_current
                    ) VALUES (1, 'CORRECTIVE_WO', 'INCLUDED_FAILURE', 1, 1, ?, ?, 1)
                    """,
                    (failure_mode_id, failure_mechanism_id),
                )

            self.assertIn("downtime_raw", mapped_columns)
            self.assertIn("downtime_minutes", mapped_columns)
            self.assertIn("downtime_hours", mapped_columns)
            self.assertIn("downtime_backfill_attempted", mapped_columns)
            self.assertEqual(row["downtime_raw"], "2 hr")
            self.assertEqual(row["downtime_minutes"], 120)
            self.assertEqual(row["downtime_hours"], 2)
            self.assertEqual(row["downtime_backfill_attempted"], 1)
            self.assertIsNone(row_without_downtime["downtime_raw"])
            self.assertIsNone(row_without_downtime["downtime_minutes"])
            self.assertIsNone(row_without_downtime["downtime_hours"])
            self.assertEqual(row_without_downtime["downtime_backfill_attempted"], 1)
            with service.write_connection() as conn:
                self.assertEqual(service._backfill_mapped_downtime_from_raw(conn), 0)
            self.assertEqual(service.refresh_mapped_cmms_records(), 0)
            pareto = service.failure_mechanism_pareto("A-100")
            self.assertEqual(len(pareto), 1)
            self.assertEqual(pareto[0]["failure_count"], 1)
            self.assertEqual(pareto[0]["downtime_hours"], 2)

    def test_import_disposition_excel_skips_unchanged_rows(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            service = LifeDataService(Path(temp_dir) / "GREMLIN.db", refresh_on_startup=False)
            with service.connect() as conn:
                conn.executescript("""
                    CREATE TABLE IF NOT EXISTS import_batch (import_batch_id INTEGER PRIMARY KEY);
                    CREATE TABLE IF NOT EXISTS raw_cmms_record (raw_record_id INTEGER PRIMARY KEY);
                    INSERT INTO import_batch(import_batch_id) VALUES (1);
                    INSERT INTO raw_cmms_record(raw_record_id) VALUES (1);
                """)
                conn.execute(
                    """
                    INSERT INTO mapped_cmms_record(
                        raw_record_id, import_batch_id, task_id, task_name, asset_number, completed_date_final,
                        record_class_auto, is_corrective_wo_candidate
                    ) VALUES (1, 1, 'T1', 'Task 1', 'A-100', '2024-01-01', 'CORRECTIVE_WO', 1)
                    """
                )

            service.save_disposition(
                1,
                kind="wo",
                disposition_category="EXCLUDED_NON_FAILURE",
                disposition_text="Already reviewed",
                include_in_weibull_candidate=False,
            )
            workbook = Path(temp_dir) / "dispositions.xlsx"
            service.export_disposition_excel("A-100", "wo", workbook)

            self.assertEqual(service.import_disposition_excel("A-100", "wo", workbook), 0)
            with service.connect() as conn:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM event_disposition").fetchone()[0], 1)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM event_disposition WHERE is_current = 1").fetchone()[0], 1)

    def test_import_disposition_excel_resolves_duplicate_mechanism_name_by_mode(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            service = LifeDataService(Path(temp_dir) / "GREMLIN.db", refresh_on_startup=False)
            with service.connect() as conn:
                conn.executescript("""
                    CREATE TABLE IF NOT EXISTS import_batch (import_batch_id INTEGER PRIMARY KEY);
                    CREATE TABLE IF NOT EXISTS raw_cmms_record (raw_record_id INTEGER PRIMARY KEY);
                    INSERT INTO import_batch(import_batch_id) VALUES (1);
                    INSERT INTO raw_cmms_record(raw_record_id) VALUES (1);
                """)
                conn.execute(
                    """
                    INSERT INTO mapped_cmms_record(
                        raw_record_id, import_batch_id, task_id, task_name, asset_number, completed_date_final,
                        record_class_auto, is_corrective_wo_candidate
                    ) VALUES (1, 1, 'T1', 'Task 1', 'A-100', '2024-01-01', 'CORRECTIVE_WO', 1)
                    """
                )
                pump_mode_id = int(conn.execute("INSERT INTO failure_mode(failure_mode_name) VALUES ('Pump')").lastrowid)
                motor_mode_id = int(conn.execute("INSERT INTO failure_mode(failure_mode_name) VALUES ('Motor')").lastrowid)
                pump_seal_id = int(conn.execute("INSERT INTO failure_mechanism(failure_mechanism_name, failure_mode_id) VALUES ('Seal', ?)", (pump_mode_id,)).lastrowid)
                motor_seal_id = int(conn.execute("INSERT INTO failure_mechanism(failure_mechanism_name, failure_mode_id) VALUES ('Seal', ?)", (motor_mode_id,)).lastrowid)
                conn.execute("INSERT INTO asset_failure_mode_option(asset_number, failure_mode_id) VALUES ('A-100', ?)", (pump_mode_id,))
                conn.execute("INSERT INTO asset_failure_mode_option(asset_number, failure_mode_id) VALUES ('A-100', ?)", (motor_mode_id,))
                conn.execute(
                    "INSERT INTO asset_failure_mechanism_option(asset_number, failure_mechanism_id, failure_mode_id, use_count) VALUES ('A-100', ?, ?, 1)",
                    (pump_seal_id, pump_mode_id),
                )
                conn.execute(
                    "INSERT INTO asset_failure_mechanism_option(asset_number, failure_mechanism_id, failure_mode_id, use_count) VALUES ('A-100', ?, ?, 10)",
                    (motor_seal_id, motor_mode_id),
                )

            workbook = Path(temp_dir) / "duplicate_mechanism_name.xlsx"
            service._write_xlsx(
                workbook,
                [
                    [
                        "mapped_record_id",
                        "disposition_category",
                        "record_class",
                        "include_in_weibull_candidate",
                        "failure_mode_id",
                        "failure_mode",
                        "failure_mechanism_id",
                        "failure_mechanism",
                    ],
                    [1, "INCLUDED_FAILURE", "CORRECTIVE_WO", True, motor_mode_id, "Motor", None, "Seal"],
                ],
                "WO Dispositions",
            )

            self.assertEqual(service.import_disposition_excel("A-100", "wo", workbook), 1)
            with service.connect() as conn:
                row = conn.execute(
                    """
                    SELECT d.failure_mode_id, d.failure_mechanism_id, mp.failure_mechanism_id AS population_mechanism_id
                    FROM event_disposition d
                    JOIN modeled_population mp ON mp.modeled_population_id = d.modeled_population_id
                    WHERE d.mapped_record_id = 1 AND d.is_current = 1
                    """
                ).fetchone()

            self.assertEqual(row["failure_mode_id"], motor_mode_id)
            self.assertEqual(row["failure_mechanism_id"], motor_seal_id)
            self.assertEqual(row["population_mechanism_id"], motor_seal_id)

    def test_failure_mechanism_upsert_reuses_existing_name_with_unique_name_index(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            service = LifeDataService(Path(temp_dir) / "GREMLIN.db", refresh_on_startup=False)
            with service.connect() as conn:
                first_mode = service._upsert_failure_mode_for_asset(conn, "A-100", "Pump", None)
                second_mode = service._upsert_failure_mode_for_asset(conn, "A-100", "Motor", None)
                existing_id = service._upsert_failure_mechanism_for_asset(conn, "A-100", "Bearing Wear", first_mode, None)
                conn.execute("CREATE UNIQUE INDEX ux_test_failure_mechanism_name ON failure_mechanism(failure_mechanism_name)")
                reused_id = service._upsert_failure_mechanism_for_asset(conn, "A-100", "Bearing Wear", second_mode, None)

        self.assertEqual(reused_id, existing_id)

    def test_failure_mechanism_lookup_prefers_mode_then_existing_name(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            service = LifeDataService(Path(temp_dir) / "GREMLIN.db", refresh_on_startup=False)
            with service.connect() as conn:
                first_mode = service._upsert_failure_mode_for_asset(conn, "A-100", "Pump", None)
                second_mode = service._upsert_failure_mode_for_asset(conn, "A-100", "Motor", None)
                first_id = service._upsert_failure_mechanism_for_asset(conn, "A-100", "Seal Leak", first_mode, None)
                self.assertEqual(service._lookup_failure_mechanism_id(conn, "Seal Leak", first_mode), first_id)
                self.assertEqual(service._lookup_failure_mechanism_id(conn, "Seal Leak", second_mode), first_id)

    def test_disposition_rows_support_pagination_and_scoped_counts(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            service = LifeDataService(Path(temp_dir) / "GREMLIN.db", refresh_on_startup=False)
            with service.connect() as conn:
                conn.executescript("""
                    CREATE TABLE IF NOT EXISTS import_batch (import_batch_id INTEGER PRIMARY KEY);
                    CREATE TABLE IF NOT EXISTS raw_cmms_record (raw_record_id INTEGER PRIMARY KEY);
                    INSERT INTO import_batch(import_batch_id) VALUES (1);
                    INSERT INTO raw_cmms_record(raw_record_id) VALUES (1), (2), (3);
                """)
                for index in range(1, 4):
                    conn.execute(
                        """
                        INSERT INTO mapped_cmms_record(
                            raw_record_id, import_batch_id, task_id, task_name, asset_number, completed_date_final,
                            start_date_final, record_class_auto, is_corrective_wo_candidate
                        ) VALUES (?, 1, ?, ?, 'A-100', ?, ?, 'CORRECTIVE_WO', 1)
                        """,
                        (
                            index,
                            f"T{index}",
                            f"Task {index}",
                            None if index == 2 else f"2024-01-0{index}",
                            f"2024-01-0{index}",
                        ),
                    )
                mode_id = service._upsert_failure_mode_for_asset(conn, "A-100", "Pump", None)
                mechanism_id = service._upsert_failure_mechanism_for_asset(conn, "A-100", "Seal", mode_id, None)
                conn.execute(
                    """
                    INSERT INTO event_disposition(
                        mapped_record_id, record_class_final, disposition_category, failure_mode_id,
                        failure_mechanism_id, include_in_event_processing, include_in_weibull_candidate, is_current
                    ) VALUES (1, 'CORRECTIVE_WO', 'INCLUDED_FAILURE', ?, ?, 1, 1, 1)
                    """,
                    (mode_id, mechanism_id),
                )

            self.assertEqual(service.disposition_row_count("A-100", "wo"), 3)
            self.assertEqual(service.disposition_row_count("A-100", "wo", only_needing_disposition=True), 2)
            fallback_row = service.disposition_rows("A-100", "wo", limit=1, offset=1)[0]
            self.assertEqual(fallback_row["taskID"], "T2")
            self.assertEqual(fallback_row["weibullEventDate_Final"], "2024-01-02")
            self.assertEqual(fallback_row["weibullEventDate_Source"], "startDate_Final")
            self.assertEqual(
                [row["taskID"] for row in service.disposition_rows("A-100", "wo", only_needing_disposition=True, limit=2, offset=0)],
                ["T2", "T3"],
            )

    def test_asset_number_options_include_best_asset_name(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            service = LifeDataService(Path(temp_dir) / "GREMLIN.db", refresh_on_startup=False)
            with service.connect() as conn:
                conn.executescript("""
                    CREATE TABLE IF NOT EXISTS import_batch (import_batch_id INTEGER PRIMARY KEY);
                    CREATE TABLE IF NOT EXISTS raw_cmms_record (raw_record_id INTEGER PRIMARY KEY);
                    INSERT INTO import_batch(import_batch_id) VALUES (1);
                    INSERT INTO raw_cmms_record(raw_record_id) VALUES (1), (2), (3);
                """)
                conn.execute("""
                    INSERT INTO mapped_cmms_record(raw_record_id, import_batch_id, asset_number, asset_name)
                    VALUES (1, 1, 'A-100', 'Main Pump'), (2, 1, 'A-100', 'Main Pump'), (3, 1, 'B-200', 'Conveyor')
                """)

            self.assertEqual(
                service.asset_number_options(),
                [
                    {"asset_number": "A-100", "asset_name": "Main Pump"},
                    {"asset_number": "B-200", "asset_name": "Conveyor"},
                ],
            )


    def test_repeated_weibull_analysis_rebuilds_population_without_foreign_key_failure(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            service = LifeDataService(Path(temp_dir) / "GREMLIN.db", refresh_on_startup=False)
            with service.connect() as conn:
                conn.executescript("""
                    CREATE TABLE IF NOT EXISTS import_batch (import_batch_id INTEGER PRIMARY KEY);
                    CREATE TABLE IF NOT EXISTS raw_cmms_record (raw_record_id INTEGER PRIMARY KEY);
                    INSERT INTO import_batch(import_batch_id) VALUES (1);
                    INSERT INTO raw_cmms_record(raw_record_id) VALUES (1), (2), (3);
                """)
                for index, completed in enumerate(("2024-01-01", "2024-02-01", "2024-03-01"), start=1):
                    conn.execute(
                        """
                        INSERT INTO mapped_cmms_record(
                            raw_record_id, import_batch_id, task_id, task_name, asset_number, completed_date_final,
                            start_date_final, record_class_auto, is_corrective_wo_candidate
                        ) VALUES (?, 1, ?, ?, 'A-100', ?, ?, 'CORRECTIVE_WO', 1)
                        """,
                        (index, f"T{index}", f"Failure {index}", completed, completed),
                    )

            mode_id = service._asset_failure_mode_id("A-100", "Pump")
            self.assertIsNone(mode_id)
            for mapped_record_id in (1, 2, 3):
                service.save_disposition(
                    mapped_record_id,
                    kind="wo",
                    disposition_category="INCLUDED_FAILURE",
                    failure_mode_text="Pump",
                    failure_mechanism_text="Seal Leak",
                )

            mode_id = service._asset_failure_mode_id("A-100", "Pump")
            mechanism_id = service._asset_failure_mechanism_id("A-100", "Seal Leak")
            self.assertIsNotNone(mode_id)
            self.assertIsNotNone(mechanism_id)

            first_result = service.perform_weibull_analysis(
                "A-100",
                grouping_level="FAILURE_MECHANISM",
                failure_mode_id=mode_id,
                failure_mechanism_id=mechanism_id,
            )
            second_result = service.perform_weibull_analysis(
                "A-100",
                grouping_level="FAILURE_MECHANISM",
                failure_mode_id=mode_id,
                failure_mechanism_id=mechanism_id,
            )

            self.assertGreater(first_result.result_id, 0)
            self.assertGreater(second_result.result_id, 0)
            self.assertEqual(second_result.total_observation_count, first_result.total_observation_count)
            with service.connect() as conn:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM analysis_dataset").fetchone()[0], 1)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM weibull_analysis_run").fetchone()[0], 1)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM weibull_observation").fetchone()[0], len(second_result.observations))

    def test_weibull_interpretation_summary_uses_requested_recommendation_logic(self):
        summary = self.service._weibull_interpretation_summary(
            beta=1.2,
            eta=1000.0,
            mean_life=940.0,
            beta_lo=1.15,
            beta_hi=1.25,
            eta_lo=900.0,
            eta_hi=1100.0,
        )

        self.assertIn("wear-out behavior", summary[0]["recommendation"])
        self.assertIn("planning reference", summary[1]["recommendation"])
        self.assertIn("high-level planning number", summary[2]["recommendation"])
        self.assertIn("relatively tight", summary[3]["recommendation"])
        self.assertIn("reasonably tight", summary[4]["recommendation"])

    def test_weibull_confidence_intervals_are_positive_when_estimable(self):
        data = [(100.0, 1), (150.0, 1), (220.0, 1), (260.0, 0), (310.0, 1)]
        beta, eta, _ = self.service._fit_weibull_2p(data)
        beta_lo, beta_hi, eta_lo, eta_hi = self.service._weibull_confidence_intervals(data, beta, eta)

        self.assertIsNotNone(beta_lo)
        self.assertIsNotNone(beta_hi)
        self.assertIsNotNone(eta_lo)
        self.assertIsNotNone(eta_hi)
        self.assertLess(beta_lo, beta_hi)
        self.assertLess(eta_lo, eta_hi)
        self.assertGreater(beta_lo, 0)
        self.assertGreater(eta_lo, 0)


if __name__ == "__main__":
    unittest.main()
