"""Storage and API tests for the Availability Dashboard.

The parity arithmetic is covered in ``test_availability_service``. These tests
cover the layers around it: that configuration seeds and persists, that work
orders are loaded without any classification filter, and that the HTTP API
reproduces the workbook end to end.
"""

import json
import os
import sqlite3
import tempfile
import unittest
from datetime import date
from pathlib import Path

from repositories.availability_repo import AvailabilityConfigError, AvailabilityRepository
from repositories.raw_repo import RawRepository
from services.availability_dashboard import build_config, build_dashboard, clamp_window_months
from services.life_data_service import LifeDataService

# Availability Data, January 2026, the Salvagnini group -- full stored precision.
JANUARY_DOWNTIME_HOURS = {
    "3101": 10.0,
    "3102": 12.5,
    "3103": 8.866666666666667,
    "3104": 0.0,
    "3105": 29.166666666666668,
    "3106": 3.0,
    "3107": 6.7,
}
JANUARY_AVAILABILITY = {
    "3101": 0.9747474747474747,
    "3102": 0.9067340067340067,
    "3103": 0.9649831649831649,
    "3104": 0.9873737373737373,
    "3105": 0.9099326599326599,
    "3106": 0.9797979797979798,
    "3107": 0.9298400673400673,
}
SALVAGNINI_JANUARY_AVERAGE = 0.95048701298701299


class AvailabilityTestCase(unittest.TestCase):
    """Builds a real GREMLIN.db with the mapped work-order table in place."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db_path = Path(self._tmp.name) / "GREMLIN.db"
        RawRepository(self.db_path).ensure_schema()
        LifeDataService(self.db_path, refresh_on_startup=False)
        self.repo = AvailabilityRepository(self.db_path)
        self.repo.ensure_schema()
        self._next_id = 1
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("INSERT INTO import_batch(import_batch_id, source_system) VALUES (1, 'Limble')")

    def add_work_order(self, asset_number, created, downtime_hours, **raw_fields):
        """Seed one work order through the real ingestion path.

        Rows go into ``raw_cmms_record`` as Limble-shaped JSON and are mapped by
        ``LifeDataService``, rather than being written straight into
        ``mapped_cmms_record``. That matters: the mapper re-derives mapped rows
        from ``raw_json`` whenever its version changes, so hand-written mapped
        rows are silently replaced. Going through the real path also means the
        classifier really runs, so ``is_pm_candidate`` and ``record_class_auto``
        are whatever production would actually compute.

        Limble reports downtime in seconds; ``downtime_hours`` is converted.
        """

        self._next_id += 1
        record_id = self._next_id
        payload = {
            "taskID": record_id,
            "Asset Number": asset_number,
            "createdDate_Final": created,
            "downtime": downtime_hours * 3600.0,
        }
        payload.update(raw_fields)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT INTO raw_cmms_record(raw_record_id, import_batch_id, source_record_id, raw_json)"
                " VALUES (?, 1, ?, ?)",
                (record_id, str(record_id), json.dumps(payload)),
            )
        LifeDataService(self.db_path, refresh_on_startup=False).refresh_mapped_cmms_records()

    def seed_salvagnini_january(self, **raw_fields):
        for asset, hours in JANUARY_DOWNTIME_HOURS.items():
            self.add_work_order(asset, "2026-01-15 15:00:00", hours, **raw_fields)

    def mapped_column(self, column, task_id=None):
        """Read a mapped column back, to assert what the real mapper produced."""

        with sqlite3.connect(self.db_path) as conn:
            sql = f"SELECT {column} FROM mapped_cmms_record"
            params = ()
            if task_id is not None:
                sql += " WHERE task_id = ?"
                params = (str(task_id),)
            return [row[0] for row in conn.execute(sql, params).fetchall()]


class SeedingTests(AvailabilityTestCase):
    def test_the_workbook_groups_are_seeded(self):
        groups = {g.asset_group: g for g in self.repo.load_groups()}
        self.assertEqual(len(groups), 10)
        self.assertEqual(groups["Salvagnini"].net_scheduled_hours_per_day, 18.0)
        self.assertEqual(groups["Building 12 Cloos Robots"].net_scheduled_hours_per_day, 16.0)
        self.assertEqual(
            groups["Salvagnini"].asset_numbers,
            ("3101", "3102", "3103", "3104", "3105", "3106", "3107"),
        )

    def test_dilo_and_enervac_is_configured_but_excluded(self):
        """The workbook carries it with Include? = N -- nine charts, not ten."""

        group = next(g for g in self.repo.load_groups() if g.asset_group == "Dilo & Enervac")
        self.assertFalse(group.include)
        self.assertEqual(group.asset_numbers, ())

    def test_pangborn_987_is_in_building_1(self):
        """The asset the workbook needed a hardcoded VBA patch to include."""

        group = next(g for g in self.repo.load_groups() if g.asset_group == "Building 1 Secondary Finishing")
        self.assertIn("987", group.asset_numbers)

    def test_display_names_and_linked_rules_are_seeded(self):
        names = self.repo.load_display_names()
        self.assertEqual(names["3101"], "MV")
        self.assertEqual(names["2958"], "ATC Sensor 2958")
        rules = self.repo.load_linked_rules()
        self.assertEqual(len(rules), 13)
        self.assertTrue(all(r.impact_factor == 0.5 for r in rules))

    def test_reseeding_does_not_overwrite_edits(self):
        self.repo.save_group_schedule(
            "Salvagnini",
            schedule_hours_per_day=20, break_hours_per_day=0,
            lunch_hours_per_day=0, setup_hours_per_day=0, include=True,
        )
        self.repo.ensure_schema()  # e.g. a later app start
        group = next(g for g in self.repo.load_groups() if g.asset_group == "Salvagnini")
        self.assertEqual(group.net_scheduled_hours_per_day, 20.0)


class NoClassificationFilterTests(AvailabilityTestCase):
    """Design doc §2.1 -- the loader must not filter by type or classification."""

    def test_a_return_to_service_note_does_not_hide_the_downtime(self):
        """The 623.7 h regression, reproduced against the real classifier.

        ``is_pm_candidate`` is set by a ``\\bpm\\b`` regex over the completion
        notes, and technicians close downtime work orders with return-to-service
        times like "RTS at 4:30 PM" -- so the clock time trips the PM rule. This
        asserts the classifier really does mislabel the row, and that
        availability counts it anyway.
        """

        self.add_work_order(
            "3101", "2026-01-15 15:00:00", 10.0,
            name="L3 down", completionNotes="Repair complete. RTS at 4:30 PM on 01/15/26.",
        )
        # The premise: production really does flag this as a PM candidate.
        self.assertEqual(self.mapped_column("is_pm_candidate"), [1])
        self.assertEqual(self.mapped_column("record_class_auto"), ["PM"])
        # The guarantee: availability counts it regardless.
        orders = self.repo.load_work_orders(["3101"])
        self.assertEqual(len(orders), 1)
        self.assertEqual(orders[0].downtime_hours, 10.0)

    def test_an_explicit_limble_pm_still_counts(self):
        self.add_work_order("3101", "2026-01-15 15:00:00", 4.0, type="1", name="MONTHLY PM INSPECTION")
        self.assertEqual(self.mapped_column("record_class_auto"), ["PM"])
        self.assertEqual(self.repo.load_work_orders(["3101"])[0].downtime_hours, 4.0)

    def test_every_limble_type_contributes(self):
        for type_raw in ["1", "2", "4", "6", "7"]:
            self.add_work_order("3101", "2026-01-15 15:00:00", 1.0, type=type_raw)
        orders = self.repo.load_work_orders(["3101"])
        self.assertEqual(len(orders), 5)
        self.assertEqual(sum(o.downtime_hours for o in orders), 5.0)

    def test_zero_downtime_work_orders_are_loaded_too(self):
        """They carry no downtime but they are the evidence an asset was watched."""

        self.add_work_order("3101", "2026-01-15 15:00:00", 0.0)
        self.assertEqual(len(self.repo.load_work_orders(["3101"])), 1)


class WorkOrderLoadingTests(AvailabilityTestCase):
    def test_downtime_falls_back_to_minutes(self):
        self.add_work_order("3101", "2026-01-15 15:00:00", 0.0)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("UPDATE mapped_cmms_record SET downtime_hours = NULL, downtime_minutes = 90")
        self.assertEqual(self.repo.load_work_orders(["3101"])[0].downtime_hours, 1.5)

    def test_created_dates_are_localized_before_bucketing(self):
        """A UTC timestamp just after midnight belongs to the previous local day.

        2026-02-01 03:00 UTC is 2026-01-31 21:00 in Chicago, so the work order
        is January's, not February's.
        """

        self.add_work_order("3101", "2026-02-01 03:00:00", 4.0)
        order = self.repo.load_work_orders(["3101"])[0]
        self.assertEqual((order.created_local.year, order.created_local.month), (2026, 1))
        self.assertEqual(order.created_local.day, 31)

    def test_an_unparseable_created_date_is_skipped_not_fatal(self):
        self.add_work_order("3101", "not a date", 4.0)
        self.assertEqual(self.repo.load_work_orders(["3101"]), [])

    def test_data_extent_reports_first_and_last_month(self):
        self.add_work_order("3101", "2026-01-15 15:00:00", 1.0)
        self.add_work_order("3101", "2026-04-15 15:00:00", 1.0)
        self.assertEqual(self.repo.data_extent(["3101"]), (date(2026, 1, 1), date(2026, 4, 1)))

    def test_a_missing_mapped_table_is_not_fatal(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("DROP TABLE mapped_cmms_record")
        self.assertEqual(self.repo.load_work_orders(), [])


class ConfigWriteTests(AvailabilityTestCase):
    def test_schedule_edits_persist_and_net_is_rederived(self):
        self.repo.save_group_schedule(
            "Salvagnini",
            schedule_hours_per_day=20, break_hours_per_day=1,
            lunch_hours_per_day=1, setup_hours_per_day=2, include=True,
        )
        group = next(g for g in self.repo.load_groups() if g.asset_group == "Salvagnini")
        self.assertEqual(group.net_scheduled_hours_per_day, 16.0)

    def test_a_schedule_whose_deductions_exceed_it_is_rejected(self):
        with self.assertRaises(AvailabilityConfigError):
            self.repo.save_group_schedule(
                "Salvagnini",
                schedule_hours_per_day=8, break_hours_per_day=4,
                lunch_hours_per_day=4, setup_hours_per_day=4, include=True,
            )

    def test_out_of_range_hours_are_rejected(self):
        for kwargs in (
            {"schedule_hours_per_day": 25},
            {"break_hours_per_day": -1},
        ):
            base = {
                "schedule_hours_per_day": 20, "break_hours_per_day": 0,
                "lunch_hours_per_day": 0, "setup_hours_per_day": 0, "include": True,
            }
            base.update(kwargs)
            with self.assertRaises(AvailabilityConfigError):
                self.repo.save_group_schedule("Salvagnini", **base)

    def test_membership_edits_replace_the_list_and_drop_duplicates(self):
        self.repo.save_group_schedule(
            "Building 6 Finishing",
            schedule_hours_per_day=20, break_hours_per_day=1,
            lunch_hours_per_day=1, setup_hours_per_day=2, include=True,
            asset_numbers=["4001", "4001", " 4002 ", "9999", ""],
        )
        group = next(g for g in self.repo.load_groups() if g.asset_group == "Building 6 Finishing")
        self.assertEqual(group.asset_numbers, ("4001", "4002", "9999"))

    def test_goals_and_overtime_are_keyed_by_month(self):
        self.repo.save_goal("Salvagnini", "2026-01-01", 0.97)
        self.repo.save_goal("Salvagnini", "2026-02-01", 0.99)
        self.repo.save_manual_ot("3101", date(2026, 1, 1), 40.0)
        self.assertEqual(self.repo.load_goals()[("Salvagnini", date(2026, 1, 1))], 0.97)
        self.assertEqual(self.repo.load_goals()[("Salvagnini", date(2026, 2, 1))], 0.99)
        self.assertEqual(self.repo.load_manual_ot()[("3101", date(2026, 1, 1))], 40.0)

    def test_a_mid_month_date_is_normalised_to_the_first(self):
        self.repo.save_goal("Salvagnini", "2026-01-23", 0.9)
        self.assertIn(("Salvagnini", date(2026, 1, 1)), self.repo.load_goals())

    def test_invalid_values_are_rejected(self):
        with self.assertRaises(AvailabilityConfigError):
            self.repo.save_goal("Salvagnini", "2026-01-01", 1.5)
        with self.assertRaises(AvailabilityConfigError):
            self.repo.save_manual_ot("3101", "2026-01-01", -5)
        with self.assertRaises(AvailabilityConfigError):
            self.repo.save_goal("Salvagnini", "nonsense", 0.9)
        with self.assertRaises(AvailabilityConfigError):
            self.repo.save_display_name("3101", "   ")

    def test_linked_rules_are_replaced_wholesale_and_validated(self):
        self.repo.replace_linked_rules([{"parent_asset_number": "3102", "linked_asset_number": "3101", "impact_factor": 0.25}])
        rules = self.repo.load_linked_rules()
        self.assertEqual(len(rules), 1)
        self.assertEqual(rules[0].impact_factor, 0.25)

        for bad in (
            [{"parent_asset_number": "3102", "linked_asset_number": "3102"}],
            [{"parent_asset_number": "3102", "linked_asset_number": "3101", "impact_factor": 2}],
            [{"parent_asset_number": "", "linked_asset_number": "3101"}],
        ):
            with self.assertRaises(AvailabilityConfigError):
                self.repo.replace_linked_rules(bad)
        # A rejected batch must not have wiped the stored rules.
        self.assertEqual(len(self.repo.load_linked_rules()), 1)

    def test_reset_restores_the_seeded_group(self):
        self.repo.save_group_schedule(
            "Salvagnini",
            schedule_hours_per_day=8, break_hours_per_day=0,
            lunch_hours_per_day=0, setup_hours_per_day=0, include=False,
            asset_numbers=["3101"],
        )
        self.repo.reset_group_to_defaults("Salvagnini")
        group = next(g for g in self.repo.load_groups() if g.asset_group == "Salvagnini")
        self.assertEqual(group.net_scheduled_hours_per_day, 18.0)
        self.assertTrue(group.include)
        self.assertEqual(len(group.asset_numbers), 7)


class DashboardTests(AvailabilityTestCase):
    def test_january_matches_the_workbook_end_to_end(self):
        self.seed_salvagnini_january()
        data = build_dashboard(self.repo, months=1, today=date(2026, 2, 15))
        salvagnini = next(g for g in data["groups"] if g["asset_group"] == "Salvagnini")
        self.assertEqual(data["months"], ["2026-01-01"])
        for asset in salvagnini["assets"]:
            with self.subTest(asset=asset["asset_number"]):
                self.assertAlmostEqual(
                    asset["values"][0], JANUARY_AVAILABILITY[asset["asset_number"]], places=12
                )
        self.assertAlmostEqual(salvagnini["average"][0], SALVAGNINI_JANUARY_AVERAGE, places=12)
        self.assertEqual(salvagnini["goal"][0], 0.95)

    def test_only_included_groups_are_charted(self):
        self.seed_salvagnini_january()
        data = build_dashboard(self.repo, months=1, today=date(2026, 2, 15))
        self.assertEqual(len(data["groups"]), 9)
        self.assertNotIn("Dilo & Enervac", [g["asset_group"] for g in data["groups"]])

    def test_the_payload_carries_the_asset_list_for_the_other_cards(self):
        self.seed_salvagnini_january()
        data = build_dashboard(self.repo, months=1, today=date(2026, 2, 15))
        self.assertEqual(len(data["all_asset_numbers"]), 40)
        self.assertIn("987", data["all_asset_numbers"])

    def test_the_payload_states_its_schedule_basis(self):
        self.seed_salvagnini_january()
        data = build_dashboard(self.repo, months=1, today=date(2026, 2, 15))
        salvagnini = next(g for g in data["groups"] if g["asset_group"] == "Salvagnini")
        self.assertEqual(salvagnini["net_scheduled_hours_per_day"], 18.0)
        self.assertTrue(data["generated_at"])
        self.assertEqual(data["timezone"], "America/Chicago")

    def test_an_empty_database_explains_itself(self):
        data = build_dashboard(self.repo, months=5, today=date(2026, 8, 6))
        self.assertEqual(data["groups"], [])
        self.assertEqual(data["months"], [])
        self.assertIn("No work orders", data["empty_reason"])

    def test_manual_overtime_reaches_the_dashboard(self):
        self.seed_salvagnini_january()
        self.repo.save_manual_ot("3101", "2026-01-01", 40.0)
        data = build_dashboard(self.repo, months=1, today=date(2026, 2, 15))
        salvagnini = next(g for g in data["groups"] if g["asset_group"] == "Salvagnini")
        row = next(r for r in salvagnini["rows"] if r["asset_number"] == "3101")
        self.assertEqual(row["adjusted_scheduled_hours"], 436.0)
        self.assertAlmostEqual(
            next(a for a in salvagnini["assets"] if a["asset_number"] == "3101")["values"][0],
            (436.0 - 10.0) / 436.0,
            places=12,
        )

    def test_window_length_is_clamped(self):
        self.assertEqual(clamp_window_months(0), 1)
        self.assertEqual(clamp_window_months(999), 36)
        self.assertEqual(clamp_window_months("junk"), 5)
        self.assertEqual(clamp_window_months(None), 5)
        self.assertEqual(clamp_window_months("12"), 12)

    def test_config_payload_exposes_every_editable_surface(self):
        config = build_config(self.repo)
        self.assertEqual(len(config["groups"]), 10)
        self.assertEqual(len(config["linked_rules"]), 13)
        self.assertIn("3101", config["display_names"])
        self.assertEqual(config["timezone"], "America/Chicago")


class ApiTests(AvailabilityTestCase):
    def setUp(self):
        super().setUp()
        os.environ["GREMLIN_DB_PATH"] = str(self.db_path)
        self.addCleanup(os.environ.pop, "GREMLIN_DB_PATH", None)
        import app as app_module

        # The module caches services per configured path; clear so each test
        # binds to its own temporary database.
        app_module._availability_repository = None
        app_module._life_data_service = None
        self.app_module = app_module
        self.client = app_module.app.test_client()

    def tearDown(self):
        self.app_module._availability_repository = None
        self.app_module._life_data_service = None

    def test_the_endpoint_reproduces_the_workbook(self):
        self.seed_salvagnini_january()
        response = self.client.get("/metrics/api/availability?months=1")
        self.assertEqual(response.status_code, 200)
        salvagnini = next(
            g for g in response.get_json()["groups"] if g["asset_group"] == "Salvagnini"
        )
        for asset in salvagnini["assets"]:
            self.assertAlmostEqual(
                asset["values"][0], JANUARY_AVAILABILITY[asset["asset_number"]], places=12
            )

    def test_goal_and_overtime_round_trip_through_the_api(self):
        self.seed_salvagnini_january()
        for url, payload in (
            ("/metrics/api/availability/goal",
             {"asset_group": "Salvagnini", "month": "2026-01-01", "goal_percent": 0.97}),
            ("/metrics/api/availability/ot",
             {"asset_number": "3101", "month": "2026-01-01", "hours": 40}),
        ):
            response = self.client.put(url, data=json.dumps(payload), content_type="application/json")
            self.assertEqual(response.status_code, 200, response.get_data(as_text=True))

        data = self.client.get("/metrics/api/availability?months=1").get_json()
        salvagnini = next(g for g in data["groups"] if g["asset_group"] == "Salvagnini")
        self.assertEqual(salvagnini["goal"][0], 0.97)
        row = next(r for r in salvagnini["rows"] if r["asset_number"] == "3101")
        self.assertEqual(row["manual_ot_hours"], 40.0)

    def test_a_bad_schedule_returns_400_with_a_readable_reason(self):
        response = self.client.put(
            "/metrics/api/availability/config/group/Salvagnini",
            data=json.dumps({
                "schedule_hours_per_day": 8, "break_hours_per_day": 4,
                "lunch_hours_per_day": 4, "setup_hours_per_day": 4,
            }),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("net hours would be negative", response.get_json()["error"])

    def test_a_missing_field_returns_400(self):
        response = self.client.put(
            "/metrics/api/availability/config/group/Salvagnini",
            data=json.dumps({"schedule_hours_per_day": 20}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("break_hours_per_day", response.get_json()["error"])

    def test_an_unknown_group_returns_400(self):
        response = self.client.put(
            "/metrics/api/availability/config/group/Nope",
            data=json.dumps({
                "schedule_hours_per_day": 20, "break_hours_per_day": 0,
                "lunch_hours_per_day": 0, "setup_hours_per_day": 0,
            }),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)

    def test_group_names_with_spaces_and_ampersands_route_correctly(self):
        response = self.client.post("/metrics/api/availability/config/group/Dilo %26 Enervac/reset")
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))

    def test_the_config_endpoint_serves_the_settings_editor(self):
        response = self.client.get("/metrics/api/availability/config")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.get_json()["groups"]), 10)

    def test_linked_rules_can_be_replaced_through_the_api(self):
        response = self.client.put(
            "/metrics/api/availability/linked-rules",
            data=json.dumps({"rules": [
                {"parent_asset_number": "3102", "linked_asset_number": "3101", "impact_factor": 0.25}
            ]}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.get_json()["linked_rules"]), 1)

    def test_the_metrics_page_still_renders(self):
        self.assertEqual(self.client.get("/metrics").status_code, 200)

    def test_the_settings_page_still_renders(self):
        self.assertEqual(self.client.get("/settings").status_code, 200)


if __name__ == "__main__":
    unittest.main()
