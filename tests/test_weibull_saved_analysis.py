"""Weibull observation downtime and the read-only saved-analysis path.

Covers the two service-level behaviors the Perform an Analysis page depends on:
the source work order's downtime travelling with each Weibull observation, and
``load_saved_weibull_analysis`` reading a stored fit back without writing, which is
what lets a viewer or a signed-out visitor open the Weibull view.
"""

import tempfile
import unittest
from pathlib import Path

from services.life_data_service import LifeDataService


# (task id, completed date, recorded downtime hours) for the corrective work orders
# every test seeds. Two dated failures are the minimum that produces a positive life
# interval, and the negative value covers the clamp.
SEEDED_WORK_ORDERS = [
    ("WO-1", "2024-01-15", 2.5),
    ("WO-2", "2024-02-15", -3.0),
    ("WO-3", "2024-03-15", 7.25),
]


class WeibullSavedAnalysisTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.service = LifeDataService(Path(tmp.name) / "gremlin.db", refresh_on_startup=False)
        self.mode_id, self.mechanism_id = self._seed_failures(self.service)

    def _seed_failures(self, service: LifeDataService) -> tuple[int, int]:
        """Insert one failure mode/mechanism and the corrective WOs dispositioned onto it."""

        with service.write_connection() as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS import_batch (import_batch_id INTEGER PRIMARY KEY, status TEXT)")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS raw_cmms_record ("
                "raw_record_id INTEGER PRIMARY KEY, import_batch_id INTEGER NOT NULL DEFAULT 1, raw_json TEXT NOT NULL)"
            )
            conn.execute("INSERT INTO import_batch (import_batch_id, status) VALUES (1, 'COMPLETED')")
            mode_id = conn.execute("INSERT INTO failure_mode (failure_mode_name) VALUES ('Mode')").lastrowid
            mechanism_id = conn.execute(
                "INSERT INTO failure_mechanism (failure_mechanism_name, failure_mode_id) VALUES ('Mechanism', ?)",
                (mode_id,),
            ).lastrowid
            for index, (task_id, completed_date, downtime_hours) in enumerate(SEEDED_WORK_ORDERS, start=1):
                conn.execute(
                    "INSERT INTO raw_cmms_record (raw_record_id, import_batch_id, raw_json) VALUES (?, 1, '{}')",
                    (index,),
                )
                mapped_id = conn.execute(
                    """
                    INSERT INTO mapped_cmms_record (
                        raw_record_id, import_batch_id, asset_number, task_id, task_name,
                        completed_date_final, downtime_hours, record_class_auto, is_corrective_wo_candidate
                    ) VALUES (?, 1, 'A-1', ?, ?, ?, ?, 'CORRECTIVE_WO', 1)
                    """,
                    (index, task_id, f"Failure {task_id}", completed_date, downtime_hours),
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
        return int(mode_id), int(mechanism_id)

    def _perform(self):
        return self.service.perform_weibull_analysis(
            "A-1",
            grouping_level="FAILURE_MECHANISM",
            failure_mode_id=self.mode_id,
            failure_mechanism_id=self.mechanism_id,
        )

    def _load_saved(self):
        return self.service.load_saved_weibull_analysis(
            "A-1",
            grouping_level="FAILURE_MECHANISM",
            failure_mode_id=self.mode_id,
            failure_mechanism_id=self.mechanism_id,
        )

    def test_observations_carry_the_closing_work_orders_downtime(self):
        result = self._perform()

        by_task = {obs["source_task_id"]: obs for obs in result.observations}
        # Each observation's life interval is closed by a work order, so it reports that
        # work order's downtime. WO-1 opens the first interval without closing one.
        self.assertEqual(by_task["WO-3"]["source_downtime_hours"], 7.25)
        # A negative recorded downtime is a data-entry artifact, reported as zero.
        self.assertEqual(by_task["WO-2"]["source_downtime_hours"], 0)
        # The trailing current-life row has no closing work order at all, so it has no
        # downtime to report rather than a misleading zero.
        current_life = [obs for obs in result.observations if obs["source_task_id"] is None]
        self.assertTrue(current_life)
        for obs in current_life:
            self.assertIsNone(obs["source_downtime_hours"])

    def test_saved_analysis_reads_back_the_performed_fit(self):
        performed = self._perform()

        saved = self._load_saved()

        self.assertIsNotNone(saved)
        self.assertEqual(saved.result_id, performed.result_id)
        self.assertEqual(saved.run_id, performed.run_id)
        self.assertAlmostEqual(saved.beta_mle, performed.beta_mle)
        self.assertAlmostEqual(saved.eta_mle, performed.eta_mle)
        self.assertEqual(saved.failure_count, performed.failure_count)
        self.assertEqual(saved.censored_count, performed.censored_count)
        self.assertEqual(saved.total_observation_count, performed.total_observation_count)
        self.assertEqual(saved.analysis_label, performed.analysis_label)
        self.assertEqual(saved.grouping_level, performed.grouping_level)
        self.assertEqual(saved.interpretation_summary, performed.interpretation_summary)
        # The charts and the data table are driven by these three lists, so a read-back
        # result has to reproduce them for the read-only view to render identically.
        self.assertEqual(len(saved.km_points), len(performed.km_points))
        self.assertEqual(len(saved.curve_points), len(performed.curve_points))
        self.assertEqual(
            [obs["weibull_observation_id"] for obs in saved.observations],
            [obs["weibull_observation_id"] for obs in performed.observations],
        )
        self.assertEqual(
            [obs["source_downtime_hours"] for obs in saved.observations],
            [obs["source_downtime_hours"] for obs in performed.observations],
        )
        self.assertEqual([obs["ordered_index"] for obs in saved.observations], [1, 2, 3])

    def test_saved_analysis_is_none_before_any_analysis_runs(self):
        self.assertIsNone(self._load_saved())

    def test_saved_analysis_does_not_write(self):
        self._perform()
        before = self._load_saved()

        # Reading twice must not create a population, dataset, run, or result: this is
        # the path a viewer takes, and it has no write permission to fall back on.
        with self.service.connect() as conn:
            counts = {
                table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in ("modeled_population", "analysis_dataset", "weibull_analysis_run", "weibull_result")
            }
        self._load_saved()
        with self.service.connect() as conn:
            after_counts = {
                table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in counts
            }

        self.assertEqual(counts, after_counts)
        self.assertEqual(before.result_id, self._load_saved().result_id)

    def test_saved_analysis_rejects_a_mechanism_level_request_without_a_mechanism(self):
        with self.assertRaises(ValueError):
            self.service.load_saved_weibull_analysis(
                "A-1",
                grouping_level="FAILURE_MECHANISM",
                failure_mode_id=self.mode_id,
            )

    def test_saved_analysis_returns_the_latest_run_for_the_group(self):
        self._perform()
        rerun = self._perform()

        saved = self._load_saved()

        self.assertEqual(saved.result_id, rerun.result_id)


if __name__ == "__main__":
    unittest.main()
