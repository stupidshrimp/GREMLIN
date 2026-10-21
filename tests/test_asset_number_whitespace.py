"""An asset number imported with whitespace around it.

The asset list shows every number trimmed, and each page then asks for exactly
the number that was picked from it -- the summary, the charts, the disposition
table, the Weibull populations. So a record stored as " C-3 " was listed as C-3
and then found by none of them. The mapper stores the number stripped instead,
and a mapper version bump re-derives rows that were mapped before it did.
"""

import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from services.life_data_service import _MAPPING_VERSION, LifeDataService
from services.population_analysis_service import PopulationAnalysisService
from services.standard_disposition_service import StandardDispositionService


def _wo(task_id, asset_number, asset_name="Conveyor 3"):
    """A corrective work order as the CMMS hands it over."""

    return {
        "taskID": task_id,
        "assetID": 3,
        "Asset Number": asset_number,
        "Asset Name": asset_name,
        "type": "6",
        "name": f"Belt jammed {task_id}",
        "completedDate_Final": f"2024-0{task_id}-15",
    }


class PaddedAssetNumberTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db_path = Path(tmp.name) / "gremlin.db"
        self.service = LifeDataService(self.db_path, refresh_on_startup=False)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS import_batch (import_batch_id INTEGER PRIMARY KEY, status TEXT)")
            conn.execute("INSERT INTO import_batch (import_batch_id, status) VALUES (0, 'COMPLETED')")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS raw_cmms_record ("
                "raw_record_id INTEGER PRIMARY KEY, import_batch_id INTEGER NOT NULL DEFAULT 0, "
                "source_record_id TEXT, raw_json TEXT NOT NULL, raw_content_hash TEXT)"
            )
            conn.commit()

    def _import(self, *tasks) -> None:
        with sqlite3.connect(self.db_path) as conn:
            for task in tasks:
                conn.execute("INSERT INTO raw_cmms_record (import_batch_id, raw_json) VALUES (0, ?)", (json.dumps(task),))
            conn.commit()
        self.service.refresh_mapped_cmms_records()

    def test_the_mapper_strips_the_asset_number_and_name(self):
        mapped = self.service._map_raw_record({"Asset Number": " C-3 ", "Asset Name": "\tConveyor 3  "})
        self.assertEqual((mapped["asset_number"], mapped["asset_name"]), ("C-3", "Conveyor 3"))

        # Nothing but whitespace is no asset number, as an empty one already was.
        blank = self.service._map_raw_record({"Asset Number": "   ", "Asset Name": " "})
        self.assertEqual((blank["asset_number"], blank["asset_name"]), (None, None))

        # A number the CMMS sent as a number is left as it came.
        self.assertEqual(self.service._map_raw_record({"Asset Number": 7})["asset_number"], 7)

    def test_a_padded_asset_is_found_once_picked_from_the_list(self):
        self._import(_wo(1, " C-3 ", " Conveyor 3 "), _wo(2, "C-3"))

        self.assertEqual(self.service.asset_number_options(), [{"asset_number": "C-3", "asset_name": "Conveyor 3"}])
        [picked] = self.service.asset_numbers()

        page = StandardDispositionService(self.service).disposition_page(picked)
        self.assertEqual(page["displayed_count"], 2)
        self.assertEqual(sorted(row["task_id"] for row in page["rows"]), ["1", "2"])
        analysis = PopulationAnalysisService(self.service)
        downtime = analysis.downtime({"assets": [picked], "categories": ["NOT_REVIEWED"]})
        self.assertEqual(downtime["summary"]["work_order_count"], 2)

    def test_the_tour_can_pick_a_padded_asset(self):
        self._import(_wo(1, " C-3 "))
        records = StandardDispositionService(self.service)
        [row] = records.disposition_page("C-3")["rows"]
        records.save_dispositions([{
            "mapped_record_id": row["mapped_record_id"],
            "disposition_category": "FAILURE",
            "component_code": "Belt",
            "failure_mechanism_code": "LOOS",
            "failure_mode_code": "UST",
            "action_code": "ADJUST",
            "evidence": "EXPLICIT",
        }])

        analysis = PopulationAnalysisService(self.service, records)
        example = analysis.tour_example()["asset"]

        self.assertEqual(example, "C-3")
        self.assertIn(example, self.service.asset_numbers())
        pareto = analysis.summary({"assets": [example]}, group_by="component")["pareto"]
        self.assertEqual([bar["count"] for bar in pareto["rows"]], [1])

    def test_a_row_mapped_padded_before_the_fix_is_remapped_on_startup(self):
        raw_json = json.dumps(_wo(1, " C-3 "))
        with sqlite3.connect(self.db_path) as conn:
            raw_id = conn.execute(
                "INSERT INTO raw_cmms_record (import_batch_id, raw_json) VALUES (0, ?)", (raw_json,)
            ).lastrowid
            # Mapped by the previous mapper: padded, and with an unchanged raw
            # hash, so only the version stamp says it is out of date.
            mapped_id = conn.execute(
                """
                INSERT INTO mapped_cmms_record (
                    raw_record_id, import_batch_id, raw_content_hash, asset_number, asset_name, task_id,
                    record_class_auto, record_class_final, is_corrective_wo_candidate, mapping_version
                ) VALUES (?, 0, ?, ' C-3 ', 'Conveyor 3', 1, 'CORRECTIVE_WO', 'CORRECTIVE_WO', 1, 'v3')
                """,
                (raw_id, hashlib.sha256(raw_json.encode("utf-8")).hexdigest()),
            ).lastrowid
            conn.commit()
        with self.service.connect() as conn:
            stale = conn.execute("SELECT asset_number FROM mapped_cmms_record WHERE mapped_record_id = ?", (mapped_id,)).fetchone()
        self.assertEqual(stale[0], " C-3 ")

        service = LifeDataService(self.db_path, refresh_on_startup=False)

        with service.connect() as conn:
            row = conn.execute(
                "SELECT mapped_record_id, asset_number, record_class_final, mapping_version "
                "FROM mapped_cmms_record WHERE raw_record_id = ?",
                (raw_id,),
            ).fetchone()
        # Re-derived in place: the same row, so anything keyed on its id stays put.
        self.assertEqual(
            tuple(row),
            (mapped_id, "C-3", "CORRECTIVE_WO", _MAPPING_VERSION),
        )
        rows = StandardDispositionService(service).disposition_page("C-3")["rows"]
        self.assertEqual([r["mapped_record_id"] for r in rows], [mapped_id])


if __name__ == "__main__":
    unittest.main()
