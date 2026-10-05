"""The Weibull method rules GREMLIN now shares with the REL-WBL document series.

Each test pins one rule to the document that sets it:

* life hours split days at midnight on the plant's clock (REL-WBL-DAT-003 §4, §7.1);
* the current life is censored at the last completed Limble import unless a cutoff
  date is entered, and the analysis window is a pair of plant-calendar days
  (REL-WBL-DAT-003 §10, §13);
* a fit needs five lives ending in a failure (REL-WBL-VAL-001 VV-071), and there
  is no substitute for a maximum-likelihood beta (REL-WBL-MTH-001 §5.5);
* lives and events carry REL-WBL-DAT-004 §11's notes, and short lives are flagged
  for the §12 duplicate check;
* each run is stamped with its method version, and a result saved under an earlier
  one, or below the minimum, is neither ranked nor reported;
* a report carries REL-WBL-MTH-001 §10's contents, and a failure-mode report the
  reason a mechanism was not fitted (REL-WBL-PLN-003 §8);
* every fit reports its probability plot's R², the squared correlation of the
  Kaplan-Meier failure points in Weibull coordinates (REL-WBL-REQ-001 VV-070,
  VV-078).
"""

import importlib
import json
import sqlite3
import statistics
import tempfile
import unittest
import zipfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from xml.etree import ElementTree as ET
from zoneinfo import ZoneInfo

from services.life_data_service import (
    MIN_WEIBULL_FAILURE_LIVES,
    WEIBULL_METHOD_VERSION,
    LifeDataService,
    WeibullFitError,
)

PLANT = ZoneInfo("America/Chicago")
WORD = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


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

    def _import_completed(self, when: str | None, status: str = "COMPLETED") -> None:
        with self.service.write_connection() as conn:
            conn.execute(
                "INSERT INTO import_batch (status, import_started_at, import_completed_at) VALUES (?, ?, ?)",
                (status, when, when),
            )

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

    def _perform(self, *, mode_level: bool = False, **window):
        if mode_level:
            return self.service.perform_weibull_analysis("A-1", grouping_level="FAILURE_MODE", failure_mode_id=self.mode_id, **window)
        return self.service.perform_weibull_analysis(
            "A-1",
            grouping_level="FAILURE_MECHANISM",
            failure_mode_id=self.mode_id,
            failure_mechanism_id=self.mechanism_id,
            **window,
        )

    def _saved(self):
        return self.service.load_saved_weibull_analysis(
            "A-1", grouping_level="FAILURE_MECHANISM", failure_mode_id=self.mode_id, failure_mechanism_id=self.mechanism_id
        )


class PlantTimeLifeHoursTests(unittest.TestCase):
    def test_the_dat_003_example_holds_on_the_plant_clock(self):
        # REL-WBL-DAT-003 §7.1: 2022-09-17 4:29 PM to 2023-01-02 2:05 PM on a
        # 24-hour Monday-Friday asset is 1814.1 schedule-adjusted hours. The interval
        # spans the November clock change, so it really holds one more weekend hour
        # than the document's wall-clock arithmetic counts: 752.5 rather than 751.5.
        start = datetime(2022, 9, 17, 16, 29, tzinfo=PLANT).astimezone(timezone.utc)
        end = datetime(2023, 1, 2, 14, 5, tzinfo=PLANT).astimezone(timezone.utc)

        life, weekend, non_run = LifeDataService._scheduled_life_hours(start, end, 24.0, tz=PLANT)

        self.assertAlmostEqual(life, 1814.1, places=1)
        self.assertAlmostEqual(weekend, 752.5, places=1)
        self.assertAlmostEqual((end - start).total_seconds() / 3600, 2566.6, places=1)
        self.assertEqual(non_run, 0)

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


class AnalysisWindowTests(_Seeded):
    def test_the_current_life_is_censored_at_the_last_completed_import(self):
        self._add_all(MONTHLY)
        self._import_completed("2025-08-01 12:00:00")
        self._import_completed("2025-09-01 12:00:00", status="FAILED")

        result = self._perform()

        self.assertEqual(result.analysis_cutoff_source, "LAST_IMPORT")
        self.assertEqual(result.analysis_cutoff, "2025-08-01T12:00:00+00:00")
        current = [obs for obs in result.observations if obs["observation_type"] == "RIGHT_CENSORED_LIFE"]
        self.assertEqual(len(current), 1)
        self.assertEqual(current[0]["analysis_cutoff_datetime"], "2025-08-01T12:00:00+00:00")

    def test_data_newer_than_the_last_import_censors_at_the_time_of_the_run(self):
        self._add_all(MONTHLY)
        self._import_completed("2025-06-01 12:00:00")  # older than F7

        result = self._perform()

        self.assertEqual(result.analysis_cutoff_source, "NOW")
        cutoff = datetime.fromisoformat(result.analysis_cutoff)
        self.assertLess(abs((datetime.now(timezone.utc) - cutoff).total_seconds()), 120)

    def test_a_window_is_whole_plant_days_and_events_outside_it_are_listed(self):
        self._add_all([("F0", _utc(2024, 12, 2, 9))] + MONTHLY + [("F8", _utc(2025, 7, 21, 10))])

        result = self._perform(analysis_start=date(2025, 1, 1), analysis_cutoff=date(2025, 6, 30))

        self.assertEqual(result.analysis_cutoff_source, "USER")
        self.assertEqual(result.analysis_start, datetime(2025, 1, 1, tzinfo=PLANT).astimezone(timezone.utc).isoformat())
        self.assertEqual(result.analysis_cutoff, datetime(2025, 7, 1, tzinfo=PLANT).astimezone(timezone.utc).isoformat())
        notes = {event["task_id"]: event["weibull_life_note"] for event in result.events}
        self.assertEqual(notes["F0"], "Excluded - before analysis start date")
        self.assertEqual(notes["F8"], "Excluded - after analysis cutoff")
        self.assertEqual(result.failure_count, len(MONTHLY) - 1)

    def test_a_window_has_to_make_sense(self):
        self._add_all(MONTHLY)
        today = datetime.now(PLANT).date()
        with self.assertRaisesRegex(ValueError, "start date has to be before the cutoff"):
            self._perform(analysis_start=date(2025, 6, 1), analysis_cutoff=date(2025, 5, 1))
        with self.assertRaisesRegex(ValueError, "cutoff date can't be later than today"):
            self._perform(analysis_cutoff=today + timedelta(days=1))
        with self.assertRaisesRegex(ValueError, "start date can't be later than today"):
            self._perform(analysis_start=today + timedelta(days=1))


class FitGateTests(_Seeded):
    def test_fewer_than_five_failure_lives_are_refused_and_the_old_result_removed(self):
        mapped = [self._add(task_id, completed) for task_id, completed in MONTHLY]
        self.assertIsNotNone(self._perform())
        self.assertIsNotNone(self._saved())
        # Two failures move to another disposition: five failures, four lives.
        with self.service.write_connection() as conn:
            conn.execute(
                "UPDATE event_disposition SET include_in_weibull_candidate = 0 WHERE mapped_record_id IN (?, ?)",
                (mapped[-1], mapped[-2]),
            )

        with self.assertRaises(WeibullFitError) as caught:
            self._perform()

        message = str(caught.exception)
        self.assertIn(f"at least {MIN_WEIBULL_FAILURE_LIVES} lives that end in a failure", message)
        self.assertIn("has 4", message)
        self.assertIn("the first only starts the clock", message)
        self.assertIn("has been removed", message)
        self.assertIsNone(self._saved())

    def test_there_is_no_substitute_for_a_maximum_likelihood_beta(self):
        service = LifeDataService.__new__(LifeDataService)
        with self.assertRaisesRegex(WeibullFitError, "did not converge"):
            service._fit_weibull_2p([(100.0, 1)] * 6)

    def test_groups_too_small_to_fit_are_listed_as_such(self):
        self._add_all(MONTHLY[:4])

        groups = self.service.weibull_group_options("A-1")

        self.assertTrue(groups)
        self.assertTrue(all(group["fittable"] is False for group in groups))


class LifeNoteTests(_Seeded):
    def test_lives_and_events_carry_the_dat_004_notes(self):
        self._add_all(MONTHLY[:3])
        self._add("PM1", _utc(2025, 3, 17, 9), pm=True)
        self._add_all(MONTHLY[3:])
        self._add("PM2", _utc(2025, 7, 7, 9), pm=True)

        result = self._perform()

        by_closing = {obs["source_task_id"]: obs for obs in result.observations}
        self.assertEqual(by_closing["F2"]["weibull_life_note"], "Valid completed life from prior same-population event")
        self.assertEqual(by_closing["PM1"]["observation_type"], "PM_RESET_CENSORED_LIFE")
        self.assertEqual(by_closing["PM1"]["weibull_life_note"], "Censored interval ended by PM reset event")
        self.assertEqual(by_closing["F4"]["weibull_life_note"], "Valid completed life from PM reset event")
        current = by_closing[None]
        self.assertEqual(current["weibull_life_note"], "PM reset censor to analysis cutoff date")
        self.assertEqual(result.pm_reset_censored_count, 2)
        self.assertEqual(result.current_life_censored_count, 1)
        first = result.events[0]
        self.assertEqual(first["weibull_life_note"], "Initial occurrence - no prior comparable start point available")
        # Raw elapsed hours, and what was taken out of them, travel with each life.
        f2 = by_closing["F2"]
        self.assertAlmostEqual(
            f2["life_hours_raw_elapsed"] - f2["excluded_weekend_hours"] - f2["excluded_schedule_non_run_hours"],
            f2["life_hours_for_weibull"],
        )

    def test_a_life_within_an_hour_is_flagged_for_a_duplicate_check(self):
        self._add_all(MONTHLY)
        self._add("F7-again", _utc(2025, 6, 23, 10, 25))

        result = self._perform()

        flagged = [obs for obs in result.observations if obs["data_quality_assumption_flag"]]
        self.assertEqual([obs["source_task_id"] for obs in flagged], ["F7-again"])
        self.assertIn("Check for duplicate", flagged[0]["data_quality_assumption_flag"])
        event = next(event for event in result.events if event["task_id"] == "F7-again")
        self.assertIn("Check for duplicate", event["data_quality_assumption_flag"])


class MethodVersionTests(_Seeded):
    def _mark_saved_runs(self, version: str) -> None:
        with sqlite3.connect(self.service.db_path) as conn:
            conn.execute("UPDATE weibull_analysis_run SET code_version = ?", (version,))

    def test_a_run_is_stamped_with_its_method_and_code(self):
        self._add_all(MONTHLY)

        result = self._perform()

        self.assertEqual(result.method_version, WEIBULL_METHOD_VERSION)
        self.assertTrue(result.method_current)
        self.assertTrue(result.software_version.startswith("GREMLIN "))
        self.assertNotIn("PyQt", result.software_version)
        self.assertEqual(result.life_basis["time_zone"], "America/Chicago")
        self.assertEqual(result.life_basis["schedule_code"], "20H_MON_FRI")

    def test_an_earlier_methods_result_is_flagged_and_still_ranked_with_a_marker(self):
        self._add_all(MONTHLY)
        self._perform()
        self._mark_saved_runs("life-data-v1")

        saved = self._saved()
        [ranked] = self.service.latest_failure_mechanism_beta_rankings("A-1")

        self.assertFalse(saved.method_current)
        self.assertFalse(ranked["method_current"])

    def test_a_result_below_the_minimum_is_not_ranked(self):
        self._add_all(MONTHLY)
        self._perform()
        with sqlite3.connect(self.service.db_path) as conn:
            conn.execute("UPDATE weibull_result SET failure_count = 3")

        self.assertEqual(self.service.latest_failure_mechanism_beta_rankings("A-1"), [])
        self.assertFalse(self._saved().meets_minimum)


class ReportTests(_Seeded):
    def _report_count(self) -> int:
        with self.service.connect() as conn:
            return conn.execute("SELECT COUNT(*) FROM weibull_report_log").fetchone()[0]

    def _report_text(self, payload) -> str:
        path = self.tmp / "report.docx"
        self.service.build_weibull_report_docx("A-1", payload, path)
        root = ET.fromstring(zipfile.ZipFile(path).read("word/document.xml"))
        return "\n".join("".join(t.text or "" for t in p.iter(f"{WORD}t")) for p in root.iter(f"{WORD}p"))

    def test_the_report_carries_the_analysis_package(self):
        self._add_all(MONTHLY)
        result = self._perform(analysis_cutoff=date(2025, 7, 31))

        text = self._report_text({"result_id": result.result_id, "target_age_hours": 300})

        for expected in (
            "Pre-release result",
            "Grouping level: Failure mechanism",
            "Schedule-adjusted elapsed hours: an exposure proxy, not run-meter hours",
            "20 hours Monday-Friday, weekends excluded",
            "Midnight, America/Chicago",
            "End of 2025-07-31 (America/Chicago), the cutoff date entered for the run",
            "B50 life (median)",
            "Reliability at 300 hours",
            "Limitations and Assumptions",
            "Observation Data",
            "Valid completed life from prior same-population event",
            f"method {WEIBULL_METHOD_VERSION}",
        ):
            self.assertIn(expected, text)

    def test_a_failure_mode_report_needs_its_rationale_and_keeps_it(self):
        self._add_all(MONTHLY)
        result = self._perform(mode_level=True)

        with self.assertRaisesRegex(ValueError, "REL-WBL-PLN-003"):
            self._report_text({"result_id": result.result_id})
        self.assertEqual(self._report_count(), 0)  # refused before a number was used

        text = self._report_text({"result_id": result.result_id, "fallback_rationale": "Notes don't name the seal."})
        self.assertIn("Why a failure mode rather than a mechanism: Notes don't name the seal.", text)
        saved = self.service.load_saved_weibull_analysis("A-1", grouping_level="FAILURE_MODE", failure_mode_id=self.mode_id)
        self.assertEqual(saved.fallback_rationale, "Notes don't name the seal.")
        # The kept reason serves the next report without being asked for again.
        self.assertIn("Notes don't name the seal.", self._report_text({"result_id": result.result_id}))

    def test_an_earlier_methods_result_or_a_bad_age_is_refused_before_numbering(self):
        self._add_all(MONTHLY)
        result = self._perform()
        with self.assertRaisesRegex(ValueError, "positive number of hours"):
            self._report_text({"result_id": result.result_id, "target_age_hours": "-5"})
        with sqlite3.connect(self.service.db_path) as conn:
            conn.execute("UPDATE weibull_analysis_run SET code_version = 'life-data-v1'")
        with self.assertRaisesRegex(ValueError, "earlier version"):
            self._report_text({"result_id": result.result_id})
        self.assertEqual(self._report_count(), 0)


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

    def test_a_run_saves_r_squared_and_shows_it_everywhere_the_fit_is_read(self):
        self._add_all(MONTHLY)

        result = self._perform()

        expected = LifeDataService._probability_plot_r_squared(result.km_points)
        self.assertIsNotNone(expected)
        self.assertAlmostEqual(result.probability_plot_r_squared, expected, places=12)
        self.assertAlmostEqual(self._saved().probability_plot_r_squared, expected, places=12)
        [row] = [row for row in result.interpretation_summary if row["metric"] == "Probability plot R²"]
        self.assertEqual(row["value"], f"{expected:.3f}")
        self.assertIn("not part of the fit", row["recommendation"])
        [ranked] = self.service.latest_failure_mechanism_beta_rankings("A-1")
        self.assertAlmostEqual(ranked["probability_plot_r_squared"], expected, places=12)

    def test_a_result_saved_before_r_squared_was_stored_works_it_out_from_its_points(self):
        self._add_all(MONTHLY)
        result = self._perform()
        with sqlite3.connect(self.service.db_path) as conn:
            summary = [row for row in result.interpretation_summary if row["metric"] != "Probability plot R²"]
            conn.execute(
                "UPDATE weibull_result SET probability_plot_r_squared = NULL, engineering_interpretation = ?",
                (json.dumps(summary),),
            )

        saved = self._saved()

        self.assertAlmostEqual(saved.probability_plot_r_squared, result.probability_plot_r_squared, places=12)
        self.assertEqual(saved.interpretation_summary[-1]["metric"], "Probability plot R²")
        self.assertIsNone(self.service.latest_failure_mechanism_beta_rankings("A-1")[0]["probability_plot_r_squared"])

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

    def test_the_report_states_r_squared(self):
        self._add_all(MONTHLY)
        result = self._perform()
        path = self.tmp / "report.docx"
        self.service.build_weibull_report_docx("A-1", {"result_id": result.result_id}, path)
        root = ET.fromstring(zipfile.ZipFile(path).read("word/document.xml"))
        cells = ["".join(t.text or "" for t in tc.iter(f"{WORD}t")) for tc in root.iter(f"{WORD}tc")]

        index = cells.index("Probability plot R²")
        self.assertTrue(cells[index + 1].startswith(f"{result.probability_plot_r_squared:.3f} ("), cells[index + 1])


def test_the_run_endpoint_takes_a_window_and_rejects_a_bad_date(monkeypatch, tmp_path):
    monkeypatch.setenv("GREMLIN_DB_PATH", str(tmp_path / "gremlin.db"))
    monkeypatch.setenv("GREMLIN_ACCESS_DB_PATH", str(tmp_path / "accesscontrol.db"))
    monkeypatch.setenv("GREMLIN_ADMIN_USERNAME", "root")
    monkeypatch.setenv("GREMLIN_ADMIN_PIN", "secret")
    import app

    module = importlib.reload(app)
    seeded = _Seeded("run")
    seeded.service = module.get_life_data_service()
    seeded._raw_id = 0
    with seeded.service.write_connection() as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS import_batch (import_batch_id INTEGER PRIMARY KEY, status TEXT)")
        conn.execute("INSERT OR IGNORE INTO import_batch (import_batch_id, status) VALUES (1, 'COMPLETED')")
        conn.execute("CREATE TABLE IF NOT EXISTS raw_cmms_record (raw_record_id INTEGER PRIMARY KEY, import_batch_id INTEGER NOT NULL DEFAULT 1, raw_json TEXT NOT NULL)")
        seeded.mode_id = int(conn.execute("INSERT INTO failure_mode (failure_mode_name) VALUES ('Clamp')").lastrowid)
        seeded.mechanism_id = int(
            conn.execute(
                "INSERT INTO failure_mechanism (failure_mechanism_name, failure_mode_id) VALUES ('Seal wear', ?)",
                (seeded.mode_id,),
            ).lastrowid
        )
    seeded._add_all(MONTHLY)
    client = module.app.test_client()
    assert client.post("/auth/login", json={"username": "root", "pin": "secret"}).status_code == 200
    body = {
        "asset": "A-1",
        "grouping_level": "FAILURE_MECHANISM",
        "failure_mode_id": seeded.mode_id,
        "failure_mechanism_id": seeded.mechanism_id,
    }

    bad = client.post("/life-data-analysis/api/perform-analysis", json={**body, "analysis_cutoff": "31/07/2025"})
    assert bad.status_code == 400
    assert "YYYY-MM-DD" in bad.get_json()["error"]

    ran = client.post("/life-data-analysis/api/perform-analysis", json={**body, "analysis_cutoff": "2025-07-31", "analysis_start": ""})
    assert ran.status_code == 200
    result = ran.get_json()["result"]
    assert result["analysis_cutoff_source"] == "USER"
    assert result["analysis_start"] is None
    assert result["events"] and result["life_basis"]["time_zone"] == "America/Chicago"


if __name__ == "__main__":
    unittest.main()
