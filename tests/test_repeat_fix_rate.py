"""Repeat Fix Rate: how often a mechanism's failure comes straight back after a repair.

* it reads the failures Weibull reads -- included failures with Include in Weibull
  Candidate, dated by completed date alone -- and nothing else;
* the gap between a mechanism's failures is in scheduled hours on the asset's
  Weibull schedule and the plant's clock, just as a Weibull life is;
* a failure is a repeat when that gap is at or under the window, 24 scheduled
  hours unless the page asks for another;
* the rate is repeats over intervals, and a mechanism is only ranked by rate once
  it has enough intervals for one quick repeat not to top the list;
* a repeat that closed within an hour of the one before is flagged for the
  REL-WBL-DAT-004 §12 duplicate check;
* it is report-only: the Weibull fit's lives do not change.
"""

import importlib
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from services.life_data_service import REPEAT_FIX_MIN_INTERVALS, LifeDataService

PLANT = ZoneInfo("America/Chicago")


def _utc(year, month, day, hour=0, minute=0) -> str:
    return datetime(year, month, day, hour, minute, tzinfo=PLANT).astimezone(timezone.utc).isoformat()


def _seed_schema(service: LifeDataService) -> None:
    with service.write_connection() as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS import_batch (import_batch_id INTEGER PRIMARY KEY, status TEXT)")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS raw_cmms_record ("
            "raw_record_id INTEGER PRIMARY KEY, import_batch_id INTEGER NOT NULL DEFAULT 1, raw_json TEXT NOT NULL)"
        )
        conn.execute("INSERT OR IGNORE INTO import_batch (import_batch_id, status) VALUES (1, 'COMPLETED')")


class _Seeded:
    """Failures on asset A-1, which is on the plant default schedule (20 h, Monday to Friday)."""

    def __init__(self, service: LifeDataService) -> None:
        self.service = service
        self._raw_id = 0
        _seed_schema(service)
        with service.write_connection() as conn:
            self.mode_id = int(conn.execute("INSERT INTO failure_mode (failure_mode_name) VALUES ('Clamp')").lastrowid)
            self.seal = self._mechanism(conn, "Seal wear")
            self.switch = self._mechanism(conn, "Switch out of adjustment")

    def _mechanism(self, conn, name: str) -> int:
        return int(
            conn.execute(
                "INSERT INTO failure_mechanism (failure_mechanism_name, failure_mode_id) VALUES (?, ?)", (name, self.mode_id)
            ).lastrowid
        )

    def add(self, task_id, completed, mechanism_id, *, category="INCLUDED_FAILURE", weibull=True, pm=False) -> int:
        self._raw_id += 1
        with self.service.write_connection() as conn:
            conn.execute(
                "INSERT INTO raw_cmms_record (raw_record_id, import_batch_id, raw_json) VALUES (?, 1, '{}')", (self._raw_id,)
            )
            mapped_id = int(
                conn.execute(
                    """
                    INSERT INTO mapped_cmms_record (
                        raw_record_id, import_batch_id, asset_number, task_id, task_name, completed_date_final,
                        downtime_hours, record_class_auto, is_corrective_wo_candidate, is_pm_candidate
                    ) VALUES (?, 1, 'A-1', ?, ?, ?, 1.0, ?, ?, ?)
                    """,
                    (self._raw_id, task_id, f"Task {task_id}", completed, "PM" if pm else "CORRECTIVE_WO", int(not pm), int(pm)),
                ).lastrowid
            )
            if pm:
                conn.execute(
                    """
                    INSERT INTO event_disposition (
                        mapped_record_id, record_class_final, disposition_category, include_in_event_processing,
                        include_in_weibull_candidate, reset_target_failure_mode_id, reset_target_failure_mechanism_id,
                        pm_reset_inclusion_decision, pm_reset_renewal_rationale
                    ) VALUES (?, 'PM', 'INCLUDED_PM_RESET_EVENT', 1, 1, ?, ?, 'APPROVED_RESET', 'Reset to spec.')
                    """,
                    (mapped_id, self.mode_id, mechanism_id),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO event_disposition (
                        mapped_record_id, record_class_final, disposition_category, include_in_event_processing,
                        include_in_weibull_candidate, failure_mode_id, failure_mechanism_id
                    ) VALUES (?, 'CORRECTIVE_WO', ?, 1, ?, ?, ?)
                    """,
                    (mapped_id, category, int(weibull), self.mode_id, mechanism_id),
                )
        return mapped_id


class RepeatFixRateTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.service = LifeDataService(Path(tmp.name) / "gremlin.db", refresh_on_startup=False)
        self.seed = _Seeded(self.service)

    def _mechanism(self, result, mechanism_id):
        return next(m for m in result["mechanisms"] if m["failure_mechanism_id"] == mechanism_id)

    def test_gaps_are_scheduled_hours_so_a_weekend_does_not_hide_a_repeat(self):
        seal = self.seed.seal
        # Monday 09:00 to Tuesday 08:00 is 23 calendar hours, 19.2 scheduled: a repeat.
        self.seed.add("1", _utc(2025, 3, 3, 9), seal)
        self.seed.add("2", _utc(2025, 3, 4, 8), seal)
        # Friday 16:00 to Monday 10:00 is 66 calendar hours but only 8 + 10 weekday
        # hours, 15 scheduled at 20/24: still a repeat.
        self.seed.add("3", _utc(2025, 3, 14, 16), seal)
        self.seed.add("4", _utc(2025, 3, 17, 10), seal)
        # Two weeks on: the fix held.
        self.seed.add("5", _utc(2025, 3, 31, 10), seal)

        result = self.service.repeat_fix_rate("A-1")
        self.assertEqual(result["window_hours"], 24.0)
        self.assertEqual(result["time_zone"], "America/Chicago")
        mechanism = self._mechanism(result, seal)
        self.assertEqual((mechanism["failures"], mechanism["intervals"], mechanism["repeats"]), (5, 4, 2))
        self.assertAlmostEqual(mechanism["repeat_rate"], 0.5)
        self.assertEqual([(p["prior_task_id"], p["repeat_task_id"]) for p in result["pairs"]], [("1", "2"), ("3", "4")])
        self.assertAlmostEqual(result["pairs"][0]["scheduled_hours"], 23 * 20 / 24, places=2)
        self.assertAlmostEqual(result["pairs"][0]["raw_hours"], 23.0, places=2)
        self.assertAlmostEqual(result["pairs"][1]["scheduled_hours"], 15.0, places=2)
        self.assertAlmostEqual(result["pairs"][1]["raw_hours"], 66.0, places=2)
        self.assertEqual((result["intervals"], result["repeats"]), (4, 2))

    def test_the_window_is_inclusive_and_can_be_changed(self):
        seal = self.seed.seal
        # Monday 08:00 to Tuesday 08:00 at 20/24 is exactly 20 scheduled hours.
        self.seed.add("1", _utc(2025, 3, 3, 8), seal)
        self.seed.add("2", _utc(2025, 3, 4, 8), seal)
        self.assertEqual(self.service.repeat_fix_rate("A-1", window_hours=20)["repeats"], 1)
        self.assertEqual(self.service.repeat_fix_rate("A-1", window_hours=19.9)["repeats"], 0)

    def test_mechanisms_are_counted_apart_and_only_weibull_failures_are_read(self):
        seal, switch = self.seed.seal, self.seed.switch
        self.seed.add("1", _utc(2025, 3, 3, 9), seal)
        # Another mechanism's failure in between neither starts nor ends a seal gap.
        self.seed.add("2", _utc(2025, 3, 3, 12), switch)
        # Nor does a failure left out of Weibull, an excluded record, or an undated one.
        self.seed.add("3", _utc(2025, 3, 3, 13), seal, weibull=False)
        self.seed.add("4", _utc(2025, 3, 3, 14), seal, category="EXCLUDED_NON_FAILURE")
        self.seed.add("5", None, seal)
        # A PM reset does not break the chain: the question is whether the repair held.
        self.seed.add("PM", _utc(2025, 3, 3, 15), seal, pm=True)
        self.seed.add("6", _utc(2025, 3, 3, 17), seal)

        result = self.service.repeat_fix_rate("A-1")
        self.assertEqual(result["undated_failures"], 1)
        self.assertEqual(result["failures"], 3)
        self.assertEqual([(p["prior_task_id"], p["repeat_task_id"]) for p in result["pairs"]], [("1", "6")])
        switch_row = self._mechanism(result, switch)
        self.assertEqual((switch_row["failures"], switch_row["intervals"], switch_row["repeat_rate"]), (1, 0, None))

    def test_a_repeat_within_an_hour_is_flagged_for_the_duplicate_check(self):
        seal = self.seed.seal
        self.seed.add("1", _utc(2025, 3, 3, 9), seal)
        self.seed.add("2", _utc(2025, 3, 3, 9, 4), seal)
        self.seed.add("3", _utc(2025, 3, 3, 15), seal)
        result = self.service.repeat_fix_rate("A-1")
        flags = [(p["repeat_task_id"], bool(p["duplicate_check"])) for p in result["pairs"]]
        self.assertEqual(flags, [("2", True), ("3", False)])
        self.assertEqual(result["possible_duplicates"], 1)

    def test_highest_rate_needs_enough_intervals_and_most_repeats_needs_one(self):
        seal, switch = self.seed.seal, self.seed.switch
        # Switch: two failures a day apart, one repeat out of one interval (100%).
        self.seed.add("S1", _utc(2025, 3, 3, 9), switch)
        self.seed.add("S2", _utc(2025, 3, 4, 9), switch)
        # Seal: five intervals, the first two of them repeats (40%).
        self.assertEqual(REPEAT_FIX_MIN_INTERVALS, 5)
        for i, day in enumerate([3, 4, 5, 12, 19, 26]):
            self.seed.add(f"F{i}", _utc(2025, 3, day, 9), seal)
        result = self.service.repeat_fix_rate("A-1")
        seal_row = self._mechanism(result, seal)
        self.assertEqual((seal_row["intervals"], seal_row["repeats"]), (REPEAT_FIX_MIN_INTERVALS, 2))
        self.assertEqual(result["highest_rate"]["failure_mechanism_id"], seal)
        self.assertEqual(result["most_repeats"]["failure_mechanism_id"], seal)
        self.assertEqual(result["mechanisms"][0]["failure_mechanism_id"], seal)

    def test_a_date_with_no_time_is_that_day_on_the_plant_calendar(self):
        seal = self.seed.seal
        # Friday 20:00 on the plant's clock, then a bare Monday date: the start of
        # Monday, 4 scheduled hours of Friday later at 20/24. Read as midnight UTC it
        # would be Sunday evening, and would count none of Friday's hours either way.
        self.seed.add("1", _utc(2025, 3, 14, 20), seal)
        self.seed.add("2", "2025-03-17", seal)
        pair = self.service.repeat_fix_rate("A-1")["pairs"][0]
        self.assertEqual(pair["repeat_completed"], datetime(2025, 3, 17, tzinfo=PLANT).astimezone(timezone.utc).isoformat())
        # The table shows the date as stored, not the plant midnight it was read as.
        self.assertEqual((pair["prior_completed_raw"], pair["repeat_completed_raw"]), (_utc(2025, 3, 14, 20), "2025-03-17"))
        self.assertAlmostEqual(pair["scheduled_hours"], 4 * 20 / 24, places=2)
        self.assertAlmostEqual(pair["raw_hours"], 52.0, places=2)

    def test_nothing_to_report(self):
        result = self.service.repeat_fix_rate("A-1")
        self.assertEqual((result["failures"], result["repeats"], result["repeat_rate"]), (0, 0, None))
        self.assertIsNone(result["highest_rate"])
        self.assertIsNone(result["most_repeats"])

    def test_it_does_not_change_the_weibull_lives(self):
        seal = self.seed.seal
        for task, when in [
            ("1", _utc(2025, 1, 6, 9)), ("2", _utc(2025, 1, 7, 8)), ("3", _utc(2025, 2, 4, 14)),
            ("4", _utc(2025, 3, 3, 9)), ("5", _utc(2025, 3, 26, 11)), ("6", _utc(2025, 5, 1, 16)),
            ("7", _utc(2025, 5, 22, 8)), ("8", _utc(2025, 6, 23, 10)),
        ]:
            self.seed.add(task, when, seal)
        kwargs = dict(grouping_level="FAILURE_MECHANISM", failure_mode_id=self.seed.mode_id, failure_mechanism_id=seal)
        before = self.service.perform_weibull_analysis("A-1", **kwargs)
        self.assertEqual(self.service.repeat_fix_rate("A-1")["repeats"], 1)
        after = self.service.perform_weibull_analysis("A-1", **kwargs)
        self.assertEqual(before.beta_mle, after.beta_mle)
        self.assertEqual(before.eta_mle, after.eta_mle)


# ---- the endpoint --------------------------------------------------------


def _client(monkeypatch, tmp_path):
    monkeypatch.setenv("GREMLIN_DB_PATH", str(tmp_path / "gremlin.db"))
    monkeypatch.setenv("GREMLIN_ACCESS_DB_PATH", str(tmp_path / "accesscontrol.db"))
    import app

    module = importlib.reload(app)
    return module, module.app.test_client()


def test_the_endpoint_returns_the_analysis_with_the_window_asked_for(monkeypatch, tmp_path):
    module, client = _client(monkeypatch, tmp_path)
    seed = _Seeded(module.get_life_data_service())
    seed.add("1", _utc(2025, 3, 3, 8), seed.seal)
    seed.add("2", _utc(2025, 3, 4, 8), seed.seal)

    payload = client.get("/life-data-analysis/api/repeat-fixes?asset=A-1").get_json()["repeat_fixes"]
    assert (payload["window_hours"], payload["repeats"]) == (24.0, 1)
    payload = client.get("/life-data-analysis/api/repeat-fixes?asset=A-1&window_hours=8").get_json()["repeat_fixes"]
    assert (payload["window_hours"], payload["repeats"]) == (8.0, 0)


@pytest.mark.parametrize("window", ["abc", "0", "-4", "721", "nan", "inf"])
def test_the_endpoint_refuses_a_window_it_cannot_use(monkeypatch, tmp_path, window):
    _, client = _client(monkeypatch, tmp_path)
    response = client.get(f"/life-data-analysis/api/repeat-fixes?asset=A-1&window_hours={window}")
    assert response.status_code == 400
    assert "repeat window" in response.get_json()["error"]


def test_the_standards_example_is_what_gremlin_counts(tmp_path):
    """The Standards page's worked example, run through the service on the 20-hour default."""

    service = LifeDataService(tmp_path / "gremlin.db", refresh_on_startup=False)
    seed = _Seeded(service)
    for task, when in [
        ("1", _utc(2025, 3, 3, 9)), ("2", _utc(2025, 3, 4, 8)), ("3", _utc(2025, 3, 14, 16)),
        ("4", _utc(2025, 3, 17, 10)), ("5", _utc(2025, 3, 31, 10)),
    ]:
        seed.add(task, when, seed.seal)
    gaps = []
    with service.connect() as conn:
        zone = service._plant_time_zone(conn)[0]
    dates = [datetime.fromisoformat(_utc(*d)) for d in [(2025, 3, 3, 9), (2025, 3, 4, 8), (2025, 3, 14, 16), (2025, 3, 17, 10), (2025, 3, 31, 10)]]
    for start, end in zip(dates, dates[1:]):
        gaps.append(service._scheduled_life_hours(start, end, 20, tz=zone)[0])
    result = service.repeat_fix_rate("A-1")
    page = (Path(__file__).resolve().parent.parent / "templates" / "standards_analysis.html").read_text(encoding="utf-8")
    example = page.split('id="repeat-cards"', 1)[1].split("</section>", 1)[0]
    assert f"= {gaps[0]:.1f} scheduled hours: a repeat" in example
    assert f"= {gaps[1]:.1f} scheduled hours: the fix held" in example
    assert f"{gaps[2]:g} scheduled hours: a repeat" in example
    assert f"{gaps[3]:.0f} scheduled hours: the fix held" in example
    assert f'= <span class="std-result">{result["repeat_rate"]:.0%}</span>' in example
    assert [p["scheduled_hours"] for p in result["pairs"]] == [round(gaps[0], 2), round(gaps[2], 2)]
