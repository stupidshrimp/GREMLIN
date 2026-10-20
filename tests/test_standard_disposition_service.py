"""The work-order disposition standard against a real GREMLIN.db.

The acceptance cases of spec section 8 are rebuilt here from the 4002 PFS records
they name (``4002_wo_dispositions_10.xlsx``, keyed by mapped_record_id): each is
dispositioned through the editor's save, as an engineer would, and the life engine
is held to what the spec expects of it for the population S09 + Filter + CONT.
"""

import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

from services import disposition_standard as std
from services.life_data_service import LifeDataService
from services.population_analysis_service import PopulationAnalysisService
from services.standard_disposition_service import StandardDispositionService

PFS = "4002"
S09, S11, S05 = "4002-S09", "4002-S11", "4002-S05"
ASSET_IDS = {PFS: "100", S09: "109", S11: "111", S05: "105"}


def coded(category="FAILURE", *, component="Filter", mechanism="CONT", mode="FTS", action="CLEAN_SERVICE",
          evidence="EXPLICIT", sub_asset=S09, **extra):
    return {
        "disposition_category": category,
        "sub_asset": sub_asset,
        "component_code": component,
        "failure_mechanism_code": mechanism,
        "failure_mode_code": mode,
        "action_code": action,
        "evidence": evidence,
        **extra,
    }


# (mapped_record_id, asset, task title, created, completed, downtime hours, disposition)
ACCEPTANCE = [
    (155579, PFS, "S09 Monthly PM - change dry-off oven filters", "2024-01-02", "2024-01-02", 0.0,
     coded("PM_RESET", mode="DEG", renewed_items=["Filter"])),
    (300001, PFS, "S09 burner tune-up - replaced flame rod", "2024-01-05", "2024-01-05", 1.0,
     coded("PM_RESET", component="Burner", mode="DEG", action="REPLACE", renewed_items=["Burner"])),
    (175552, PFS, "Dry-off oven will not light", "2024-02-01", "2024-02-01", 3.0,
     coded(action="CLEAN_SERVICE")),
    (168996, PFS, "Dry-off oven tripped out mid-shift", "2024-03-01", "2024-03-01", 2.0,
     coded(mode="UST")),
    (213728, PFS, "S09 PM - replace blower belt", "2024-03-20", "2024-03-20", 0.0,
     coded("PM_RESET", component="Belt", mechanism="WEAR", mode="DEG", action="REPLACE", renewed_items=["Belt"])),
    (92475, PFS, "Check dry-off oven filter", "2024-04-01", "2024-04-01", 0.5,
     coded("NON_FAILURE", mechanism="UNK", mode="NFF", action="INSPECT_ONLY")),
    (72445, PFS, "Dry-off oven down - main gas off", "2024-04-15", "2024-04-15", 4.0,
     coded("EXTERNAL", component="GasTrain", mechanism="UNK", action="RESET_ONLY")),
    (220477, PFS, "Filter sucked into burner", "2024-05-01", "2024-05-02", 6.0,
     coded(mechanism="DEFM", mode="STD", action="REPLACE")),
    (224611, PFS, "Dry-off oven low temp", "2024-06-01", "2024-06-01", 1.5,
     coded(mode="LOO", evidence="INFERRED")),
    (241959, PFS, "Dry-off oven won't fire - cleaned filter, replaced flame rod", "2024-07-01", "2024-07-01", 2.5,
     coded(renewed_items=["Burner"])),
    (79204, PFS, "Activator pump not pumping", "2024-02-10", "2024-02-10", 1.0,
     coded(component="Electrical", mechanism="ELEC", action="REPLACE", sub_asset=S05)),
    (126581, PFS, "Replace clogged risers", "2024-03-05", "2024-03-05", 2.0,
     coded(component="Nozzle", mechanism="PLUG", mode="LOO", action="REPLACE", sub_asset=S11, qty=5)),
    (47436, PFS, "Activator pump not running", "2024-05-20", "2024-05-20", 0.5,
     coded("EXTERNAL", component="Pump", mechanism="UNK", action="RESET_ONLY", sub_asset=S05)),
]


class StandardDispositionFixture(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db_path = Path(tmp.name) / "gremlin.db"
        self.life = LifeDataService(self.db_path, refresh_on_startup=False)
        self.std = StandardDispositionService(self.life)
        self.analysis = PopulationAnalysisService(self.life, self.std)
        with self.life.write_connection() as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS import_batch (import_batch_id INTEGER PRIMARY KEY, status TEXT)")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS raw_cmms_record ("
                "raw_record_id INTEGER PRIMARY KEY, import_batch_id INTEGER NOT NULL DEFAULT 1, raw_json TEXT NOT NULL)"
            )
            conn.execute("INSERT INTO import_batch (import_batch_id, status) VALUES (1, 'COMPLETED')")
        # Each sub-asset has a work order of its own, which is how the hierarchy
        # knows it is there.
        for number, record_id in ((S09, 900009), (S11, 900011), (S05, 900005)):
            self.add_record(record_id, number, f"{number} housekeeping", "2023-06-01", "2023-06-01")

    def add_record(self, record_id, asset, title, created, completed, downtime=0.0, record_class="CORRECTIVE_WO"):
        with self.life.write_connection() as conn:
            conn.execute(
                "INSERT INTO raw_cmms_record (raw_record_id, import_batch_id, raw_json) VALUES (?, 1, '{}')", (record_id,)
            )
            conn.execute(
                """
                INSERT INTO mapped_cmms_record (
                    mapped_record_id, raw_record_id, import_batch_id, asset_number, asset_name, asset_id_raw,
                    immediate_parent_asset_id, task_id, task_name, created_date_final, completed_date_final,
                    downtime_hours, record_class_auto, mapping_version
                ) VALUES (?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'v4')
                """,
                (
                    record_id, record_id, asset, f"Asset {asset}", ASSET_IDS[asset],
                    None if asset == PFS else ASSET_IDS[PFS], str(record_id), title, created, completed,
                    downtime, record_class,
                ),
            )

    def load_acceptance(self, extra=()):
        for record_id, asset, title, created, completed, downtime, _ in [*ACCEPTANCE, *extra]:
            record_class = "PM" if "PM" in title else "CORRECTIVE_WO"
            self.add_record(record_id, asset, title, created, completed, downtime, record_class)
        self.std.save_dispositions(
            [{"mapped_record_id": record[0], **record[6]} for record in [*ACCEPTANCE, *extra]], decided_by="tim"
        )

    def weibull(self, **selection):
        selection.setdefault("assets", [PFS])
        return self.analysis.run_weibull(selection, analysis_cutoff=date(2024, 12, 31))

    def roles(self, result, component="Filter"):
        return {
            event["record_id"]: event["role"]
            for event in result["events"]
            if event["component"] == component
        }


class AcceptanceTests(StandardDispositionFixture):
    """Spec section 8, against POP location=4002-S09; component=Filter; failure_mech=CONT."""

    def setUp(self) -> None:
        super().setUp()
        self.load_acceptance()
        self.result = self.weibull(locations=[S09], components=["Filter"], failure_mechanisms=["CONT"])
        self.filter_roles = self.roles(self.result)

    def test_failures_of_the_mechanism_count_whatever_their_mode_or_evidence(self):
        for record_id in (175552, 168996, 224611, 241959):
            self.assertEqual(self.filter_roles[record_id], std.ROLE_FAILURE, record_id)

    def test_the_filter_pm_starts_the_clock_and_the_belt_pm_never_touches_it(self):
        self.assertEqual(self.filter_roles[155579], std.ROLE_STARTS_LIFE)
        self.assertNotIn(213728, self.filter_roles)

    def test_a_competing_mechanism_is_a_suspension(self):
        self.assertEqual(self.filter_roles[220477], std.ROLE_SUSPENSION)

    def test_non_failures_and_externals_are_ignored(self):
        for record_id in (92475, 72445, 47436):
            self.assertNotIn(record_id, {event["record_id"] for event in self.result["events"]})

    def test_the_flame_rod_renewed_alongside_gets_a_suspension_in_its_own_stream(self):
        result = self.weibull(locations=[S09], components=["Filter", "Burner"], failure_mechanisms=["CONT"])
        self.assertEqual(self.roles(result, "Burner")[241959], std.ROLE_SUSPENSION)
        self.assertEqual(self.roles(result, "Filter")[241959], std.ROLE_FAILURE)
        self.assertIn("MIXED_COMPONENTS", {warning["code"] for warning in result["warnings"]})

    def test_the_population_definition_is_kept_with_the_run(self):
        self.assertEqual(
            self.result["population_definition"],
            "POP: asset=4002; location=4002-S09; component=Filter; failure_mech=CONT; "
            "suspensions=other renewals; window=2024-01-02..2024-12-31",
        )

    def test_the_fuse_is_coded_where_it_failed_not_where_the_request_pointed(self):
        record = self.std.disposition_record(79204)
        self.assertEqual(record["disposition"]["component_code"], "Electrical")
        self.assertEqual(record["derived"]["renewal_stream_key"], "4002-S05 | Electrical")
        pump = self.weibull(components=["Pump"])
        self.assertEqual(pump["streams"], [])

    def test_five_risers_at_once_warn_against_per_unit_weibull(self):
        result = self.weibull(components=["Nozzle"])
        self.assertIn("MULTI_UNIT", {warning["code"] for warning in result["warnings"]})

    def test_selecting_by_mode_warns_and_loses_the_trip(self):
        result = self.weibull(locations=[S09], components=["Filter"], failure_modes=["FTS"])
        self.assertIn("MODE_KEYED", {warning["code"] for warning in result["warnings"]})
        self.assertEqual(self.roles(result)[168996], std.ROLE_SUSPENSION)


class FittingTests(StandardDispositionFixture):
    def monthly_failures(self, count, *, start_id=500000, location=S09, mechanism="CONT"):
        extra = [(155579, PFS, "S09 Monthly PM - change dry-off oven filters", "2024-01-02", "2024-01-02", 0.0,
                  coded("PM_RESET", mode="DEG", renewed_items=["Filter"], sub_asset=location))]
        day = 10
        for index in range(count):
            month = 2 + index
            extra.append((
                start_id + index, PFS, f"Filter fault {index}", f"2024-{month:02d}-{day:02d}",
                f"2024-{month:02d}-{day:02d}", 1.0,
                coded(mechanism=mechanism, sub_asset=location),
            ))
            day = 5 + (index * 7) % 20
        for record_id, asset, title, created, completed, downtime, _ in extra:
            self.add_record(record_id, asset, title, created, completed, downtime)
        self.std.save_dispositions([{"mapped_record_id": record[0], **record[6]} for record in extra])

    def test_a_population_with_enough_failures_is_fitted_saved_and_read_back(self):
        self.monthly_failures(8)

        result = self.analysis.run_weibull(
            {"assets": [PFS], "locations": [S09], "components": ["Filter"], "failure_mechanisms": ["CONT"]},
            analysis_cutoff=date(2024, 12, 31),
            save=True,
            run_by="tim",
        )

        fit = result["fit"]
        self.assertTrue(fit["fitted"])
        self.assertEqual(fit["failure_count"], 8)
        self.assertGreater(fit["beta"], 0)
        self.assertIsNotNone(result["analysis_id"])
        saved = self.analysis.load_saved(result["analysis_id"])
        self.assertEqual(saved["population_definition"], result["population_definition"])
        self.assertEqual(saved["fit"]["beta"], fit["beta"])
        self.assertEqual(saved["beta_mle"], fit["beta"])
        self.assertTrue(saved["method_current"] and saved["schedule_current"] and saved["time_zone_current"])
        listed = self.analysis.saved_analyses(asset_number=PFS)
        self.assertEqual([row["analysis_id"] for row in listed], [result["analysis_id"]])
        self.assertEqual(self.analysis.saved_analyses(asset_number="3101"), [])

    def test_too_few_failures_lists_the_intervals_but_fits_nothing(self):
        self.monthly_failures(3)
        result = self.weibull(locations=[S09], components=["Filter"], failure_mechanisms=["CONT"])
        self.assertFalse(result["fit"]["fitted"])
        self.assertIn("at least 5 lives", result["fit"]["reason"])
        self.assertEqual(sum(row["failure_indicator"] for row in result["observations"]), 3)
        self.assertIn("SMALL_N", {warning["code"] for warning in result["warnings"]})

    def test_an_unknown_mechanism_is_fitted_both_ways(self):
        self.monthly_failures(6)
        self.add_record(600001, PFS, "Oven fault, cause not found", "2024-09-20", "2024-09-20")
        self.std.save_dispositions([{"mapped_record_id": 600001, **coded(mechanism="UNK")}])
        result = self.weibull(locations=[S09], components=["Filter"], failure_mechanisms=["CONT"])
        self.assertEqual(result["fit"]["failure_count"], 6)
        self.assertEqual(result["sensitivity_fit"]["failure_count"], 7)
        self.assertIn("UNKNOWN_SHARE", {warning["code"] for warning in result["warnings"]})

    def test_a_sub_asset_is_counted_on_its_parents_schedule(self):
        self.monthly_failures(5)
        self.life.set_asset_weibull_schedule(PFS, "CONTINUOUS", reason="Runs weekends", changed_by="tim")
        result = self.weibull(locations=[S09], components=["Filter"], failure_mechanisms=["CONT"])
        self.assertEqual(result["streams"][0]["schedule_name"], "Continuous, every clock hour")
        life = result["observations"][0]
        self.assertAlmostEqual(life["life_hours_for_weibull"], life["life_hours_raw_elapsed"], places=3)
        self.assertEqual(result["life_basis"]["schedule_name"], "Continuous, every clock hour")

    def test_a_repair_logged_later_moves_the_renewal_to_its_completion(self):
        # Bearings found bad on Feb 1, replaced on a WO finished Feb 11: the next
        # life starts on Feb 11.
        self.add_record(700001, PFS, "S09 bearings bad, need to schedule", "2024-02-01", "2024-02-01")
        self.add_record(700002, PFS, "Replace S09 bearings", "2024-02-09", "2024-02-11")
        self.add_record(700003, PFS, "S09 bearings noisy again", "2024-03-12", "2024-03-12")
        bearing = dict(component="Bearing", mechanism="WEAR", mode="VIB", action="REPLACE")
        self.std.save_dispositions([
            {"mapped_record_id": 700001, **coded(**bearing)},
            {"mapped_record_id": 700002, "disposition_category": "DUPLICATE_MISFILED", "linked_wo": "700001"},
            {"mapped_record_id": 700003, **coded(**bearing)},
        ])
        result = self.weibull(locations=[S09], components=["Bearing"])
        [life] = [row for row in result["observations"] if row["failure_indicator"]]
        self.assertEqual(life["start_datetime"][:10], "2024-02-11")
        self.assertEqual(round(life["life_hours_raw_elapsed"] / 24), 30)
        self.assertEqual(life["start_task_id"], "700001")
        self.assertEqual(self.std.disposition_record(700002)["disposition"]["linked_task_id"], "700001")


class DispositionEditorTests(StandardDispositionFixture):
    def test_sub_assets_come_from_the_asset_hierarchy(self):
        # The parent's asset number is tied to its asset id by a record of its own,
        # which every asset on the asset list has.
        self.add_record(1, PFS, "PFS line walk", "2024-01-01", "2024-01-01")
        options = self.std.editor_options(PFS)
        self.assertEqual([option["asset_number"] for option in options["sub_assets"]], [S05, S09, S11])
        self.assertEqual(self.std.editor_options(S09)["sub_assets"], [])

    def test_a_save_is_refused_whole_and_names_the_record(self):
        self.add_record(1, PFS, "One", "2024-01-01", "2024-01-01")
        self.add_record(2, PFS, "Two", "2024-01-02", "2024-01-02")
        with self.assertRaisesRegex(ValueError, r"^Task 2: FAILURE needs Component"):
            self.std.save_dispositions([
                {"mapped_record_id": 1, **coded()},
                {"mapped_record_id": 2, "disposition_category": "FAILURE"},
            ])
        self.assertIsNone(self.std.disposition_record(1)["disposition"]["source"])

    def test_saving_keeps_history_and_the_current_one_wins(self):
        self.add_record(1, PFS, "One", "2024-01-01", "2024-01-01")
        self.std.save_dispositions([{"mapped_record_id": 1, **coded()}], decided_by="tim")
        self.std.save_dispositions([{"mapped_record_id": 1, **coded(mode="UST")}], decided_by="jose")
        history = self.std.disposition_history(1)
        self.assertEqual([row["failure_mode_code"] for row in history], ["UST", "FTS"])
        self.assertEqual([row["is_current"] for row in history], [1, 0])
        self.assertEqual(self.std.disposition_record(1)["disposition"]["decided_by"], "jose")

    def test_the_page_filters_by_scope_and_search(self):
        self.add_record(1, PFS, "Oven filter clogged", "2024-01-01", "2024-01-01")
        self.add_record(2, PFS, "Belt squeal", "2024-01-02", "2024-01-02")
        self.std.save_dispositions([{"mapped_record_id": 1, **coded()}])
        page = self.std.disposition_page(PFS)
        self.assertEqual(page["counts"], {"all": 2, "not_reviewed": 1, "migrated": 0})
        self.assertEqual([row["mapped_record_id"] for row in page["rows"]], [2, 1])
        self.assertEqual([row["mapped_record_id"] for row in self.std.disposition_page(PFS, scope="not_reviewed")["rows"]], [2])
        self.assertEqual([row["mapped_record_id"] for row in self.std.disposition_page(PFS, search="belt")["rows"]], [2])

    def test_the_list_sorts_and_filters_every_row_not_just_the_page(self):
        self.add_record(1, PFS, "Oven filter clogged", "2024-01-01", "2024-01-01")
        self.add_record(2, PFS, "Belt squeal", "2024-01-02", "2024-01-02")
        self.add_record(3, PFS, "Activator pump", "2024-01-03", "2024-01-03")
        self.add_record(10, PFS, "Riser clogged", "2023-12-01", "2023-12-01")
        self.std.save_dispositions([
            {"mapped_record_id": 1, **coded(sub_asset=S09)},
            {"mapped_record_id": 3, **coded(component="Pump", mechanism="ELEC", action="REPLACE", sub_asset=None)},
        ])

        def ids(**view):
            return [row["mapped_record_id"] for row in self.std.disposition_page(PFS, page_size=2, **view)["rows"]]

        # Task IDs sort as numbers, across pages: 10 is last, not second.
        self.assertEqual(ids(sort="task_id"), [1, 2])
        self.assertEqual(ids(sort="task_id", direction="desc"), [10, 3])
        self.assertEqual(ids(sort="title"), [3, 2])
        # A blank sorts last whichever way.
        self.assertEqual(
            [row["mapped_record_id"] for row in self.std.disposition_page(PFS, sort="component", direction="desc")["rows"]][:2],
            [3, 1],
        )
        # A filter keeps the values named for it, "" standing for a blank cell.
        self.assertEqual(ids(filters={"category": ["NOT_REVIEWED"]}), [2, 10])
        self.assertEqual(ids(filters={"component": ["", "Pump"]}), [3, 2])
        self.assertEqual(ids(filters={"sub_asset": [S09]}), [1])
        self.assertEqual(ids(filters={"category": []}), [])
        # A column or a sort it does not know is ignored, not an error.
        page = self.std.disposition_page(PFS, sort="bogus", filters={"bogus": ["x"], "found": "2024"})
        self.assertEqual((page["sort"], page["filters"], page["displayed_count"]), (None, {}, 4))
        self.assertEqual(self.std.disposition_page(PFS, filters={"found": ["2024-01-02"]})["displayed_count"], 1)
        # The sub-asset reads by its name.
        self.assertEqual(self.std.disposition_page(PFS, filters={"sub_asset": [S09]})["rows"][0]["location_label"], f"Asset {S09}")

    def test_a_columns_filter_lists_its_values_under_the_other_filters(self):
        self.add_record(1, PFS, "Oven filter clogged", "2024-01-01", "2024-01-01")
        self.add_record(2, PFS, "Belt squeal", "2024-01-02", "2024-01-02")
        self.add_record(3, PFS, "Activator pump", "2024-01-03", "2024-01-03")
        self.std.save_dispositions([
            {"mapped_record_id": 1, **coded(sub_asset=S09)},
            {"mapped_record_id": 3, **coded(component="Pump", mechanism="ELEC", action="REPLACE", sub_asset=None)},
        ])
        values = self.std.list_column_values(PFS, "component")["values"]
        self.assertEqual([(item["value"], item["count"]) for item in values], [("Filter", 1), ("Pump", 1), ("", 1)])
        self.assertEqual(values[-1]["label"], "(Blanks)")
        # Its own filter does not hide its values; another column's does.
        same = self.std.list_column_values(PFS, "component", filters={"component": ["Pump"]})["values"]
        self.assertEqual(len(same), 3)
        other = self.std.list_column_values(PFS, "component", filters={"category": ["NOT_REVIEWED"]})["values"]
        self.assertEqual([item["value"] for item in other], [""])
        places = self.std.list_column_values(PFS, "sub_asset")["values"]
        self.assertEqual({item["value"]: item["label"] for item in places}, {PFS: f"Asset {PFS}", S09: f"Asset {S09}"})
        self.assertEqual([item["value"] for item in self.std.list_column_values(PFS, "found")["values"]],
                         ["2024-01-03", "2024-01-02", "2024-01-01"])
        self.assertEqual([item["value"] for item in self.std.list_column_values(PFS, "task_id")["values"]], ["1", "2", "3"])
        with self.assertRaises(ValueError):
            self.std.list_column_values(PFS, "bogus")

    def test_a_duplicate_link_must_name_a_real_work_order(self):
        self.add_record(1, PFS, "One", "2024-01-01", "2024-01-01")
        with self.assertRaisesRegex(ValueError, "No work order with task ID 999"):
            self.std.save_dispositions([
                {"mapped_record_id": 1, "disposition_category": "DUPLICATE_MISFILED", "linked_wo": "999"}
            ])


class CodeListTests(StandardDispositionFixture):
    def test_the_lists_are_seeded_once_and_then_belong_to_the_team(self):
        lists = self.std.code_lists()
        self.assertEqual(len(lists["COMPONENT"]["codes"]), len(std.SEED_CODES[std.COMPONENT]))
        self.std.save_code({"code_list": "COMPONENT", "code": "Filter", "name": "Filter element", "is_active": True})
        self.std.save_code({"code_list": "COMPONENT", "code": "Belt", "name": "Belt", "is_active": False})
        with self.life.write_connection() as conn:
            conn.execute("DELETE FROM disposition_code WHERE code_list = 'COMPONENT' AND code = 'Hose'")
        reopened = StandardDispositionService(LifeDataService(self.db_path, refresh_on_startup=False))
        codes = {code["code"]: code for code in reopened.code_lists()["COMPONENT"]["codes"]}
        self.assertEqual(codes["Filter"]["name"], "Filter element")
        self.assertFalse(codes["Belt"]["is_active"])
        self.assertNotIn("Hose", codes)

    def test_a_new_code_and_its_meaning(self):
        self.std.save_code({
            "code_list": "FAILURE_MECHANISM", "code": "CORR", "name": "Corrosion", "iso_ref": "2.2",
            "default_cause_code": "EXP-WEAR", "sort_order": 115,
        })
        self.std.save_code({"code_list": "ACTION", "code": "REBUILD", "name": "Rebuild", "renews_item": True})
        codebook = self.std.codebook()
        self.assertEqual(codebook.default_cause("CORR"), "EXP-WEAR")
        self.assertTrue(codebook.renews("REBUILD"))
        with self.assertRaisesRegex(ValueError, "can mean"):
            self.std.save_code({"code_list": "ACTION", "code": "X", "name": "X", "semantic": "UNKNOWN"})
        with self.assertRaisesRegex(ValueError, "not in the failure cause list"):
            self.std.save_code({"code_list": "FAILURE_MECHANISM", "code": "Y", "name": "Y", "default_cause_code": "NOPE"})
        with self.assertRaisesRegex(ValueError, "A code is"):
            self.std.save_code({"code_list": "COMPONENT", "code": "two words", "name": "Bad"})

    def test_a_retired_code_is_no_longer_offered_but_still_reads(self):
        self.add_record(1, PFS, "One", "2024-01-01", "2024-01-01")
        self.std.save_dispositions([{"mapped_record_id": 1, **coded()}])
        self.std.save_code({"code_list": "COMPONENT", "code": "Filter", "name": "Filter", "is_active": False})
        offered = {code["code"] for code in self.std.editor_options(PFS)["code_lists"]["COMPONENT"]}
        self.assertNotIn("Filter", offered)
        self.assertEqual(self.std.disposition_record(1)["disposition"]["component_code"], "Filter")
        # Re-saving the record untouched is fine; choosing the retired code anew is not.
        self.std.save_dispositions([{"mapped_record_id": 1, **coded(mode="UST")}])
        self.add_record(2, PFS, "Two", "2024-01-02", "2024-01-02")
        with self.assertRaisesRegex(ValueError, "retired"):
            self.std.save_dispositions([{"mapped_record_id": 2, **coded()}])


class MigrationTests(StandardDispositionFixture):
    def legacy(self, record_id, category, *, mode=None, mechanism=None, notes="", include=1):
        with self.life.write_connection() as conn:
            mode_id = mechanism_id = None
            if mode:
                mode_id = self.life._upsert_failure_mode_for_asset(conn, PFS, mode)
            if mechanism:
                mechanism_id = self.life._upsert_failure_mechanism_for_asset(conn, PFS, mechanism, mode_id)
            conn.execute("UPDATE event_disposition SET is_current = 0 WHERE mapped_record_id = ?", (record_id,))
            conn.execute(
                """
                INSERT INTO event_disposition(mapped_record_id, disposition_category, failure_mode_id,
                    failure_mechanism_id, disposition_notes, include_in_weibull_candidate)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (record_id, category, mode_id, mechanism_id, notes, include),
            )

    def reopen(self):
        return StandardDispositionService(LifeDataService(self.db_path, refresh_on_startup=False))

    def test_each_legacy_category_maps_and_keeps_its_text(self):
        for record_id, category in enumerate(std.LEGACY_CATEGORY_MAP, start=1):
            self.add_record(record_id, PFS, f"Legacy {category}", "2024-01-01", "2024-01-01")
            self.legacy(record_id, category, mode="PFS oven/heating/temperature failure", mechanism="Burner fault",
                        notes="old note")
        service = self.reopen()
        for record_id, category in enumerate(std.LEGACY_CATEGORY_MAP, start=1):
            disposition = service.disposition_record(record_id)["disposition"]
            self.assertEqual(disposition["disposition_category"], std.LEGACY_CATEGORY_MAP[category])
            self.assertEqual(disposition["legacy_disposition_category"], category)
            self.assertEqual(disposition["legacy_failure_mode"], "PFS oven/heating/temperature failure")
            self.assertEqual(disposition["legacy_failure_mechanism"], "Burner fault")
            self.assertEqual(disposition["disposition_note"], "old note")
            self.assertEqual(disposition["source"], "MIGRATION")
            self.assertIsNone(disposition["component_code"])
        flag = service.disposition_record(1)["disposition"]["review_flag"]
        self.assertIn("not no-fault-found or external", flag)

    def test_the_unambiguous_legacy_mechanism_is_converted(self):
        self.add_record(219, PFS, "Dry-off oven filter", "2024-01-01", "2024-01-01")
        self.legacy(219, "INCLUDED_FAILURE", mode="PFS oven/heating/temperature failure", mechanism="S09 clogged air filter")
        disposition = self.reopen().disposition_record(219)["disposition"]
        self.assertEqual(
            (disposition["sub_asset"], disposition["component_code"], disposition["failure_mechanism_code"]),
            (S09, "Filter", "CONT"),
        )
        self.assertIsNone(disposition["failure_mode_code"])
        self.assertIn("read off the legacy mechanism name", disposition["review_flag"])

    def test_a_migrated_row_follows_the_old_screen_until_an_engineer_saves(self):
        self.add_record(1, PFS, "One", "2024-01-01", "2024-01-01")
        self.legacy(1, "UNKNOWN")
        service = self.reopen()
        self.assertEqual(service.disposition_record(1)["disposition"]["disposition_category"], std.NOT_REVIEWED)
        # Saved on the old screen: the migrated row follows at once.
        self.life.save_disposition(1, kind="wo", disposition_category="HELD_AMBIGUOUS", disposition_text="unclear")
        self.assertEqual(service.disposition_record(1)["disposition"]["disposition_category"], std.HELD)
        # An engineer's standard disposition is not overwritten by the old screen,
        # and keeps the legacy text.
        service.save_dispositions([{"mapped_record_id": 1, **coded()}])
        self.life.save_disposition(1, kind="wo", disposition_category="EXCLUDED_NON_FAILURE")
        disposition = self.reopen().disposition_record(1)["disposition"]
        self.assertEqual(disposition["disposition_category"], std.FAILURE)
        self.assertEqual(disposition["source"], "ENGINEER")
        self.assertEqual(disposition["legacy_disposition_category"], "HELD_AMBIGUOUS")

    def test_migrated_failures_are_left_out_of_the_life_engine_and_counted(self):
        self.add_record(1, PFS, "One", "2024-01-01", "2024-01-01")
        self.legacy(1, "INCLUDED_FAILURE", mode="M", mechanism="Burner fault")
        result = PopulationAnalysisService(self.reopen().life).run_weibull({"assets": [PFS]}, analysis_cutoff=date(2024, 12, 31))
        self.assertIn("UNCODED_RECORDS", {warning["code"] for warning in result["warnings"]})
        self.assertEqual(result["observations"], [])


class PatternViewTests(StandardDispositionFixture):
    def setUp(self) -> None:
        super().setUp()
        self.load_acceptance()

    def summary(self, selection, **options):
        return self.analysis.summary({"assets": [PFS], **selection}, **options)

    def test_the_pareto_counts_and_sums_downtime_with_cause_underneath(self):
        summary = self.summary({"categories": ["FAILURE"]}, group_by="sub_asset")
        rows = {row["key"]: row for row in summary["pareto"]["rows"]}
        self.assertEqual(rows[S09]["label"], "Asset 4002-S09")
        self.assertEqual(rows[S09]["count"], 5)
        self.assertEqual(rows[S09]["downtime_hours"], 3.0 + 2.0 + 6.0 + 1.5 + 2.5)
        self.assertEqual({cause["key"]: cause["count"] for cause in rows[S09]["causes"]}, {"EXP-WEAR": 4, "UNK": 1})
        self.assertEqual(summary["pareto"]["next_group_by"], "component")
        self.assertEqual(summary["pareto"]["selection_key"], "locations")

    def test_the_pareto_offers_sub_assets_not_locations_or_assets(self):
        summary = self.summary({"categories": ["FAILURE"]})
        self.assertEqual(summary["pareto"]["group_by"], "sub_asset")
        options = self.analysis.options([PFS])
        self.assertNotIn("location", [option["value"] for option in options["group_by"]])
        self.assertNotIn("asset", [option["value"] for option in options["group_by"]])
        # A work order placed on no sub-asset is the asset itself, said as such.
        self.add_record(2, PFS, "PFS line stop", "2024-08-01", "2024-08-01")
        self.std.save_dispositions([{"mapped_record_id": 2, **coded(sub_asset=None)}])
        rows = {row["key"]: row["label"] for row in self.summary({"categories": ["FAILURE"]})["pareto"]["rows"]}
        self.assertEqual(rows[PFS], "Asset 4002, no sub-asset")

    def test_drilling_into_a_sub_asset_then_a_component(self):
        summary = self.summary(
            {"locations": [S09], "components": ["Filter"], "categories": ["FAILURE"]}, group_by="failure_mechanism"
        )
        self.assertEqual({row["key"]: row["count"] for row in summary["pareto"]["rows"]}, {"CONT": 4, "DEFM": 1})

    def test_the_default_categories_leave_out_unreviewed_and_duplicates(self):
        summary = self.summary({}, group_by="category")
        categories = {row["key"] for row in summary["pareto"]["rows"]}
        self.assertEqual(categories, {"FAILURE", "PM_RESET", "NON_FAILURE", "EXTERNAL"})

    def test_a_held_record_reads_as_not_coded(self):
        self.add_record(1, PFS, "Something", "2024-08-01", "2024-08-01")
        self.std.save_dispositions([{"mapped_record_id": 1, **coded("HELD", disposition_note="No notes on the WO")}])
        summary = self.summary({"categories": ["HELD"]}, group_by="component")
        self.assertEqual([row["label"] for row in summary["pareto"]["rows"]], ["Not coded"])

    def test_the_trend_is_continuous_and_reads_the_pareto_dimension(self):
        trend = self.summary({"categories": ["FAILURE"]}, group_by="component")["trend"]
        self.assertEqual(trend["months"], ["2024-02", "2024-03", "2024-04", "2024-05", "2024-06", "2024-07"])
        self.assertEqual(trend["monthly_counts"], [2, 2, 0, 1, 1, 1])
        groups = {group["key"]: group for group in trend["groups"]}
        self.assertEqual(groups["Filter"]["monthly_counts"], [1, 1, 0, 1, 1, 1])
        self.assertTrue(trend["has_growth_window"])
        self.assertEqual(trend["summary"]["most_frequent"]["key"], "Filter")
        self.assertEqual(trend["summary"]["highest_downtime"]["key"], "Filter")

    def test_the_matrix_crosses_location_and_component(self):
        matrix = self.summary({"categories": ["FAILURE"]})["matrix"]
        rows = [row["key"] for row in matrix["rows"]]
        columns = [column["key"] for column in matrix["columns"]]
        self.assertEqual(matrix["cells"][rows.index(S09)][columns.index("Filter")]["count"], 5)
        self.assertEqual(matrix["cells"][rows.index(S11)][columns.index("Nozzle")]["count"], 1)

    def test_the_date_window_reads_the_detection_date(self):
        summary = self.summary({"categories": ["FAILURE"]}, start=date(2024, 5, 1), end=date(2024, 6, 30))
        self.assertEqual(sorted(record["mapped_record_id"] for record in summary["trend"]["records"]), [220477, 224611])
        self.assertTrue(summary["population_definition"].endswith("window=2024-05-01..2024-06-30"))

    def test_readiness_counts_what_the_analyses_can_use(self):
        readiness = self.summary({})["readiness"]
        self.assertEqual(readiness["failures"], 7)
        self.assertEqual(readiness["pm_resets"], 3)
        self.assertEqual(readiness["not_reviewed"], 3)
        self.assertEqual(readiness["uncoded"], 0)

    def test_options_list_what_is_there_to_choose(self):
        options = self.analysis.options([PFS])
        locations = {option["value"] for option in options["dimensions"]["locations"]}
        self.assertEqual(locations, {PFS, S05, S09, S11})
        components = {option["value"] for option in options["dimensions"]["components"]}
        self.assertTrue({"Filter", "Burner", "Electrical", "Nozzle"} <= components)
        self.assertEqual([c["value"] for c in options["categories"] if c["default"]], ["FAILURE"])


if __name__ == "__main__":
    unittest.main()
