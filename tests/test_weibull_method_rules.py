"""The Weibull method rules GREMLIN shares with the REL-WBL document series.

Each test pins one rule to the document that sets it. These are the rules the
renewal-stream analysis on Perform an Analysis still reads from LifeDataService:

* life hours split days at midnight on the plant's clock (REL-WBL-DAT-003 §7, §7.1);
* there is no substitute for a maximum-likelihood beta (REL-WBL-MTH-001 §5.5);
* every fit reports its probability plot's R², the squared correlation of the
  Kaplan-Meier failure points in Weibull coordinates (REL-WBL-REQ-001 VV-070,
  VV-078), flagged for review below the value 90% of genuine Weibull samples of
  its size reach (VV-074);
* an interval counts as stable by its width relative to its estimate (REL-WBL-MTH-001 §8);
* life hours are counted on the asset's schedule in the Weibull schedule register,
  the plant default otherwise (REL-WBL-DAT-003 §7).
"""

import importlib
import sqlite3
import statistics
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from services.life_data_service import (
    R_SQUARED_REVIEW_THRESHOLDS,
    LifeDataService,
    WeibullFitError,
)

PLANT = ZoneInfo("America/Chicago")
def _utc(year, month, day, hour=0, minute=0, *, zone=PLANT) -> str:
    """A plant-clock time as the ISO UTC string the Limble sync stores."""

    return datetime(year, month, day, hour, minute, tzinfo=zone).astimezone(timezone.utc).isoformat()


# Seven failures some weeks apart, at varying plant-clock times: six lives ending in
# one. They vary on purpose -- identical lives have no maximum-likelihood beta.
MONTHLY = [
    ("F1", _utc(2025, 1, 6, 9)),
    ("F2", _utc(2025, 2, 4, 14)),
    ("F3", _utc(2025, 3, 3, 9)),
    ("F4", _utc(2025, 3, 26, 11)),
    ("F5", _utc(2025, 5, 1, 16)),
    ("F6", _utc(2025, 5, 22, 8)),
    ("F7", _utc(2025, 6, 23, 10)),
]


class _Seeded(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.service = LifeDataService(self.tmp / "gremlin.db", refresh_on_startup=False)
        with self.service.write_connection() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS import_batch (import_batch_id INTEGER PRIMARY KEY, status TEXT, "
                "import_started_at TEXT, import_completed_at TEXT)"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS raw_cmms_record ("
                "raw_record_id INTEGER PRIMARY KEY, import_batch_id INTEGER NOT NULL DEFAULT 1, raw_json TEXT NOT NULL)"
            )
            conn.execute("INSERT INTO import_batch (import_batch_id, status) VALUES (1, 'COMPLETED')")
            self.mode_id = int(conn.execute("INSERT INTO failure_mode (failure_mode_name) VALUES ('Clamp')").lastrowid)
            self.mechanism_id = int(
                conn.execute(
                    "INSERT INTO failure_mechanism (failure_mechanism_name, failure_mode_id) VALUES ('Seal wear', ?)",
                    (self.mode_id,),
                ).lastrowid
            )
        self._raw_id = 0

    def _add(self, task_id: str, completed: str | None, *, pm: bool = False) -> int:
        self._raw_id += 1
        with self.service.write_connection() as conn:
            conn.execute(
                "INSERT INTO raw_cmms_record (raw_record_id, import_batch_id, raw_json) VALUES (?, 1, '{}')",
                (self._raw_id,),
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
                    ) VALUES (?, 'PM', 'INCLUDED_PM_RESET_EVENT', 1, 1, ?, ?, 'APPROVED_RESET', 'Seals replaced to spec.')
                    """,
                    (mapped_id, self.mode_id, self.mechanism_id),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO event_disposition (
                        mapped_record_id, record_class_final, disposition_category, include_in_event_processing,
                        include_in_weibull_candidate, failure_mode_id, failure_mechanism_id
                    ) VALUES (?, 'CORRECTIVE_WO', 'INCLUDED_FAILURE', 1, 1, ?, ?)
                    """,
                    (mapped_id, self.mode_id, self.mechanism_id),
                )
        return mapped_id

    def _add_all(self, records) -> None:
        for task_id, completed in records:
            self._add(task_id, completed)

class PlantTimeLifeHoursTests(unittest.TestCase):
    def test_the_dat_003_example_holds_on_the_plant_clock(self):
        # REL-WBL-DAT-003 §7.1, Table 1: 2022-09-17 4:29 PM to 2023-01-02 2:05 PM is
        # 2566.6 raw hours with 752.5 of them at weekends, leaving 1814.1 life hours on
        # a 24-hour Monday-Friday asset and 1511.7 on the 20-hour plant default. The
        # interval spans the November clock change, so the raw and weekend hours are
        # each one more than a wall-clock count.
        start = datetime(2022, 9, 17, 16, 29, tzinfo=PLANT).astimezone(timezone.utc)
        end = datetime(2023, 1, 2, 14, 5, tzinfo=PLANT).astimezone(timezone.utc)

        life, weekend, non_run = LifeDataService._scheduled_life_hours(start, end, 24.0, tz=PLANT)

        self.assertAlmostEqual(life, 1814.1, places=1)
        self.assertAlmostEqual(weekend, 752.5, places=1)
        self.assertAlmostEqual((end - start).total_seconds() / 3600, 2566.6, places=1)
        self.assertEqual(non_run, 0)
        self.assertAlmostEqual(LifeDataService._scheduled_life_hours(start, end, 20.0, tz=PLANT)[0], 1511.7, places=1)

    def test_the_weekend_is_the_plants_saturday_and_sunday(self):
        # Friday 20:00 to Monday 06:00 plant time on a 24-hour asset: four Friday
        # hours and six Monday ones. Split at midnight UTC, the weekend would start at
        # 19:00 Friday and end at 19:00 Sunday instead, counting eleven hours.
        start = datetime(2026, 9, 18, 20, 0, tzinfo=PLANT).astimezone(timezone.utc)
        end = datetime(2026, 9, 21, 6, 0, tzinfo=PLANT).astimezone(timezone.utc)

        plant_life, _, _ = LifeDataService._scheduled_life_hours(start, end, 24.0, tz=PLANT)
        utc_life, _, _ = LifeDataService._scheduled_life_hours(start, end, 24.0)

        self.assertAlmostEqual(plant_life, 10.0)
        self.assertAlmostEqual(utc_life, 11.0)


class FitGateTests(_Seeded):
    def test_there_is_no_substitute_for_a_maximum_likelihood_beta(self):
        service = LifeDataService.__new__(LifeDataService)
        with self.assertRaisesRegex(WeibullFitError, "did not converge"):
            service._fit_weibull_2p([(100.0, 1)] * 6)

class ProbabilityPlotRSquaredTests(_Seeded):
    @staticmethod
    def _points(xys):
        return [{"weibull_plot_x": x, "weibull_plot_y": y} for x, y in xys]

    def test_r_squared_is_the_squared_correlation_of_the_plotted_points(self):
        lives = [(520.0, 1), (450.0, 0), (310.0, 1), (980.0, 1), (760.0, 1), (640.0, 1), (1050.0, 0)]
        km = self.service._kaplan_meier_points(lives)
        drawn = [(p["weibull_plot_x"], p["weibull_plot_y"]) for p in km if p["weibull_plot_y"] is not None]

        r_squared = LifeDataService._probability_plot_r_squared(km)

        self.assertAlmostEqual(r_squared, statistics.correlation(*zip(*drawn)) ** 2, places=12)
        self.assertAlmostEqual(r_squared, 0.990, places=3)
        # Points on a line give exactly 1, whatever the line.
        self.assertAlmostEqual(
            LifeDataService._probability_plot_r_squared(self._points([(5.0, -2.0), (6.0, -0.5), (7.0, 1.0), (8.0, 2.5)])), 1.0
        )

    def test_points_the_plot_cannot_draw_are_left_out_and_two_points_are_not_enough(self):
        # A Kaplan-Meier estimate of 0 has no place on the plot (y is None), so not in R² either.
        on_a_line = self._points([(5.0, -2.0), (6.0, -1.0), (7.0, 0.0), (8.0, None)])
        self.assertAlmostEqual(LifeDataService._probability_plot_r_squared(on_a_line), 1.0)
        self.assertIsNone(LifeDataService._probability_plot_r_squared(self._points([(5.0, -2.0), (6.0, -1.0), (7.0, None)])))
        self.assertIsNone(LifeDataService._probability_plot_r_squared(self._points([(5.0, -2.0), (5.0, -1.0), (5.0, 0.0)])))

    def test_an_existing_database_gets_the_column(self):
        path = self.tmp / "older.db"
        with sqlite3.connect(path) as conn:
            conn.execute(
                "CREATE TABLE weibull_result (weibull_result_id INTEGER PRIMARY KEY, weibull_analysis_run_id INTEGER NOT NULL, "
                "beta_mle REAL, eta_mle REAL, b10_life REAL, b50_life REAL, created_at TEXT NOT NULL DEFAULT (datetime('now')))"
            )
        LifeDataService(path, refresh_on_startup=False)
        with sqlite3.connect(path) as conn:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(weibull_result)")}
        self.assertIn("probability_plot_r_squared", columns)

    def test_the_review_threshold_rises_with_the_failure_count_and_can_be_regenerated(self):
        table = [value for _, value in R_SQUARED_REVIEW_THRESHOLDS]
        self.assertEqual(table, sorted(table))
        self.assertAlmostEqual(LifeDataService.r_squared_review_threshold(5), 0.806)
        self.assertAlmostEqual(LifeDataService.r_squared_review_threshold(11), (0.846 + 0.861) / 2)
        self.assertAlmostEqual(LifeDataService.r_squared_review_threshold(500), 0.976)
        # The table is what the documented simulation gives; a smaller run of it lands close.
        for failures, value in ((5, 0.806), (20, 0.893)):
            self.assertAlmostEqual(LifeDataService.simulated_r_squared_threshold(failures, samples=4000, seed=7), value, delta=0.01)

class ScheduleRegisterTests(_Seeded):
    """REL-WBL-DAT-003 §7: the plant default schedule, and the register of assets off it."""

    def test_the_built_in_24_hour_assets_seed_the_register_once(self):
        register = self.service.weibull_schedule_register()

        self.assertEqual(register["default_code"], "20H_MON_FRI")
        self.assertEqual([s["code"] for s in register["schedules"]], ["20H_MON_FRI", "24H_MON_FRI", "CONTINUOUS"])
        self.assertEqual(
            {(row["asset_number"], row["schedule_code"]) for row in register["assignments"]},
            {(asset, "24H_MON_FRI") for asset in ("3101", "3102", "3103", "3104", "3105", "3106", "3107", "3154", "3142", "3023", "3253")},
        )
        self.assertTrue(all(change["changed_by"] == "GREMLIN" for change in register["history"]))
        # Emptied on purpose, it stays empty: the change record shows a register was kept.
        with self.service.write_connection() as conn:
            conn.execute("DELETE FROM asset_schedule_assignment")
        self.assertEqual(LifeDataService(self.service.db_path, refresh_on_startup=False).weibull_schedule_register()["assignments"], [])

    def test_a_change_needs_a_known_asset_a_listed_schedule_a_reason_and_a_difference(self):
        self._add_all(MONTHLY)
        for asset, code, reason, message in (
            ("NOPE", "24H_MON_FRI", "x", "no Limble records"),
            ("A-1", "RAW_ELAPSED_ONLY", "x", "listed schedules"),
            ("A-1", "24H_MON_FRI", "  ", "Say why"),
            ("A-1", "20H_MON_FRI", "x", "already on that schedule"),
        ):
            with self.assertRaisesRegex(ValueError, message):
                self.service.set_asset_weibull_schedule(asset, code, reason=reason)


class IntervalRuleTests(unittest.TestCase):
    """REL-WBL-MTH-001 §8: an interval counts as stable by its width relative to its estimate."""

    def setUp(self) -> None:
        self.service = LifeDataService.__new__(LifeDataService)

    def test_a_beta_interval_is_judged_against_beta_itself(self):
        # 40 failures at beta 3: about 2.36 to 3.82, 49% of beta. Stable, though 1.46 wide.
        self.assertIn("no wider than 70% of beta", self.service._beta_ci_recommendation(2.36, 3.82, 3.0))
        # The pilot's SQ87 interval, 0.488 to 0.732, is 42% of beta 0.574.
        self.assertIn("no wider than 70% of beta", self.service._beta_ci_recommendation(0.488, 0.732, 0.574))
        # The Documentation worked example: 1.350 to 5.558 is 154% of beta 2.739.
        self.assertIn("wider than 70% of beta", self.service._beta_ci_recommendation(1.350, 5.558, 2.739))
        self.assertNotIn("no wider", self.service._beta_ci_recommendation(1.350, 5.558, 2.739))
        # Crossing 1 is read first, whatever the width.
        self.assertIn("crossing 1.0", self.service._beta_ci_recommendation(0.95, 1.05, 1.0))

    def test_an_eta_interval_is_stable_up_to_40_percent_of_eta(self):
        self.assertIn("reasonably tight", self.service._eta_ci_recommendation(800.0, 1200.0, 1000.0))
        self.assertIn("directional guidance only", self.service._eta_ci_recommendation(700.0, 1200.0, 1000.0))


def test_the_schedule_register_endpoint_needs_an_editor_and_keeps_who(monkeypatch, tmp_path):
    monkeypatch.setenv("GREMLIN_DB_PATH", str(tmp_path / "gremlin.db"))
    monkeypatch.setenv("GREMLIN_ACCESS_DB_PATH", str(tmp_path / "accesscontrol.db"))
    monkeypatch.setenv("GREMLIN_ADMIN_USERNAME", "root")
    monkeypatch.setenv("GREMLIN_ADMIN_PIN", "secret")
    import app

    module = importlib.reload(app)
    service = module.get_life_data_service()
    with service.write_connection() as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS import_batch (import_batch_id INTEGER PRIMARY KEY, status TEXT)")
        conn.execute("INSERT OR IGNORE INTO import_batch (import_batch_id, status) VALUES (1, 'COMPLETED')")
        conn.execute("CREATE TABLE IF NOT EXISTS raw_cmms_record (raw_record_id INTEGER PRIMARY KEY, import_batch_id INTEGER NOT NULL DEFAULT 1, raw_json TEXT NOT NULL)")
        conn.execute("INSERT INTO raw_cmms_record (raw_record_id, import_batch_id, raw_json) VALUES (1, 1, '{}')")
        conn.execute("INSERT INTO mapped_cmms_record (raw_record_id, import_batch_id, asset_number, task_id) VALUES (1, 1, 'C-7', 'T1')")
    client = module.app.test_client()
    change = {"asset": "C-7", "schedule_code": "CONTINUOUS", "reason": "Compressor runs all week."}

    assert client.post("/life-data-analysis/api/schedule-register", json=change).status_code == 401
    # The register names who made each change, so reading it needs an account too.
    assert client.get("/life-data-analysis/api/schedule-register").status_code == 401
    assert client.post("/auth/login", json={"username": "root", "pin": "secret"}).status_code == 200
    saved = client.post("/life-data-analysis/api/schedule-register", json=change)

    assert saved.status_code == 200
    assert saved.get_json()["history"][0]["changed_by"] == "root"
    register = client.get("/life-data-analysis/api/schedule-register").get_json()
    assert {"asset_number": "C-7", "schedule_code": "CONTINUOUS"}.items() <= next(
        row for row in register["assignments"] if row["asset_number"] == "C-7"
    ).items()


if __name__ == "__main__":
    unittest.main()
