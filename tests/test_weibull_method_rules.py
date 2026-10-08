"""The Weibull method rules GREMLIN now shares with the REL-WBL document series.

Each test pins one rule to the document that sets it:

* life hours split days at midnight on the plant's clock (REL-WBL-DAT-003 §7, §7.1);
* the current life is censored at the last completed Limble import unless a cutoff
  date is entered, and the analysis window is a pair of plant-calendar days
  (REL-WBL-DAT-003 §10);
* a fit needs five lives ending in a failure (REL-WBL-MTH-001 §4, REL-WBL-REQ-001
  VV-071), and there is no substitute for a maximum-likelihood beta
  (REL-WBL-MTH-001 §5.5);
* lives and events carry REL-WBL-DAT-004 §11's notes, and short lives are flagged
  for the §12 duplicate check;
* each run is stamped with its method version, and a result saved under an earlier
  one, or below the minimum, is neither ranked nor reported;
* a report carries REL-WBL-MTH-001 §10's contents, and a failure-mode report the
  reason a mechanism was not fitted (REL-WBL-PLN-003 §8);
* every fit reports its probability plot's R², the squared correlation of the
  Kaplan-Meier failure points in Weibull coordinates (REL-WBL-REQ-001 VV-070,
  VV-078), flagged for review below the value 90% of genuine Weibull samples of
  its size reach (VV-074);
* an interval counts as stable by its width relative to its estimate (REL-WBL-MTH-001 §8);
* a PM reset restarts only what it restores (REL-WBL-DAT-004 §7);
* life hours are counted on the asset's schedule in the Weibull schedule register,
  the plant default otherwise, and a result counted on a schedule the asset has
  left says so (REL-WBL-DAT-003 §7);
* the asset's mechanisms are ranked by beta and by the chance of failing in the
  next few weeks (REL-WBL-MTH-001 §8.1).
"""

import importlib
import json
import math
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
    R_SQUARED_REVIEW_THRESHOLDS,
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

    def test_an_entered_cutoff_leaves_out_an_event_at_the_next_plant_midnight(self):
        # A cutoff of June 30 runs to midnight starting July 1, which is July's.
        self._add_all(MONTHLY + [("F8", _utc(2025, 7, 1, 0))])

        result = self._perform(analysis_cutoff=date(2025, 6, 30))

        notes = {event["task_id"]: event["weibull_life_note"] for event in result.events}
        self.assertEqual(notes["F8"], "Excluded - after analysis cutoff")
        self.assertEqual(result.failure_count, len(MONTHLY) - 1)

    def test_an_event_at_the_last_import_is_inside_the_window(self):
        self._add_all(MONTHLY)
        self._import_completed(MONTHLY[-1][1])

        result = self._perform()

        self.assertEqual(result.analysis_cutoff_source, "LAST_IMPORT")
        notes = {event["task_id"]: event["weibull_life_note"] for event in result.events}
        self.assertFalse(notes["F7"].startswith("Excluded"), notes["F7"])

    def test_a_date_with_no_time_is_that_day_on_the_plant_calendar(self):
        # F1 and F8 carry a bare date. F1's day is the start date, so it is in the
        # window, and F8's is the day after the cutoff, so it is out; read as
        # midnight UTC, the evening before on the plant's clock, both would flip.
        self._add("F1", "2025-01-06")
        self._add_all(MONTHLY[1:])
        self._add("F8", "07/01/2025")

        result = self._perform(analysis_start=date(2025, 1, 6), analysis_cutoff=date(2025, 6, 30))

        events = {event["task_id"]: event for event in result.events}
        self.assertEqual(events["F1"]["completed_date_parsed"], datetime(2025, 1, 6, tzinfo=PLANT).astimezone(timezone.utc).isoformat())
        self.assertEqual(events["F1"]["weibull_life_note"], "Initial occurrence - no prior comparable start point available")
        self.assertEqual(events["F8"]["weibull_life_note"], "Excluded - after analysis cutoff")
        self.assertEqual(result.failure_count, len(MONTHLY) - 1)

    def test_a_date_with_no_time_is_sequenced_by_its_plant_day(self):
        # FS closes on Sunday evening on the plant's clock, 03:00 UTC on Monday. F3
        # is that Monday with no time, so it comes after FS, though as text, and as
        # midnight UTC, it would sort before it.
        self._add_all(MONTHLY[:2])
        self._add("FS", _utc(2025, 3, 2, 21))
        self._add("F3", "2025-03-03")
        self._add_all(MONTHLY[3:])

        result = self._perform()

        sequence = {event["task_id"]: event["weibull_sequence_number"] for event in result.events}
        self.assertLess(sequence["FS"], sequence["F3"])
        self.assertEqual(sorted(sequence, key=sequence.get), ["F1", "F2", "FS", "F3", "F4", "F5", "F6", "F7"])

    def test_a_start_and_cutoff_on_the_same_day_is_a_one_day_window(self):
        # Six failures on one Tuesday, closer together on some gaps than others.
        hours = [(8, 0), (9, 30), (11, 0), (13, 15), (15, 0), (17, 40)]
        self._add_all([(f"D{i}", _utc(2025, 3, 4, h, m)) for i, (h, m) in enumerate(hours)])
        self._add("NEXT", _utc(2025, 3, 5, 9))

        result = self._perform(analysis_start=date(2025, 3, 4), analysis_cutoff=date(2025, 3, 4))

        self.assertEqual(result.analysis_start, datetime(2025, 3, 4, tzinfo=PLANT).astimezone(timezone.utc).isoformat())
        self.assertEqual(result.analysis_cutoff, datetime(2025, 3, 5, tzinfo=PLANT).astimezone(timezone.utc).isoformat())
        self.assertEqual(result.failure_count, len(hours) - 1)
        notes = {event["task_id"]: event["weibull_life_note"] for event in result.events}
        self.assertEqual(notes["NEXT"], "Excluded - after analysis cutoff")

    def test_a_window_has_to_make_sense(self):
        self._add_all(MONTHLY)
        today = datetime.now(PLANT).date()
        with self.assertRaisesRegex(ValueError, "start date can't be later than the cutoff date"):
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


class GroupFittableTests(_Seeded):
    """The Perform Analysis dialog only offers a group that could reach the minimum."""

    def _fittable(self):
        groups = self.service.weibull_group_options("A-1")
        return {group["grouping_level"]: group["fittable"] for group in groups}

    def test_five_failures_with_no_reset_are_four_lives_and_not_offered(self):
        # The first failure only starts the clock, so the run would refuse it.
        self._add_all(MONTHLY[:5])
        self.assertEqual(self._fittable(), {"FAILURE_MODE": False, "FAILURE_MECHANISM": False})
        with self.assertRaises(WeibullFitError):
            self._perform()

    def test_six_failures_with_no_reset_are_offered(self):
        self._add_all(MONTHLY[:6])
        self.assertTrue(self._fittable()["FAILURE_MECHANISM"])
        self.assertEqual(self._perform().failure_count, 5)

    def test_five_failures_after_a_reset_are_offered(self):
        # A PM reset first starts the clock, so all five failures end a life.
        self._add("PM0", _utc(2024, 12, 2, 9), pm=True)
        self._add_all(MONTHLY[:5])
        self.assertTrue(self._fittable()["FAILURE_MECHANISM"])
        self.assertEqual(self._perform().failure_count, 5)


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
        threshold = LifeDataService.r_squared_review_threshold(result.failure_count)
        self.assertEqual(result.probability_plot_r_squared_threshold, threshold)
        self.assertIn(f"{threshold:.3f}, the R² that 90% of genuine Weibull samples with {result.failure_count} failures reach", row["recommendation"])
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

    def test_the_review_threshold_rises_with_the_failure_count_and_can_be_regenerated(self):
        table = [value for _, value in R_SQUARED_REVIEW_THRESHOLDS]
        self.assertEqual(table, sorted(table))
        self.assertAlmostEqual(LifeDataService.r_squared_review_threshold(5), 0.806)
        self.assertAlmostEqual(LifeDataService.r_squared_review_threshold(11), (0.846 + 0.861) / 2)
        self.assertAlmostEqual(LifeDataService.r_squared_review_threshold(500), 0.976)
        # The table is what the documented simulation gives; a smaller run of it lands close.
        for failures, value in ((5, 0.806), (20, 0.893)):
            self.assertAlmostEqual(LifeDataService.simulated_r_squared_threshold(failures, samples=4000, seed=7), value, delta=0.01)

    def test_a_fit_below_the_threshold_is_flagged_for_review(self):
        self._add_all(MONTHLY)
        self._perform()
        self.assertFalse(self._saved().probability_plot_review)
        with sqlite3.connect(self.service.db_path) as conn:
            conn.execute("UPDATE weibull_result SET probability_plot_r_squared = 0.5")

        saved = self._saved()
        [ranked] = self.service.latest_failure_mechanism_beta_rankings("A-1")

        self.assertTrue(saved.probability_plot_review)
        self.assertTrue(ranked["probability_plot_review"])
        row = LifeDataService._r_squared_interpretation_row(0.5, saved.failure_count)
        self.assertTrue(row["recommendation"].startswith(f"Below {saved.probability_plot_r_squared_threshold:.3f}"))
        self.assertIn("Review the population before acting on beta", row["recommendation"])

    def test_the_report_states_r_squared(self):
        self._add_all(MONTHLY)
        result = self._perform()
        path = self.tmp / "report.docx"
        self.service.build_weibull_report_docx("A-1", {"result_id": result.result_id}, path)
        root = ET.fromstring(zipfile.ZipFile(path).read("word/document.xml"))
        cells = ["".join(t.text or "" for t in tc.iter(f"{WORD}t")) for tc in root.iter(f"{WORD}tc")]

        index = cells.index("Probability plot R²")
        self.assertTrue(cells[index + 1].startswith(f"{result.probability_plot_r_squared:.3f}: "), cells[index + 1])
        self.assertIn(
            f"Meets {result.probability_plot_r_squared_threshold:.3f}, the review threshold for {result.failure_count} failures.",
            cells[index + 1],
        )


class PmResetScopeTests(_Seeded):
    """A PM reset restarts only what it restores (REL-WBL-DAT-004 §7)."""

    def setUp(self) -> None:
        super().setUp()
        with self.service.write_connection() as conn:
            self.other_mechanism_id = int(
                conn.execute(
                    "INSERT INTO failure_mechanism (failure_mechanism_name, failure_mode_id) VALUES ('Valve sticking', ?)",
                    (self.mode_id,),
                ).lastrowid
            )

    def _add_pm(self, task_id: str, completed: str, *, mechanism_id: int | None) -> None:
        """A PM reset aimed at the mode and, unless ``mechanism_id`` is None, one mechanism under it."""

        mapped_id = self._add(task_id, completed, pm=True)
        with self.service.write_connection() as conn:
            conn.execute(
                "UPDATE event_disposition SET reset_target_failure_mechanism_id = ? WHERE mapped_record_id = ?",
                (mechanism_id, mapped_id),
            )

    def _timeline_tasks(self, result) -> set[str]:
        return {event["task_id"] for event in result.events if event["weibull_sequence_number"] is not None}

    def test_a_mode_wide_pm_restarts_every_mechanism_under_the_mode(self):
        self._add_all(MONTHLY)
        self._add_pm("PM-MODE", _utc(2025, 4, 10, 9), mechanism_id=None)

        mechanism = self._perform()
        mode = self._perform(mode_level=True)

        self.assertIn("PM-MODE", self._timeline_tasks(mechanism))
        self.assertEqual(mechanism.pm_reset_censored_count, 1)
        self.assertIn("PM-MODE", self._timeline_tasks(mode))

    def test_a_pm_aimed_at_one_mechanism_restarts_that_mechanism_alone(self):
        self._add_all(MONTHLY)
        self._add_pm("PM-SEAL", _utc(2025, 4, 10, 9), mechanism_id=self.mechanism_id)
        self._add_pm("PM-VALVE", _utc(2025, 5, 12, 9), mechanism_id=self.other_mechanism_id)

        mechanism = self._perform()
        mode = self._perform(mode_level=True)

        self.assertIn("PM-SEAL", self._timeline_tasks(mechanism))
        self.assertNotIn("PM-VALVE", self._timeline_tasks(mechanism))
        # The mode's other mechanisms keep ageing through a PM aimed at one of them.
        self.assertFalse({"PM-SEAL", "PM-VALVE"} & self._timeline_tasks(mode))
        self.assertEqual(mode.pm_reset_censored_count, 0)

    def test_the_group_picker_counts_resets_by_the_same_rule(self):
        self._add_all(MONTHLY)
        self._add_pm("PM-MODE", _utc(2025, 4, 10, 9), mechanism_id=None)
        self._add_pm("PM-SEAL", _utc(2025, 4, 20, 9), mechanism_id=self.mechanism_id)

        options = {(o["grouping_level"], o["failure_mechanism_id"]): o for o in self.service.weibull_group_options("A-1")}

        self.assertEqual(options[("FAILURE_MODE", None)]["reset_count"], 1)
        self.assertEqual(options[("FAILURE_MECHANISM", self.mechanism_id)]["reset_count"], 2)

    def test_a_mode_wide_pm_reset_is_a_finished_disposition(self):
        self._add_pm("PM-MODE", _utc(2025, 4, 10, 9), mechanism_id=None)

        self.assertEqual(self.service.disposition_row_count("A-1", "pm", only_needing_disposition=True), 0)


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

    def test_life_hours_follow_the_register(self):
        self._add_all(MONTHLY)
        self.assertEqual(self._perform().life_basis["schedule_code"], "20H_MON_FRI")

        self.service.set_asset_weibull_schedule("A-1", "CONTINUOUS", reason="Runs through weekends.", changed_by="pat")
        continuous = self._perform()

        self.assertEqual(continuous.life_basis["schedule_code"], "CONTINUOUS")
        for obs in continuous.observations:
            # Every clock hour counts: nothing is taken out.
            self.assertAlmostEqual(obs["life_hours_for_weibull"], obs["life_hours_raw_elapsed"], places=6)
            self.assertAlmostEqual(obs["excluded_weekend_hours"], 0.0, places=6)

    def test_a_change_of_plant_time_zone_flags_saved_results_until_run_again(self):
        # The zone splits the days, so changing it, or installing the zone database a
        # run fell back to UTC without, moves every life's hours.
        self._add_all(MONTHLY)
        self.assertTrue(self._perform().time_zone_current)
        with self.service.write_connection() as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS availability_settings (id INTEGER PRIMARY KEY, timezone TEXT)")
            conn.execute("INSERT OR REPLACE INTO availability_settings (id, timezone) VALUES (1, 'America/New_York')")

        saved = self._saved()
        self.assertFalse(saved.time_zone_current)
        self.assertEqual(saved.current_time_zone, "America/New_York")
        self.assertTrue(saved.schedule_current)
        self.assertFalse(self.service.latest_failure_mechanism_beta_rankings("A-1")[0]["time_zone_current"])
        with self.assertRaisesRegex(ValueError, "split at midnight America/Chicago, but the plant's time zone is now America/New_York"):
            self.service.build_weibull_report_docx("A-1", {"result_id": saved.result_id}, self.tmp / "report.docx")
        again = self._perform()
        self.assertTrue(again.time_zone_current)
        self.assertEqual(again.life_basis["time_zone"], "America/New_York")

    def test_a_change_is_recorded_and_flags_saved_results_until_run_again(self):
        self._add_all(MONTHLY)
        self._perform()

        register = self.service.set_asset_weibull_schedule("A-1", "24H_MON_FRI", reason="Ops confirmed 3 shifts.", changed_by="pat")

        latest = register["history"][0]
        self.assertEqual(
            (latest["asset_number"], latest["from_code"], latest["to_code"], latest["reason"], latest["changed_by"]),
            ("A-1", "20H_MON_FRI", "24H_MON_FRI", "Ops confirmed 3 shifts.", "pat"),
        )
        saved = self._saved()
        self.assertFalse(saved.schedule_current)
        self.assertEqual(saved.current_schedule_name, "24 hours Monday-Friday")
        self.assertFalse(self.service.latest_failure_mechanism_beta_rankings("A-1")[0]["schedule_current"])
        with self.assertRaisesRegex(ValueError, "now on 24 hours Monday-Friday"):
            self.service.build_weibull_report_docx("A-1", {"result_id": saved.result_id}, self.tmp / "report.docx")
        self.assertTrue(self._perform().schedule_current)
        # Back to the plant default takes the asset off the register.
        self.service.set_asset_weibull_schedule("A-1", "20H_MON_FRI", reason="Back to two shifts.", changed_by="pat")
        self.assertNotIn("A-1", {row["asset_number"] for row in self.service.weibull_schedule_register()["assignments"]})

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


class RiskRankingTests(_Seeded):
    """REL-WBL-MTH-001 §8.1: the chance each mechanism fails in the next few weeks."""

    def test_the_chance_is_conditional_on_the_current_life(self):
        self._add_all(MONTHLY)
        result = self._perform(analysis_cutoff=date(2025, 7, 31))

        [four] = self.service.latest_failure_mechanism_risk_rankings("A-1")
        [eight] = self.service.latest_failure_mechanism_risk_rankings("A-1", weeks=8)

        [current] = [obs for obs in result.observations if obs["observation_type"] == "RIGHT_CENSORED_LIFE"]
        t = current["life_hours_for_weibull"]
        beta, eta = result.beta_mle, result.eta_mle
        for row, weeks in ((four, 4), (eight, 8)):
            window = weeks * 100.0  # 20 hours a weekday, five weekdays a week
            self.assertAlmostEqual(row["window_hours"], window)
            self.assertAlmostEqual(row["current_life_hours"], t)
            expected = 1 - math.exp(-((t + window) / eta) ** beta) / math.exp(-((t / eta) ** beta))
            self.assertAlmostEqual(row["probability"], expected, places=12)
        self.assertGreater(eight["probability"], four["probability"])

    def test_mechanisms_are_ordered_by_the_chance(self):
        fits = [
            {"failure_mechanism_name": "Slow", "beta_mle": 3.0, "eta_mle": 5000.0, "current_life_hours": 100.0,
             "hours_per_day": 20.0, "exclude_weekends": True, "failure_count": 9},
            {"failure_mechanism_name": "Due", "beta_mle": 3.0, "eta_mle": 900.0, "current_life_hours": 800.0,
             "hours_per_day": 20.0, "exclude_weekends": True, "failure_count": 6},
            {"failure_mechanism_name": "Early-life survivor", "beta_mle": 0.6, "eta_mle": 300.0, "current_life_hours": 4000.0,
             "hours_per_day": 24.0, "exclude_weekends": False, "failure_count": 7},
        ]
        self.service._latest_mechanism_fits = lambda asset_number: [dict(fit) for fit in fits]

        ranked = self.service.latest_failure_mechanism_risk_rankings("A-1")

        self.assertEqual([row["failure_mechanism_name"] for row in ranked], ["Due", "Early-life survivor", "Slow"])
        self.assertAlmostEqual(ranked[1]["window_hours"], 4 * 168.0)  # continuous: every clock hour

    def test_the_window_has_to_be_1_to_52_weeks(self):
        for weeks in (0, 53, float("nan")):
            with self.assertRaisesRegex(ValueError, "between 1 and 52 weeks"):
                self.service.latest_failure_mechanism_risk_rankings("A-1", weeks=weeks)


class IntervalRuleTests(unittest.TestCase):
    """REL-WBL-MTH-001 §8: an interval counts as stable by its width relative to its estimate."""

    def setUp(self) -> None:
        self.service = LifeDataService.__new__(LifeDataService)

    def test_a_beta_interval_is_judged_against_beta_itself(self):
        # 40 failures at beta 3: about 2.36 to 3.82, 49% of beta. Stable, though 1.46 wide.
        self.assertIn("no wider than 70% of beta", self.service._beta_ci_recommendation(2.36, 3.82, 3.0))
        # The pilot's SQ87 interval, 0.488 to 0.732, is 42% of beta 0.574.
        self.assertIn("no wider than 70% of beta", self.service._beta_ci_recommendation(0.488, 0.732, 0.574))
        # The Standards worked example: 1.350 to 5.558 is 154% of beta 2.739.
        self.assertIn("wider than 70% of beta", self.service._beta_ci_recommendation(1.350, 5.558, 2.739))
        self.assertNotIn("no wider", self.service._beta_ci_recommendation(1.350, 5.558, 2.739))
        # Crossing 1 is read first, whatever the width.
        self.assertIn("crossing 1.0", self.service._beta_ci_recommendation(0.95, 1.05, 1.0))

    def test_an_eta_interval_is_stable_up_to_40_percent_of_eta(self):
        self.assertIn("reasonably tight", self.service._eta_ci_recommendation(800.0, 1200.0, 1000.0))
        self.assertIn("directional guidance only", self.service._eta_ci_recommendation(700.0, 1200.0, 1000.0))


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

    # The summary carries both rankings; the risk list takes its own window.
    summary = client.get("/life-data-analysis/api/summary?asset=A-1&weeks=8").get_json()
    assert summary["rankings"] and summary["risk_rankings"][0]["window_weeks"] == 8
    risk = client.get("/life-data-analysis/api/risk-rankings?asset=A-1&weeks=6").get_json()
    assert risk["weeks"] == 6 and risk["rankings"][0]["window_weeks"] == 6
    assert client.get("/life-data-analysis/api/risk-rankings?asset=A-1&weeks=0").status_code == 400


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
