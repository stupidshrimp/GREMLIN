import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from services.life_data_service import LifeDataService


class DowntimeConversionTests(unittest.TestCase):
    """Limble reports task downtime in seconds; the pipeline must land on hours.

    Regression guard for the bug where a bare numeric downtime was treated as
    minutes, inflating every displayed downtime figure by 60x (e.g. a 3.5 h
    repair, 12600 s, showed as 210 h).
    """

    def _service(self) -> LifeDataService:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        db_path = Path(self._tmp.name) / "gremlin.db"
        return LifeDataService(db_path, refresh_on_startup=False)

    def test_bare_seconds_convert_to_minutes(self):
        service = self._service()
        # 12600 s == 210 min == 3.5 h
        self.assertAlmostEqual(service._parse_downtime_minutes(12600), 210.0)
        self.assertAlmostEqual(service._parse_downtime_minutes(0), 0.0)
        self.assertIsNone(service._parse_downtime_minutes(None))
        self.assertIsNone(service._parse_downtime_minutes(""))

    def test_mapped_record_downtime_hours(self):
        service = self._service()
        mapped = service._map_raw_record({"taskID": 1, "downtime": 12600})
        self.assertAlmostEqual(mapped["downtime_minutes"], 210.0)
        self.assertAlmostEqual(mapped["downtime_hours"], 3.5)

    def test_legacy_prescaled_rows_are_not_rescaled(self):
        service = self._service()
        # Rows imported by the retired --downtime-unit=seconds ingestion path
        # stored `downtime` already in minutes and kept the original seconds in
        # provenance fields. The remap must not divide those by 60 again.
        mapped = service._map_raw_record(
            {
                "taskID": 2,
                "downtime": 210.0,  # already normalised to minutes
                "downtime_source_value": 12600,  # original seconds
                "downtime_source_unit": "seconds",
            }
        )
        self.assertAlmostEqual(mapped["downtime_minutes"], 210.0)
        self.assertAlmostEqual(mapped["downtime_hours"], 3.5)

    def test_stale_provenance_after_resync_is_ignored(self):
        service = self._service()
        # If a resync overwrote `downtime` with fresh raw seconds but stale
        # provenance survived, the consistency check must reject it (12600 !=
        # 12600 s -> 210 min) and normalise as seconds, not reinflate to 210 h.
        mapped = service._map_raw_record(
            {
                "taskID": 3,
                "downtime": 12600,  # fresh raw seconds
                "downtime_source_value": 12600,  # stale
                "downtime_source_unit": "seconds",  # stale
            }
        )
        self.assertAlmostEqual(mapped["downtime_minutes"], 210.0)
        self.assertAlmostEqual(mapped["downtime_hours"], 3.5)

    def test_version_bump_remaps_existing_rows_without_refresh_on_startup(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db_path = Path(tmp.name) / "gremlin.db"
        # First construction creates the mapped_cmms_record schema.
        LifeDataService(db_path, refresh_on_startup=False)
        with sqlite3.connect(db_path) as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS import_batch (import_batch_id INTEGER PRIMARY KEY, status TEXT)")
            conn.execute("INSERT INTO import_batch (import_batch_id, status) VALUES (0, 'COMPLETED')")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS raw_cmms_record ("
                "raw_record_id INTEGER PRIMARY KEY, import_batch_id INTEGER NOT NULL DEFAULT 0, "
                "source_record_id TEXT, raw_json TEXT NOT NULL, raw_content_hash TEXT)"
            )
            conn.execute(
                "INSERT INTO raw_cmms_record (raw_record_id, import_batch_id, raw_json) VALUES (1, 0, ?)",
                (json.dumps({"taskID": 1, "assetID": 7, "Asset Number": "7", "downtime": 12600}),),
            )
            # Simulate a pre-fix (v1) mapped row carrying the old 60x-inflated downtime.
            conn.execute(
                "INSERT INTO mapped_cmms_record (raw_record_id, import_batch_id, asset_number, "
                "downtime_raw, downtime_minutes, downtime_hours, mapping_version) "
                "VALUES (1, 0, '7', '12600', 12600, 210.0, 'v1')"
            )
            conn.commit()
        # A fresh construction with refresh_on_startup=False must still remap the
        # stale v1 row rather than leave the dashboards on the inflated value.
        LifeDataService(db_path, refresh_on_startup=False)
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT downtime_hours, mapping_version FROM mapped_cmms_record WHERE raw_record_id = 1"
            ).fetchone()
        self.assertEqual(row["mapping_version"], "v2")
        self.assertAlmostEqual(row["downtime_hours"], 3.5)

    def test_explicit_text_units_are_honoured(self):
        service = self._service()
        # Legacy / hand-entered values that name their unit are not treated as seconds.
        self.assertAlmostEqual(service._parse_downtime_minutes("3.5 hours"), 210.0)
        self.assertAlmostEqual(service._parse_downtime_minutes("2 hr"), 120.0)
        # Plural abbreviation must not fall through to the seconds branch.
        self.assertAlmostEqual(service._parse_downtime_minutes("2 hrs"), 120.0)
        self.assertAlmostEqual(service._parse_downtime_minutes("45 min"), 45.0)
        # An explicit seconds label matches the default bare-number behaviour.
        self.assertAlmostEqual(service._parse_downtime_minutes("7200 seconds"), 120.0)


if __name__ == "__main__":
    unittest.main()
