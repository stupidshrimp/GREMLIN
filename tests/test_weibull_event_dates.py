"""The date a Weibull event takes, and the order its events are numbered in.

The fit dates each event the way the trend, downtime driver and PM effectiveness
analyses date a record: completed, else start, else created, with a blank date
(empty or spaces-only) passed over for the next one. A spaces-only completed
date used to be picked as it stood, fail to parse, and take the event out of the
fit even when its start date was usable.
"""

import tempfile
import unittest
from pathlib import Path

from services.life_data_service import LifeDataService


class WeibullEventDateTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.service = LifeDataService(Path(tmp.name) / "gremlin.db", refresh_on_startup=False)

    def _seed(self, work_orders: list[tuple[str, str | None, str | None, str | None]]) -> None:
        """Disposition each (task id, completed, start, created) onto one mechanism as an included failure."""

        with self.service.write_connection() as conn:
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
            for index, (task_id, completed, start, created) in enumerate(work_orders, start=1):
                conn.execute(
                    "INSERT INTO raw_cmms_record (raw_record_id, import_batch_id, raw_json) VALUES (?, 1, '{}')",
                    (index,),
                )
                mapped_id = conn.execute(
                    """
                    INSERT INTO mapped_cmms_record (
                        raw_record_id, import_batch_id, asset_number, task_id, task_name,
                        completed_date_final, start_date_final, created_date_final,
                        record_class_auto, is_corrective_wo_candidate
                    ) VALUES (?, 1, 'A-1', ?, ?, ?, ?, ?, 'CORRECTIVE_WO', 1)
                    """,
                    (index, task_id, f"Failure {task_id}", completed, start, created),
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
        self.mode_id, self.mechanism_id = int(mode_id), int(mechanism_id)

    def _perform(self):
        return self.service.perform_weibull_analysis(
            "A-1",
            grouping_level="FAILURE_MECHANISM",
            failure_mode_id=self.mode_id,
            failure_mechanism_id=self.mechanism_id,
        )

    def _numbered_events(self) -> list[tuple[str, str | None]]:
        """Each event's task id in sequence-number order, with the task id of the event before it."""

        with self.service.connect() as conn:
            rows = conn.execute(
                """
                SELECT m.task_id, previous_m.task_id AS previous_task_id
                FROM event_processing_record ep
                JOIN mapped_cmms_record m ON m.mapped_record_id = ep.mapped_record_id
                LEFT JOIN event_processing_record previous_ep
                    ON previous_ep.event_processing_id = ep.previous_same_population_event_id
                LEFT JOIN mapped_cmms_record previous_m ON previous_m.mapped_record_id = previous_ep.mapped_record_id
                WHERE ep.asset_number = 'A-1'
                ORDER BY ep.weibull_sequence_number
                """
            ).fetchall()
        return [(row["task_id"], row["previous_task_id"]) for row in rows]

    def test_a_spaces_only_completed_date_falls_back_to_the_start_date(self):
        self._seed([
            ("WO-1", "2024-01-15", None, None),
            ("WO-2", "   ", "2024-02-15", "2024-02-14"),
            ("WO-3", "2024-03-15", None, None),
        ])

        result = self._perform()

        # WO-2 stays in the fit, so it closes the life from WO-1 and opens the one
        # WO-3 closes: two failure lives, where dropping it would leave one.
        self.assertEqual(result.failure_count, 2)
        by_closing_task = {obs["source_task_id"]: obs for obs in result.observations}
        self.assertEqual(by_closing_task["WO-2"]["end_datetime"][:10], "2024-02-15")
        self.assertEqual(by_closing_task["WO-3"]["start_datetime"][:10], "2024-02-15")
        # The same date the trend puts the record in its month by.
        trend_dates = {
            record["task_id"]: record["wo_date"]
            for mechanism in self.service.failure_mode_trend("A-1")["mechanisms"]
            for record in mechanism["records"]
        }
        self.assertEqual(trend_dates["WO-2"], "2024-02-15")

    def test_events_are_numbered_in_calendar_order(self):
        # Compared as text, "   " sorts ahead of every date and "1/20/2024 08:00"
        # (the shape an older import wrote) ahead of "2024-01-10".
        self._seed([
            ("WO-A", "1/20/2024 08:00", None, None),
            ("WO-B", "2024-01-10", None, None),
            ("WO-C", "   ", "2024-02-15", None),
            ("WO-D", "2024-03-15", None, None),
        ])

        self._perform()

        self.assertEqual(
            self._numbered_events(),
            [("WO-B", None), ("WO-A", "WO-B"), ("WO-C", "WO-A"), ("WO-D", "WO-C")],
        )


if __name__ == "__main__":
    unittest.main()
