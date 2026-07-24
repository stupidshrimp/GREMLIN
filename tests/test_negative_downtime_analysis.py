import tempfile
import unittest
from pathlib import Path

from services.life_data_service import LifeDataService


class NegativeDowntimeAnalysisTests(unittest.TestCase):
    def _service(self) -> LifeDataService:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        return LifeDataService(Path(self._tmp.name) / "gremlin.db", refresh_on_startup=False)

    def _seed_failure(self, service: LifeDataService, downtime_hours: float) -> None:
        with service.write_connection() as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS import_batch (import_batch_id INTEGER PRIMARY KEY, status TEXT)")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS raw_cmms_record ("
                "raw_record_id INTEGER PRIMARY KEY, import_batch_id INTEGER NOT NULL DEFAULT 1, raw_json TEXT NOT NULL)"
            )
            conn.execute("INSERT INTO import_batch (import_batch_id, status) VALUES (1, 'COMPLETED')")
            conn.execute("INSERT INTO raw_cmms_record (raw_record_id, import_batch_id, raw_json) VALUES (1, 1, '{}')")
            mode_id = conn.execute("INSERT INTO failure_mode (failure_mode_name) VALUES ('Mode')").lastrowid
            mechanism_id = conn.execute(
                "INSERT INTO failure_mechanism (failure_mechanism_name, failure_mode_id) VALUES ('Mechanism', ?)",
                (mode_id,),
            ).lastrowid
            mapped_id = conn.execute(
                """
                INSERT INTO mapped_cmms_record (
                    raw_record_id, import_batch_id, asset_number, task_id, task_name,
                    completed_date_final, downtime_hours, record_class_auto, is_corrective_wo_candidate
                ) VALUES (1, 1, 'A-1', 'WO-1', 'Negative downtime WO', '2024-01-15', ?, 'CORRECTIVE_WO', 1)
                """,
                (downtime_hours,),
            ).lastrowid
            conn.execute(
                """
                INSERT INTO event_disposition (
                    mapped_record_id, record_class_final, disposition_category, include_in_event_processing,
                    include_in_weibull_candidate, failure_mode_id, failure_mechanism_id
                ) VALUES (?, 'CORRECTIVE_WO', 'INCLUDED_FAILURE', 1, 1, ?, ?)
                """,
                (mapped_id, mode_id, mechanism_id),
            )

    def test_pareto_clamps_negative_downtime_to_zero(self):
        service = self._service()
        self._seed_failure(service, -4.0)

        rows = service.failure_mechanism_pareto("A-1")

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["failure_count"], 1)
        self.assertEqual(rows[0]["downtime_hours"], 0.0)
        self.assertEqual(rows[0]["cumulative_percent"], 0.0)

    def test_failure_mode_trend_clamps_negative_downtime_to_zero(self):
        service = self._service()
        self._seed_failure(service, -4.0)

        trend = service.failure_mode_trend("A-1")
        mechanism = trend["mechanisms"][0]

        self.assertEqual(mechanism["total_count"], 1)
        self.assertEqual(mechanism["total_downtime_hours"], 0.0)
        self.assertEqual(mechanism["records"][0]["downtime_hours"], 0.0)


if __name__ == "__main__":
    unittest.main()
