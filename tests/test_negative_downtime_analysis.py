"""A negative downtime (a CMMS clock entered backwards) counts as no downtime.

The Pareto's downtime, the downtime drivers and the record itself all read it as
0 hours rather than subtracting from the population's total.
"""

import tempfile
import unittest
from pathlib import Path

from services.life_data_service import LifeDataService
from services.population_analysis_service import PopulationAnalysisService
from services.standard_disposition_service import StandardDispositionService


class NegativeDowntimeAnalysisTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.life = LifeDataService(Path(tmp.name) / "gremlin.db", refresh_on_startup=False)
        self.records = StandardDispositionService(self.life)
        self.analysis = PopulationAnalysisService(self.life, self.records)
        with self.life.write_connection() as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS import_batch (import_batch_id INTEGER PRIMARY KEY, status TEXT)")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS raw_cmms_record ("
                "raw_record_id INTEGER PRIMARY KEY, import_batch_id INTEGER NOT NULL DEFAULT 1, raw_json TEXT NOT NULL)"
            )
            conn.execute("INSERT INTO import_batch (import_batch_id, status) VALUES (1, 'COMPLETED')")
            for record_id, downtime in ((1, -4.0), (2, 3.0)):
                conn.execute("INSERT INTO raw_cmms_record (raw_record_id, raw_json) VALUES (?, '{}')", (record_id,))
                conn.execute(
                    """
                    INSERT INTO mapped_cmms_record (
                        mapped_record_id, raw_record_id, import_batch_id, asset_number, task_id, task_name,
                        created_date_final, completed_date_final, downtime_hours, record_class_auto, mapping_version
                    ) VALUES (?, ?, 1, 'A-1', ?, 'Pump failure', '2024-01-15', '2024-01-15', ?, 'CORRECTIVE_WO', 'v4')
                    """,
                    (record_id, record_id, str(record_id), downtime),
                )
        self.records.save_dispositions([
            {
                "mapped_record_id": record_id,
                "disposition_category": "FAILURE",
                "component_code": "Pump",
                "failure_mechanism_code": "WEAR",
                "failure_mode_code": "UST",
                "action_code": "REPLACE",
                "evidence": "EXPLICIT",
            }
            for record_id in (1, 2)
        ])

    def test_the_record_reads_negative_downtime_as_zero(self):
        self.assertEqual(self.records.disposition_record(1)["downtime_hours"], 0)

    def test_the_pareto_and_the_downtime_drivers_add_it_as_zero(self):
        summary = self.analysis.summary({"assets": ["A-1"]}, group_by="component")
        bar = summary["pareto"]["rows"][0]
        self.assertEqual((bar["count"], bar["downtime_hours"]), (2, 3.0))
        downtime = self.analysis.downtime({"assets": ["A-1"]})
        self.assertEqual(downtime["summary"]["total_downtime_hours"], 3.0)
        self.assertEqual(downtime["summary"]["work_order_count"], 2)


if __name__ == "__main__":
    unittest.main()
