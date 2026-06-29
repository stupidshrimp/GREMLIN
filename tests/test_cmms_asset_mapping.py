"""Regression tests for CMMS asset-number mapping from API task payloads.

Limble's task API identifies an asset only by the numeric ``assetID``; the
legacy Excel export used a human-readable "Asset Number" column. These tests
pin two things:

* API-sourced raw records (``assetID`` only) still populate an asset number, so
  they appear in the asset dropdown instead of collapsing to a single NULL
  asset. This is the bug behind "200k records mapped, only 10 asset numbers".
* A change to the mapping logic re-maps already-imported rows via the
  ``mapping_version`` bump, even though their raw JSON is unchanged.
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from services.life_data_service import LifeDataService


class AssetNumberMappingTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "GREMLIN.db"
        # refresh_on_startup=False keeps each test in control of when mapping runs.
        self.service = LifeDataService(self.db_path, refresh_on_startup=False)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("INSERT INTO import_batch (source_system) VALUES ('Limble')")
            conn.commit()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _seed_raw(self, records: list[dict]) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("PRAGMA foreign_keys = ON")
            for raw in records:
                conn.execute(
                    "INSERT INTO raw_cmms_record (import_batch_id, source_record_id, raw_json) "
                    "VALUES (1, ?, ?)",
                    (str(raw.get("taskID") or ""), json.dumps(raw)),
                )
            conn.commit()

    def test_api_records_populate_asset_number_from_asset_id(self) -> None:
        # API task payloads carry assetID, not a human-readable "Asset Number".
        self._seed_raw(
            [
                {"taskID": 1, "name": "WO 1", "assetID": 4757, "type": "6"},
                {"taskID": 2, "name": "WO 2", "assetID": 3102, "type": "6"},
                {"taskID": 3, "name": "WO 3", "assetID": 4757, "type": "1"},
            ]
        )
        mapped = self.service.refresh_mapped_cmms_records()
        self.assertEqual(mapped, 3)
        numbers = [opt["asset_number"] for opt in self.service.asset_number_options()]
        # Two distinct assets, naturally sorted; the duplicate assetID collapses.
        self.assertEqual(numbers, ["3102", "4757"])

    def test_legacy_asset_number_takes_precedence_over_asset_id(self) -> None:
        # A row that still carries the legacy Excel columns keeps the old behavior.
        self._seed_raw(
            [
                {
                    "taskID": 1,
                    "name": "WO 1",
                    "assetID": 4757,
                    "Asset Number": "PUMP-1",
                    "Asset Name": "Main Pump",
                }
            ]
        )
        self.service.refresh_mapped_cmms_records()
        self.assertEqual(
            self.service.asset_number_options(),
            [{"asset_number": "PUMP-1", "asset_name": "Main Pump"}],
        )

    def test_records_with_no_asset_are_excluded(self) -> None:
        self._seed_raw(
            [
                {"taskID": 1, "name": "General task", "type": "1"},  # no assetID at all
                {"taskID": 2, "name": "WO", "assetID": 500},
            ]
        )
        self.service.refresh_mapped_cmms_records()
        numbers = [opt["asset_number"] for opt in self.service.asset_number_options()]
        self.assertEqual(numbers, ["500"])

    def test_version_change_forces_full_remap(self) -> None:
        # The fix only reaches already-imported rows because the mapping_version
        # bump defeats the unchanged-raw-JSON skip. Prove that mechanism here.
        self._seed_raw([{"taskID": 1, "name": "WO", "assetID": 100}])
        self.assertEqual(self.service.refresh_mapped_cmms_records(), 1)  # initial map
        self.assertEqual(self.service.refresh_mapped_cmms_records(), 0)  # unchanged -> skipped
        self.service._MAPPING_VERSION = "v-next"
        self.assertEqual(self.service.refresh_mapped_cmms_records(), 1)  # version bump -> re-mapped


if __name__ == "__main__":
    unittest.main()
