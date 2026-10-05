"""The date a Weibull event takes, and the order its events are numbered in.

The fit dates each event by its completed date alone (REL-WBL-DAT-001 makes it the
Weibull chronology field): a life restarts when the repair is finished, so a work
order with no completed date has not restored anything yet. Unlike the trend,
downtime driver and PM effectiveness analyses there is no falling back to the start
or created date. Such an event is not dropped silently either: it gets a row in the
event processing table saying why it was left out (REL-WBL-DAT-004 §6).
"""

import tempfile
import unittest
from pathlib import Path

from services.life_data_service import LifeDataService

# Monthly failures on one mechanism: six dated failures give the five lives ending in
# a failure that a Weibull fit needs, the first only starting the clock.
DATED = [
    ("WO-1", "2024-01-15", None, None),
    ("WO-3", "2024-03-15", None, None),
    ("WO-4", "2024-04-15", None, None),
    ("WO-5", "2024-05-15", None, None),
    ("WO-6", "2024-06-17", None, None),
    ("WO-7", "2024-07-15", None, None),
]


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
        """Each numbered event's task id in sequence-number order, with the task id of the event before it."""

        with self.service.connect() as conn:
            rows = conn.execute(
                """
                SELECT m.task_id, previous_m.task_id AS previous_task_id
                FROM event_processing_record ep
                JOIN mapped_cmms_record m ON m.mapped_record_id = ep.mapped_record_id
                LEFT JOIN event_processing_record previous_ep
                    ON previous_ep.event_processing_id = ep.previous_same_population_event_id
                LEFT JOIN mapped_cmms_record previous_m ON previous_m.mapped_record_id = previous_ep.mapped_record_id
                WHERE ep.asset_number = 'A-1' AND ep.weibull_sequence_number IS NOT NULL
                ORDER BY ep.weibull_sequence_number
                """
            ).fetchall()
        return [(row["task_id"], row["previous_task_id"]) for row in rows]

    def test_a_work_order_with_no_completed_date_is_listed_and_left_out(self):
        # WO-2 has a start date but a spaces-only completed date: the trend still dates
        # it by the start date, but no life may begin or end at it.
        self._seed(DATED + [("WO-2", "   ", "2024-02-15", "2024-02-14")])

        result = self._perform()

        closing_tasks = {obs["source_task_id"] for obs in result.observations}
        self.assertNotIn("WO-2", closing_tasks)
        self.assertEqual(result.failure_count, 5)
        by_task = {event["task_id"]: event for event in result.events}
        self.assertEqual(by_task["WO-2"]["event_role"], "EXCLUDED_EVENT")
        self.assertEqual(by_task["WO-2"]["weibull_life_note"], "Excluded - missing completed date")
        self.assertIsNone(by_task["WO-2"]["weibull_sequence_number"])
        # WO-3's life runs from WO-1, straight past WO-2's start date.
        by_closing_task = {obs["source_task_id"]: obs for obs in result.observations}
        self.assertEqual(by_closing_task["WO-3"]["start_datetime"][:10], "2024-01-15")
        # The trend is not governed by the Weibull chronology rule.
        trend_dates = {
            record["task_id"]: record["wo_date"]
            for mechanism in self.service.failure_mode_trend("A-1")["mechanisms"]
            for record in mechanism["records"]
        }
        self.assertEqual(trend_dates["WO-2"], "2024-02-15")

    def test_a_completed_date_that_cannot_be_read_is_listed_and_left_out(self):
        self._seed(DATED + [("WO-X", "not a date", "2024-02-15", None)])

        result = self._perform()

        by_task = {event["task_id"]: event for event in result.events}
        self.assertEqual(by_task["WO-X"]["weibull_life_note"], "Excluded - date parse issue")
        self.assertEqual(by_task["WO-X"]["date_parse_status"], "UNPARSEABLE")
        self.assertNotIn("WO-X", {obs["source_task_id"] for obs in result.observations})
        # Listed after every dated event.
        self.assertEqual(result.events[-1]["task_id"], "WO-X")

    def test_events_are_numbered_in_calendar_order(self):
        # Compared as text, "1/20/2024 08:00" (the shape an older import wrote) sorts
        # ahead of "2024-01-10"; read as dates it comes after.
        self._seed([
            ("WO-A", "1/20/2024 08:00", None, None),
            ("WO-B", "2024-01-10", None, None),
            ("WO-C", "   ", "2024-02-15", None),
            ("WO-D", "2024-03-15", None, None),
            ("WO-E", "2024-04-15", None, None),
            ("WO-F", "2024-05-15", None, None),
            ("WO-G", "2024-06-17", None, None),
        ])

        self._perform()

        self.assertEqual(
            self._numbered_events(),
            [("WO-B", None), ("WO-A", "WO-B"), ("WO-D", "WO-A"), ("WO-E", "WO-D"), ("WO-F", "WO-E"), ("WO-G", "WO-F")],
        )

    def test_the_first_event_starts_the_clock_and_closes_no_life(self):
        self._seed(DATED)

        result = self._perform()

        first = result.events[0]
        self.assertEqual(first["task_id"], "WO-1")
        self.assertEqual(first["weibull_life_note"], "Initial occurrence - no prior comparable start point available")
        self.assertNotIn("WO-1", {obs["source_task_id"] for obs in result.observations})


if __name__ == "__main__":
    unittest.main()
