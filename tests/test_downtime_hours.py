import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from services.ingestion_service import IngestionService
from services.life_data_service import LifeDataService


class _DummyClient:
    pass


class _DummyRepo:
    db_path = ":memory:"


class DowntimeHoursTests(unittest.TestCase):
    def test_ingestion_preserves_raw_downtime_and_adds_hour_derivatives(self):
        service = IngestionService(_DummyClient(), _DummyRepo(), fetch_assets=False, refresh_mapping=False, log=lambda _: None)

        record = service.transform({"taskID": 123, "assetID": 456, "downtime": 2.5}, asset_index=type("Empty", (), {"enrich": lambda *_: None})())

        self.assertEqual(record["downtime"], 2.5)
        self.assertEqual(record["downtime_source_value"], 2.5)
        self.assertEqual(record["downtime_source_unit"], "hours")
        self.assertEqual(record["downtime_hours"], 2.5)
        self.assertEqual(record["downtime_minutes"], 150.0)

    def test_mapping_treats_numeric_raw_downtime_as_hours(self):
        service = object.__new__(LifeDataService)

        mapped = service._map_raw_record({"taskID": 123, "downtime": 2.5})

        self.assertEqual(mapped["downtime_raw"], 2.5)
        self.assertEqual(mapped["downtime_hours"], 2.5)
        self.assertEqual(mapped["downtime_minutes"], 150.0)

    def test_explicit_minute_strings_still_convert_to_hours(self):
        service = object.__new__(LifeDataService)

        self.assertEqual(service._parse_downtime_hours("90 minutes"), 1.5)
        self.assertEqual(service._parse_downtime_hours("90min"), 1.5)
        self.assertAlmostEqual(service._parse_downtime_hours("30sec"), 30 / 3600.0)
        self.assertEqual(service._parse_downtime_hours("2 hr"), 2.0)

    def test_legacy_mapped_table_migrates_before_v2_remap(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "gremlin.db"
            conn = sqlite3.connect(db_path)
            conn.execute("CREATE TABLE import_batch (import_batch_id INTEGER PRIMARY KEY, status TEXT)")
            conn.execute(
                """
                CREATE TABLE raw_cmms_record (
                    raw_record_id INTEGER PRIMARY KEY,
                    import_batch_id INTEGER NOT NULL,
                    raw_json TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE mapped_cmms_record (
                    mapped_record_id INTEGER PRIMARY KEY,
                    raw_record_id INTEGER NOT NULL,
                    record_class_final TEXT,
                    mapping_version TEXT NOT NULL DEFAULT 'v1'
                )
                """
            )
            conn.execute("INSERT INTO import_batch(import_batch_id, status) VALUES (1, 'COMPLETED')")
            conn.execute(
                "INSERT INTO raw_cmms_record(raw_record_id, import_batch_id, raw_json) VALUES (1, 1, ?)",
                (json.dumps({"taskID": 123, "name": "Repair", "downtime": 2.5}),),
            )
            conn.execute(
                "INSERT INTO mapped_cmms_record(raw_record_id, record_class_final, mapping_version) VALUES (1, 'WO', 'v1')"
            )
            conn.commit()
            conn.close()

            service = LifeDataService(db_path, refresh_on_startup=False)

            self.assertEqual(service.refresh_mapped_cmms_records(), 1)
            with sqlite3.connect(db_path) as conn:
                row = conn.execute(
                    "SELECT task_name, downtime_hours, downtime_minutes, mapping_version FROM mapped_cmms_record WHERE raw_record_id = 1"
                ).fetchone()
            self.assertEqual(row, ("Repair", 2.5, 150.0, "v2"))


if __name__ == "__main__":
    unittest.main()
