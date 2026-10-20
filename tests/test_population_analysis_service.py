"""Perform an Analysis's views, read from the disposition standard.

Built on the same 4002 PFS records as the standard's own tests: downtime drivers,
PM effectiveness and repeat fixes over a population, the rankings of saved fits,
an adjustment kept beside a fit, the numbered report, and the Disposition page's
workbook round trip.
"""

import tempfile
import zipfile
from datetime import date
from pathlib import Path

from tests.test_standard_disposition_service import PFS, S09, S11, StandardDispositionFixture, coded


class PopulationViewTests(StandardDispositionFixture):
    def setUp(self) -> None:
        super().setUp()
        self.load_acceptance()

    def test_downtime_reads_the_population_and_bins_each_stop(self):
        downtime = self.analysis.downtime({"assets": [PFS], "categories": ["FAILURE"]})
        self.assertEqual(downtime["summary"]["work_order_count"], 7)
        self.assertEqual(downtime["summary"]["total_downtime_hours"], 3.0 + 2.0 + 6.0 + 1.5 + 2.5 + 1.0 + 2.0)
        self.assertEqual(downtime["summary"]["max_downtime_hours"], 6.0)
        bins = {row["label"]: row["count"] for row in downtime["distribution"]}
        self.assertEqual(bins["1–4 hr"], 6)
        self.assertEqual(bins["4–8 hr"], 1)
        self.assertEqual(downtime["by_location"][0], {"label": "Asset 4002-S09", "downtime_hours": 15.0})
        self.assertEqual(downtime["top_events"][0]["mapped_record_id"], 220477)
        self.assertEqual(downtime["months"][0], "2024-02")

    def test_downtime_follows_a_drill_into_one_location(self):
        downtime = self.analysis.downtime({"assets": [PFS], "locations": [S11], "categories": ["FAILURE"]})
        self.assertEqual(downtime["summary"]["work_order_count"], 1)
        self.assertEqual(downtime["top_events"][0]["mapped_record_id"], 126581)

    def test_pm_effectiveness_pairs_each_pm_with_the_next_failure_where_it_was_done(self):
        pm = self.analysis.pm_effectiveness({"assets": [PFS], "locations": [S09], "components": ["Filter"]})
        # The filter PM on Jan 2 is the only PM that renewed a filter at S09; the
        # next filter failure there was found on Feb 1.
        self.assertEqual(pm["pms_performed"], 1)
        [row] = pm["rows"]
        self.assertEqual((row["pm_task_id"], row["corrective_wo_number"]), ("155579", "175552"))
        self.assertEqual(row["days_to_failure"], 30.0)
        self.assertEqual(pm["effectiveness"], "Fair")

    def test_a_pm_on_another_item_is_not_a_pm_on_this_one(self):
        pm = self.analysis.pm_effectiveness({"assets": [PFS], "locations": [S09], "components": ["Belt"]})
        self.assertEqual(pm["pms_performed"], 1)
        self.assertEqual(pm["rows"], [])
        self.assertEqual(pm["effectiveness"], "Insufficient Data")

    def test_repeat_fixes_are_counted_per_item_on_its_schedule(self):
        self.add_record(800001, PFS, "Filter clogged again", "2024-07-02", "2024-07-02", 1.0)
        self.std.save_dispositions([{"mapped_record_id": 800001, **coded()}])
        repeats = self.analysis.repeat_fixes({"assets": [PFS], "locations": [S09], "components": ["Filter"]}, window_hours=24)
        [group] = repeats["groups"]
        self.assertEqual((group["location"], group["component"]), (S09, "Filter"))
        self.assertEqual(group["failures"], 6)
        self.assertEqual(group["repeats"], 1)
        [pair] = repeats["pairs"]
        self.assertEqual((pair["prior_task_id"], pair["repeat_task_id"]), ("241959", "800001"))
        with self.assertRaisesRegex(ValueError, "between 0 and"):
            self.analysis.repeat_fixes({"assets": [PFS]}, window_hours=-1)


class SavedFitTests(StandardDispositionFixture):
    def setUp(self) -> None:
        super().setUp()
        extra = [(155579, PFS, "S09 Monthly PM - change dry-off oven filters", "2024-01-02", "2024-01-02", 0.0,
                  coded("PM_RESET", mode="DEG", renewed_items=["Filter"]))]
        day = 10
        for index in range(8):
            extra.append((500000 + index, PFS, f"Filter fault {index}", f"2024-{2 + index:02d}-{day:02d}",
                          f"2024-{2 + index:02d}-{day:02d}", 1.0, coded()))
            day = 5 + (index * 7) % 20
        self.load_acceptance_rows(extra)
        self.selection = {"assets": [PFS], "locations": [S09], "components": ["Filter"], "failure_mechanisms": ["CONT"]}

    def load_acceptance_rows(self, rows):
        for record_id, asset, title, created, completed, downtime, _ in rows:
            self.add_record(record_id, asset, title, created, completed, downtime, "PM" if "PM" in title else "CORRECTIVE_WO")
        self.std.save_dispositions([{"mapped_record_id": row[0], **row[6]} for row in rows])

    def run_and_save(self, **selection):
        return self.analysis.run_weibull(
            {**self.selection, **selection}, analysis_cutoff=date(2024, 12, 31), save=True, run_by="tim"
        )

    def test_a_saved_fit_is_the_latest_for_its_population_whatever_the_categories(self):
        first = self.run_and_save()
        second = self.run_and_save()
        latest = self.analysis.latest_saved({**self.selection, "categories": ["FAILURE", "HELD"]})
        self.assertEqual(latest["analysis_id"], second["analysis_id"])
        self.assertNotEqual(first["analysis_id"], second["analysis_id"])
        self.assertIsNone(self.analysis.latest_saved({"assets": [PFS], "components": ["Belt"]}))

    def test_the_result_reads_as_the_weibull_view_reads_it(self):
        result = self.run_and_save()
        self.assertEqual(result["failure_count"], 8)
        self.assertEqual(result["total_observation_count"], len(result["observations"]))
        self.assertEqual(result["current_life_censored_count"], 1)
        self.assertEqual(result["analysis_label"], "Asset 4002-S09 · Filter · CONT")
        self.assertEqual(result["life_basis"]["schedule_name"], "20 hours Monday-Friday")
        self.assertTrue(result["interpretation_summary"])
        self.assertEqual(result["result_id"], result["analysis_id"])

    def test_rankings_take_the_latest_single_component_fit_on_the_asset(self):
        self.run_and_save()
        self.run_and_save(components=["Filter", "Burner"])
        summary = self.analysis.summary({"assets": [PFS]})
        [ranked] = summary["rankings"]
        self.assertEqual(ranked["selection"]["components"], ["Filter"])
        [risk] = summary["risk_rankings"]
        self.assertEqual(risk["items_in_service"], 1)
        self.assertTrue(0 < risk["probability"] < 1)
        self.assertEqual(self.analysis.summary({"assets": [S11]})["rankings"], [])

    def test_an_adjustment_is_kept_beside_the_fit(self):
        result = self.run_and_save()
        with self.assertRaisesRegex(ValueError, "Say why"):
            self.analysis.save_adjustment(result["analysis_id"], 2.0, 900.0, "")
        self.analysis.save_adjustment(result["analysis_id"], 2.0, 900.0, "Two early lives look like duplicates", adjusted_by="tim")
        saved = self.analysis.load_saved(result["analysis_id"])
        self.assertEqual(saved["adjustment"]["adjusted_beta"], 2.0)
        self.assertEqual(saved["beta_mle"], result["beta_mle"])
        with self.assertRaises(LookupError):
            self.analysis.save_adjustment(999, 2.0, 900.0, "why")

    def test_a_report_states_the_population_and_is_numbered(self):
        result = self.run_and_save()
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "report.docx"
            number = self.analysis.build_report(result["analysis_id"], {"target_age_hours": 500}, path)
            self.assertEqual(number, "REL-WBL-RPT-4002-001")
            with zipfile.ZipFile(path) as docx:
                body = docx.read("word/document.xml").decode("utf-8")
        self.assertIn("POP: asset=4002; location=4002-S09; component=Filter; failure_mech=CONT", body)
        self.assertIn("Renewal streams pooled", body)
        self.assertIn("Reliability at 500 hours", body)
        with self.life.connect() as conn:
            logged = conn.execute("SELECT population_weibull_analysis_id FROM weibull_report_log").fetchone()[0]
        self.assertEqual(logged, result["analysis_id"])

    def test_a_report_is_refused_once_the_schedule_changed(self):
        result = self.run_and_save()
        self.life.set_asset_weibull_schedule(PFS, "CONTINUOUS", reason="Runs weekends", changed_by="tim")
        self.assertFalse(self.analysis.load_saved(result["analysis_id"])["schedule_current"])
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaisesRegex(ValueError, "Run it again"):
                self.analysis.build_report(result["analysis_id"], {}, Path(folder) / "report.docx")

    def test_the_tour_opens_a_saved_fit_first(self):
        self.assertEqual(self.analysis.tour_example()["saved_fit"], False)
        self.run_and_save()
        example = self.analysis.tour_example()
        self.assertTrue(example["saved_fit"])
        self.assertEqual(example["selection"]["components"], ["Filter"])


class RecordClassAndWorkbookTests(StandardDispositionFixture):
    def test_the_record_class_is_corrected_on_the_record(self):
        self.add_record(1, PFS, "Quarterly lube", "2024-01-01", "2024-01-01")
        self.std.save_dispositions([{"mapped_record_id": 1, **coded("NON_FAILURE", mode="NFF", action="INSPECT_ONLY",
                                                                     record_class="pm")}])
        record = self.std.disposition_record(1)
        self.assertEqual(record["record_class"], "PM")
        self.assertEqual(record["record_class_auto"], "CORRECTIVE_WO")
        self.assertEqual(record["derived"]["detection_method"], "PERIODIC_MAINTENANCE")
        with self.assertRaisesRegex(ValueError, "Record class must be one of"):
            self.std.save_dispositions([{"mapped_record_id": 1, **coded(record_class="NOPE")}])

    def test_the_workbook_round_trips_and_saves_only_what_changed(self):
        self.add_record(1, PFS, "Oven filter clogged", "2024-01-01", "2024-01-01")
        self.add_record(2, PFS, "Belt squeal", "2024-01-02", "2024-01-02")
        self.std.save_dispositions([{"mapped_record_id": 1, **coded()}], decided_by="tim")
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "dispositions.xlsx"
            self.assertEqual(self.std.export_excel(PFS, path), 2)
            rows = self.life._read_xlsx(path)
            headers = list(rows[0])
            self.assertIn("component_code", headers)
            with zipfile.ZipFile(path) as workbook:
                sheet = workbook.read("xl/worksheets/sheet1.xml").decode("utf-8")
                self.assertIn("'Lookup Lists'!", sheet)
            # Fill in row for record 2 and write the workbook back.
            filled = [list(row) + [None] * (len(headers) - len(row)) for row in rows]
            target = next(row for row in filled[1:] if int(row[0]) == 2)
            for column, value in coded(component="Belt", mechanism="WEAR", mode="VIB", action="REPLACE").items():
                target[headers.index(column)] = value
            self.life._write_xlsx(path, filled, "Dispositions")
            outcome = self.std.import_excel(PFS, path, decided_by="jose")
        self.assertEqual(outcome, {"saved": 1, "unchanged": 1})
        self.assertEqual(self.std.disposition_record(2)["disposition"]["component_code"], "Belt")
        self.assertEqual(self.std.disposition_record(1)["disposition"]["decided_by"], "tim")

    def test_one_bad_row_refuses_the_whole_upload(self):
        self.add_record(1, PFS, "One", "2024-01-01", "2024-01-01")
        self.add_record(2, PFS, "Two", "2024-01-02", "2024-01-02")
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "dispositions.xlsx"
            self.std.export_excel(PFS, path)
            rows = [list(row) for row in self.life._read_xlsx(path)]
            headers = rows[0]
            rows = [row + [None] * (len(headers) - len(row)) for row in rows]
            good = next(row for row in rows[1:] if int(row[0]) == 1)
            for column, value in coded().items():
                good[headers.index(column)] = value
            bad = next(row for row in rows[1:] if int(row[0]) == 2)
            bad[headers.index("disposition_category")] = "FAILURE"
            self.life._write_xlsx(path, rows, "Dispositions")
            with self.assertRaisesRegex(ValueError, r"^Row \d+, Task 2: FAILURE needs .* Nothing was saved\."):
                self.std.import_excel(PFS, path)
        self.assertIsNone(self.std.disposition_record(1)["disposition"]["source"])

    def test_a_workbook_row_from_another_asset_is_refused(self):
        self.add_record(1, PFS, "One", "2024-01-01", "2024-01-01")
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "dispositions.xlsx"
            self.std.export_excel(PFS, path)
            with self.assertRaisesRegex(ValueError, "not one of asset 4002-S09's work orders"):
                self.std.import_excel(S09, path)


if __name__ == "__main__":
    import unittest

    unittest.main()
