"""Storage and API tests for the Availability Dashboard.

The parity arithmetic is covered in ``test_availability_service``. These tests
cover the layers around it: that configuration seeds and persists, that work
orders are loaded without any classification filter, and that the HTTP API
reproduces the workbook end to end.
"""

import json
import os
import sqlite3
import sys
import tempfile
import unittest
import zoneinfo
from datetime import date, datetime
from datetime import timezone as dt_timezone
from pathlib import Path
from unittest import mock

import repositories.availability_repo
from repositories.availability_repo import AvailabilityConfigError, AvailabilityRepository
from repositories.raw_repo import RawRepository
from services.availability_dashboard import (
    build_config,
    build_dashboard,
    build_work_order_detail,
    clamp_window_months,
    parse_month,
)
from services.life_data_service import DatabaseWriteError, LifeDataService

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
        self.assertEqual(self.repo.load_work_order_details(["3101"], date(2026, 1, 1)), [])


class WorkOrderDetailLoadingTests(AvailabilityTestCase):
    """The wider load behind a single bar -- same rows, plus the text."""

    def test_the_descriptive_text_comes_through(self):
        self.add_work_order(
            "3101", "2026-01-15 15:00:00", 4.0,
            name="L3 down", status="Completed", type="6",
            description="Would not cycle.", completionNotes="Replaced valve.",
        )
        detail = self.repo.load_work_order_details(["3101"], date(2026, 1, 1))[0]
        self.assertEqual(detail.order.downtime_hours, 4.0)
        self.assertEqual(detail.task_name, "L3 down")
        self.assertEqual(detail.status, "Completed")
        self.assertEqual(detail.type_raw, "6")
        self.assertEqual(detail.description, "Would not cycle.")
        self.assertEqual(detail.completion_notes, "Replaced valve.")

    def test_the_description_falls_back_to_whichever_field_was_filled_in(self):
        """A work order raised as a request carries its text somewhere else."""

        self.add_work_order(
            "3101", "2026-01-15 15:00:00", 1.0, requestorDescription="Operator says it stalls"
        )
        detail = self.repo.load_work_order_details(["3101"], date(2026, 1, 1))[0]
        self.assertEqual(detail.description, "Operator says it stalls")

    def test_the_month_filter_uses_plant_time_not_the_stored_utc(self):
        """The same rule the chart buckets by, or a bar and its rows disagree.

        2026-02-01 03:00 UTC is 2026-01-31 21:00 in Chicago, so this work order
        belongs to January's bar and must appear under it.
        """

        self.add_work_order("3101", "2026-02-01 03:00:00", 4.0)
        self.assertEqual(len(self.repo.load_work_order_details(["3101"], date(2026, 1, 1))), 1)
        self.assertEqual(self.repo.load_work_order_details(["3101"], date(2026, 2, 1)), [])

    def test_an_omitted_month_loads_every_month(self):
        self.add_work_order("3101", "2026-01-15 15:00:00", 1.0)
        self.add_work_order("3101", "2026-04-15 15:00:00", 1.0)
        self.assertEqual(len(self.repo.load_work_order_details(["3101"])), 2)

    def test_the_drill_down_is_not_filtered_by_classification_either(self):
        """§2.1 again: the bar counts it, so the evidence must show it.

        A row counted into the total but missing from the list that explains
        the total is the same bug as excluding it, only harder to notice.
        """

        self.add_work_order(
            "3101", "2026-01-15 15:00:00", 10.0,
            name="L3 down", completionNotes="Repair complete. RTS at 4:30 PM on 01/15/26.",
        )
        self.assertEqual(self.mapped_column("is_pm_candidate"), [1])
        details = self.repo.load_work_order_details(["3101"], date(2026, 1, 1))
        self.assertEqual(len(details), 1)
        self.assertEqual(details[0].order.downtime_hours, 10.0)
        self.assertEqual(details[0].record_class, "PM")

    def test_a_database_missing_the_descriptive_columns_still_loads(self):
        """Same defence the hot path takes: a column-poor database degrades."""

        self.add_work_order("3101", "2026-01-15 15:00:00", 4.0, name="L3 down")
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("ALTER TABLE mapped_cmms_record DROP COLUMN completion_notes")
        detail = self.repo.load_work_order_details(["3101"], date(2026, 1, 1))[0]
        self.assertEqual(detail.order.downtime_hours, 4.0)
        self.assertEqual(detail.completion_notes, "")


# The schema the earlier Availability Dashboard attempt left in real databases.
# Kept verbatim rather than derived, so it stays a fixed description of what is
# actually out there even as the current schema moves on.
_LEGACY_SCHEMA = """
CREATE TABLE availability_settings (id INTEGER PRIMARY KEY CHECK (id=1), selected_year INTEGER NOT NULL,
    utc_offset_hours REAL NOT NULL DEFAULT 5, last_updated TEXT);
CREATE TABLE availability_asset_groups (id INTEGER PRIMARY KEY AUTOINCREMENT, asset_group TEXT NOT NULL UNIQUE,
    schedule_hours_per_day REAL NOT NULL, break_hours_per_day REAL NOT NULL DEFAULT 0,
    lunch_hours_per_day REAL NOT NULL DEFAULT 0, setup_hours_per_day REAL NOT NULL DEFAULT 0,
    net_scheduled_hours_per_day REAL NOT NULL, include_flag INTEGER NOT NULL DEFAULT 1, notes TEXT,
    sort_order INTEGER NOT NULL);
CREATE TABLE availability_asset_group_assets (id INTEGER PRIMARY KEY AUTOINCREMENT, asset_group_id INTEGER NOT NULL,
    asset_number TEXT NOT NULL, sort_order INTEGER NOT NULL, UNIQUE(asset_group_id, asset_number));
CREATE TABLE availability_asset_display_names (asset_number TEXT PRIMARY KEY, display_name TEXT NOT NULL);
CREATE TABLE availability_linked_downtime_rules (id INTEGER PRIMARY KEY AUTOINCREMENT, rule_group TEXT NOT NULL,
    parent_asset_number TEXT NOT NULL, linked_asset_number TEXT NOT NULL, impact_factor REAL NOT NULL DEFAULT 0.5);
CREATE TABLE availability_manual_ot (id INTEGER PRIMARY KEY AUTOINCREMENT, asset_group TEXT NOT NULL,
    asset_number TEXT NOT NULL, month_date TEXT NOT NULL, manual_ot_hours REAL NOT NULL DEFAULT 0,
    UNIQUE(asset_group, asset_number, month_date));
CREATE TABLE availability_goal_percent (id INTEGER PRIMARY KEY AUTOINCREMENT, asset_group TEXT NOT NULL,
    month_date TEXT NOT NULL, goal_percent REAL NOT NULL DEFAULT 0.95, UNIQUE(asset_group, month_date));
CREATE TABLE availability_results (id INTEGER PRIMARY KEY, availability_percent REAL);
"""


class LegacySchemaMigrationTests(unittest.TestCase):
    """A database from the earlier attempt must upgrade, not break.

    ``CREATE TABLE IF NOT EXISTS`` keeps a legacy table's *data* but leaves its
    *schema* alone, and this feature's schema changed: ``updated_at`` was added
    everywhere, ``availability_settings`` swapped ``selected_year`` and
    ``utc_offset_hours`` for a timezone name, and ``availability_manual_ot``
    dropped ``asset_group`` from its unique key. Without migration the first
    availability request fails with ``no column named timezone`` -- and that is
    the expected state of every existing installation, not an edge case.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db_path = Path(self._tmp.name) / "legacy.db"
        with sqlite3.connect(self.db_path) as conn:
            conn.executescript(_LEGACY_SCHEMA)
            conn.execute("INSERT INTO availability_settings(id, selected_year, utc_offset_hours) VALUES (1, 2026, 5)")
            conn.execute(
                "INSERT INTO availability_asset_groups(id, asset_group, schedule_hours_per_day,"
                " break_hours_per_day, lunch_hours_per_day, setup_hours_per_day,"
                " net_scheduled_hours_per_day, include_flag, notes, sort_order)"
                " VALUES (1, 'Salvagnini', 24, 1, 2, 3, 18, 1, 'legacy notes', 1)"
            )
            conn.execute(
                "INSERT INTO availability_asset_group_assets(asset_group_id, asset_number, sort_order)"
                " VALUES (1, '3101', 1)"
            )
            conn.execute(
                "INSERT INTO availability_asset_display_names(asset_number, display_name)"
                " VALUES ('3101', 'LEGACY-MV')"
            )
            conn.execute(
                "INSERT INTO availability_linked_downtime_rules"
                "(rule_group, parent_asset_number, linked_asset_number, impact_factor)"
                " VALUES ('SALV', '3102', '3101', 0.25)"
            )
            conn.execute(
                "INSERT INTO availability_goal_percent(asset_group, month_date, goal_percent)"
                " VALUES ('Salvagnini', '2026-01-01', 0.97)"
            )
            # One asset/month under two groups -- only representable once now.
            conn.execute(
                "INSERT INTO availability_manual_ot(asset_group, asset_number, month_date, manual_ot_hours)"
                " VALUES ('Old Group', '3101', '2026-01-01', 10)"
            )
            conn.execute(
                "INSERT INTO availability_manual_ot(asset_group, asset_number, month_date, manual_ot_hours)"
                " VALUES ('Salvagnini', '3101', '2026-01-01', 40)"
            )
        self.repo = AvailabilityRepository(self.db_path)

    def test_a_legacy_database_bootstraps(self):
        self.repo.ensure_schema()
        self.assertEqual(self.repo.load_timezone(), "America/Chicago")

    def test_migration_is_idempotent(self):
        self.repo.ensure_schema()
        self.repo.ensure_schema()
        self.assertEqual(self.repo.load_display_names()["3101"], "LEGACY-MV")

    def test_user_edits_survive_the_migration(self):
        self.repo.ensure_schema()
        self.assertEqual(self.repo.load_goals()[("Salvagnini", date(2026, 1, 1))], 0.97)
        self.assertEqual(self.repo.load_display_names()["3101"], "LEGACY-MV")
        rules = self.repo.load_linked_rules()
        self.assertEqual([(r.parent_asset_number, r.linked_asset_number, r.impact_factor) for r in rules],
                         [("3102", "3101", 0.25)])
        group = next(g for g in self.repo.load_groups() if g.asset_group == "Salvagnini")
        self.assertEqual(group.net_scheduled_hours_per_day, 18.0)
        self.assertEqual(group.notes, "legacy notes")
        self.assertEqual(group.asset_numbers, ("3101",))

    def test_overtime_rows_colliding_on_the_new_key_keep_the_later_edit(self):
        self.repo.ensure_schema()
        self.assertEqual(self.repo.load_manual_ot()[("3101", date(2026, 1, 1))], 40.0)

    def test_seeding_does_not_re_add_groups_the_legacy_database_already_had(self):
        self.repo.ensure_schema()
        names = [g.asset_group for g in self.repo.load_groups()]
        self.assertEqual(names, ["Salvagnini"])
        self.assertEqual(len(names), len(set(names)))

    def test_the_orphaned_results_table_is_left_alone(self):
        """Those rows are the operator's to discard, not ours."""

        self.repo.ensure_schema()
        with sqlite3.connect(self.db_path) as conn:
            found = conn.execute(
                "SELECT name FROM sqlite_master WHERE name = 'availability_results'"
            ).fetchone()
        self.assertIsNotNone(found)

    def test_no_staging_tables_are_left_behind(self):
        self.repo.ensure_schema()
        with sqlite3.connect(self.db_path) as conn:
            leftovers = conn.execute(
                "SELECT name FROM sqlite_master WHERE name LIKE '%__migrating'"
            ).fetchall()
        self.assertEqual(leftovers, [])

    def test_writes_work_after_migrating(self):
        self.repo.ensure_schema()
        self.repo.save_manual_ot("3101", "2026-02-01", 8)
        self.repo.save_goal("Salvagnini", "2026-02-01", 0.9)
        self.repo.save_display_name("3101", "MV")
        self.assertEqual(self.repo.load_manual_ot()[("3101", date(2026, 2, 1))], 8.0)
        self.assertEqual(self.repo.load_goals()[("Salvagnini", date(2026, 2, 1))], 0.9)
        self.assertEqual(self.repo.load_display_names()["3101"], "MV")


class SeedOnceTests(AvailabilityTestCase):
    """An empty configuration is a configuration, not an uninitialised database."""

    def test_a_deliberately_emptied_rule_set_survives_a_restart(self):
        """Otherwise Salvagnini's availability changes on its own overnight.

        A count-based seed cannot tell "the user deleted every rule" from "this
        database is new", so it restores all thirteen defaults on the next
        process start and quietly rewrites the numbers.
        """

        self.assertEqual(len(self.repo.load_linked_rules()), 13)
        self.repo.replace_linked_rules([])
        self.assertEqual(len(self.repo.load_linked_rules()), 0)

        AvailabilityRepository(self.db_path).ensure_schema()  # next process start
        self.assertEqual(len(self.repo.load_linked_rules()), 0)
        AvailabilityRepository(self.db_path).ensure_schema()  # and the one after
        self.assertEqual(len(self.repo.load_linked_rules()), 0)

    def test_a_narrowed_rule_set_is_not_topped_back_up(self):
        self.repo.replace_linked_rules(
            [{"parent_asset_number": "3102", "linked_asset_number": "3101", "impact_factor": 0.25}]
        )
        AvailabilityRepository(self.db_path).ensure_schema()
        rules = self.repo.load_linked_rules()
        self.assertEqual(len(rules), 1)
        self.assertEqual(rules[0].impact_factor, 0.25)

    def test_a_fresh_database_still_gets_its_defaults(self):
        fresh = Path(self._tmp.name) / "fresh.db"
        repo = AvailabilityRepository(fresh)
        repo.ensure_schema()
        self.assertEqual(len(repo.load_linked_rules()), 13)
        self.assertEqual(len(repo.load_groups()), 10)

    def test_a_legacy_database_seeds_once_on_upgrade_then_stops(self):
        """The flag defaults to 0, so an upgraded database is filled in once."""

        legacy = Path(self._tmp.name) / "legacy.db"
        with sqlite3.connect(legacy) as conn:
            conn.executescript(_LEGACY_SCHEMA)
            conn.execute("INSERT INTO availability_settings(id, selected_year) VALUES (1, 2026)")
        repo = AvailabilityRepository(legacy)
        repo.ensure_schema()
        self.assertEqual(len(repo.load_linked_rules()), 13)
        repo.replace_linked_rules([])
        AvailabilityRepository(legacy).ensure_schema()
        self.assertEqual(len(repo.load_linked_rules()), 0)


class TimezoneAvailabilityTests(AvailabilityTestCase):
    """A missing timezone database must not quietly become UTC.

    GREMLIN ships to Windows, where Python has no system IANA database, so
    without the ``tzdata`` package ``ZoneInfo`` cannot load the plant timezone at
    all. Falling back to UTC keeps the page working but moves work orders
    created near local midnight into the wrong month -- and defeats
    ``plant_today`` during exactly the hours it exists for.
    """

    def _without_timezone_database(self):
        """Simulate a stock Windows install: no tzpath, no tzdata package."""

        original_path = list(zoneinfo.TZPATH)
        zoneinfo.reset_tzpath([])
        zoneinfo.ZoneInfo.clear_cache()
        patched = mock.patch.dict(sys.modules, {"tzdata": None})
        patched.start()

        def restore():
            patched.stop()
            zoneinfo.reset_tzpath(original_path)
            zoneinfo.ZoneInfo.clear_cache()

        self.addCleanup(restore)

    def test_tzdata_is_a_declared_dependency(self):
        """The fix is a runtime dependency, not just a nicer error message."""

        requirements = (Path(__file__).resolve().parent.parent / "requirements.txt").read_text()
        packages = [
            line.strip().lower()
            for line in requirements.splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        self.assertIn("tzdata", packages)

    def test_a_working_timezone_produces_no_warning(self):
        self.assertIsNone(self.repo.timezone_warning())

    def test_a_missing_database_is_reported_not_hidden(self):
        self._without_timezone_database()
        warning = self.repo.timezone_warning()
        self.assertIsNotNone(warning)
        self.assertIn("tzdata", warning)
        self.assertIn("UTC", warning)

    def test_the_dashboard_payload_carries_the_warning(self):
        self._without_timezone_database()
        data = build_dashboard(self.repo, months=1, today=date(2026, 2, 15))
        self.assertIn("tzdata", data["timezone_warning"])

    def test_an_unrecognised_name_is_not_blamed_on_a_missing_database(self):
        """Same exception, opposite remedies: install a package vs fix a typo."""

        with sqlite3.connect(self.db_path) as conn:
            conn.execute("UPDATE availability_settings SET timezone = 'Mars/Olympus' WHERE id = 1")
        warning = self.repo.timezone_warning()
        self.assertIn("not a recognised timezone", warning)
        self.assertNotIn("tzdata", warning)

    def test_the_page_still_computes_when_the_timezone_cannot_be_loaded(self):
        self._without_timezone_database()
        self.add_work_order("3101", "2026-01-15 15:00:00", 10.0)
        data = build_dashboard(self.repo, months=1, today=date(2026, 2, 15))
        group = next(g for g in data["groups"] if g["asset_group"] == "Salvagnini")
        row = next(r for r in group["rows"] if r["asset_number"] == "3101")
        self.assertEqual(row["direct_downtime_hours"], 10.0)


class PlantTimezoneTests(AvailabilityTestCase):
    """Month completeness is judged on the plant's clock, not the server's."""

    def _at(self, utc_instant):
        """Run with the wall clock pinned to a given UTC instant."""

        real = repositories.availability_repo.datetime

        class Frozen(real):
            @classmethod
            def now(cls, tz=None):
                return utc_instant.astimezone(tz) if tz else utc_instant.replace(tzinfo=None)

        return mock.patch.object(repositories.availability_repo, "datetime", Frozen)

    def test_plant_today_lags_a_utc_server_across_midnight(self):
        # 02:00 UTC on 1 August is 21:00 on 31 July in Chicago.
        with self._at(datetime(2026, 8, 1, 2, 0, tzinfo=dt_timezone.utc)):
            self.assertEqual(self.repo.plant_today(), date(2026, 7, 31))

    def test_a_month_is_not_charted_until_it_is_over_where_the_plant_is(self):
        """July's downtime is still arriving; charting it against full
        scheduled hours would report an availability that only reads high."""

        self.add_work_order("3101", "2026-06-15 15:00:00", 4.0)
        self.add_work_order("3101", "2026-07-15 15:00:00", 4.0)
        with self._at(datetime(2026, 8, 1, 2, 0, tzinfo=dt_timezone.utc)):
            data = build_dashboard(self.repo, months=12)
        self.assertNotIn("2026-07-01", data["months"])
        self.assertIn("2026-06-01", data["months"])

    def test_the_month_appears_once_the_plant_has_rolled_over(self):
        self.add_work_order("3101", "2026-06-15 15:00:00", 4.0)
        self.add_work_order("3101", "2026-07-15 15:00:00", 4.0)
        # 12:00 UTC on 1 August is 07:00 on 1 August in Chicago.
        with self._at(datetime(2026, 8, 1, 12, 0, tzinfo=dt_timezone.utc)):
            data = build_dashboard(self.repo, months=12)
        self.assertIn("2026-07-01", data["months"])

    def test_an_explicit_today_still_wins(self):
        """Tests and callers keep their injection point."""

        self.add_work_order("3101", "2026-06-15 15:00:00", 4.0)
        data = build_dashboard(self.repo, months=1, today=date(2026, 7, 15))
        self.assertEqual(data["months"], ["2026-06-01"])


class UnmigratedMappedTableTests(AvailabilityTestCase):
    """A GREMLIN.db predating a mapped_cmms_record migration must not 500.

    The card is the Metrics page's first request, so it is the one that meets an
    un-migrated database after an upgrade. Long-lived GREMLIN.db files only ever
    gain columns, so "the column isn't there yet" is a real state, not a
    hypothetical.
    """

    def _drop_column(self, column):
        with sqlite3.connect(self.db_path) as conn:
            try:
                conn.execute(f"ALTER TABLE mapped_cmms_record DROP COLUMN {column}")
            except sqlite3.OperationalError:
                self.skipTest("SQLite build does not support DROP COLUMN")

    def test_a_missing_downtime_hours_column_falls_back_to_minutes(self):
        self.add_work_order("3101", "2026-01-15 15:00:00", 2.0)
        self._drop_column("downtime_hours")
        orders = self.repo.load_work_orders(["3101"])
        self.assertEqual(len(orders), 1)
        self.assertEqual(orders[0].downtime_hours, 2.0)

    def test_loading_falls_back_to_whichever_date_columns_remain(self):
        """The fallback chain must narrow to the columns that exist, not break."""

        self.add_work_order(
            "3101", "2026-01-15 15:00:00", 2.0,
            completedDate_Final="2026-01-16 12:00:00",
        )
        # The two secondary completed-date columns; completed_date_final itself
        # is indexed, so SQLite will not drop it.
        self._drop_column("completed_datetime_raw")
        self._drop_column("completed_date_raw")
        orders = self.repo.load_work_orders(["3101"])
        self.assertEqual(len(orders), 1)
        self.assertIsNotNone(orders[0].completed_local)
        self.assertEqual(orders[0].completed_local.day, 16)

    def test_the_existence_check_survives_a_missing_column(self):
        self.add_work_order("3101", "2026-01-15 15:00:00", 2.0)
        self._drop_column("created_datetime_raw")
        self.assertTrue(self.repo.has_any_work_orders())

    def test_the_dashboard_still_computes_on_an_unmigrated_database(self):
        self.add_work_order("3101", "2026-01-15 15:00:00", 2.0)
        self._drop_column("downtime_hours")
        data = build_dashboard(self.repo, months=1, today=date(2026, 2, 15))
        group = next(g for g in data["groups"] if g["asset_group"] == "Salvagnini")
        row = next(r for r in group["rows"] if r["asset_number"] == "3101")
        self.assertEqual(row["direct_downtime_hours"], 2.0)


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
        self.assertEqual(data["empty_reason"], "No work orders have been imported yet.")

    def test_work_orders_for_unconfigured_assets_are_not_reported_as_an_empty_import(self):
        """Three empty screens, three different things to go fix.

        A database holding work orders only for assets nobody has added to a
        group is a configuration gap. Reporting "nothing imported" would send
        someone to re-run a Limble sync that is already working.
        """

        self.add_work_order("99999", "2026-06-15 15:00:00", 4.0)
        data = build_dashboard(self.repo, months=5, today=date(2026, 8, 6))
        self.assertEqual(data["groups"], [])
        self.assertIn("configured asset groups", data["empty_reason"])
        self.assertNotIn("imported", data["empty_reason"])

    def test_no_configured_assets_says_so(self):
        for group in self.repo.load_groups():
            self.repo.save_group_schedule(
                group.asset_group,
                schedule_hours_per_day=group.schedule_hours_per_day,
                break_hours_per_day=group.break_hours_per_day,
                lunch_hours_per_day=group.lunch_hours_per_day,
                setup_hours_per_day=group.setup_hours_per_day,
                include=group.include,
                asset_numbers=[],
            )
        self.add_work_order("3101", "2026-06-15 15:00:00", 4.0)
        data = build_dashboard(self.repo, months=5, today=date(2026, 8, 6))
        self.assertIn("No asset groups are configured", data["empty_reason"])

    def test_an_asset_with_no_work_orders_still_charts_when_others_have_data(self):
        """The per-asset no-entry note only applies once the window can resolve."""

        # Building 6 Finishing, so no linked rule feeds the quiet asset --
        # in Salvagnini a silent machine still inherits its neighbours' hours.
        self.add_work_order("4001", "2026-06-15 15:00:00", 4.0)
        data = build_dashboard(self.repo, months=1, today=date(2026, 7, 15))
        group = next(g for g in data["groups"] if g["asset_group"] == "Building 6 Finishing")
        quiet = next(r for r in group["rows"] if r["asset_number"] == "4002")
        self.assertEqual(quiet["availability"], 1.0)
        self.assertTrue(quiet["no_wo_entries"])
        self.assertEqual(quiet["note"], "No WO entries this month")
        # And the asset that does have data is not at 100%.
        busy = next(r for r in group["rows"] if r["asset_number"] == "4001")
        self.assertLess(busy["availability"], 1.0)

    def test_has_any_work_orders_ignores_undated_rows(self):
        self.assertFalse(self.repo.has_any_work_orders())
        self.add_work_order("3101", "2026-06-15 15:00:00", 1.0)
        self.assertTrue(self.repo.has_any_work_orders())

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

    def test_a_linked_asset_outside_the_group_still_contributes(self):
        """A rule pointing at a non-member must not silently contribute zero.

        Rules outlive membership: an asset removed from a group, or a group
        excluded, leaves rules that still name it. If its downtime is never
        loaded the parent quietly loses those hours, which reads as an
        improvement rather than as missing data.
        """

        self.add_work_order("3102", "2026-01-15 15:00:00", 12.5)
        self.add_work_order("3101", "2026-01-15 15:00:00", 10.0)

        def linked_hours_for_3102():
            data = build_dashboard(self.repo, months=1, today=date(2026, 2, 15))
            group = next(g for g in data["groups"] if g["asset_group"] == "Salvagnini")
            return next(r for r in group["rows"] if r["asset_number"] == "3102")["linked_downtime_hours"]

        self.assertEqual(linked_hours_for_3102(), 5.0)  # 10.0 x 0.5

        # Decommission 3101 from the group; the 3102 -> 3101 rule remains.
        self.repo.save_group_schedule(
            "Salvagnini",
            schedule_hours_per_day=24, break_hours_per_day=1,
            lunch_hours_per_day=2, setup_hours_per_day=3, include=True,
            asset_numbers=["3102", "3103", "3104", "3105", "3106", "3107"],
        )
        self.assertEqual(linked_hours_for_3102(), 5.0)

    def test_a_linked_asset_outside_the_group_is_not_charted(self):
        self.add_work_order("3102", "2026-01-15 15:00:00", 12.5)
        self.add_work_order("3101", "2026-01-15 15:00:00", 10.0)
        self.repo.save_group_schedule(
            "Salvagnini",
            schedule_hours_per_day=24, break_hours_per_day=1,
            lunch_hours_per_day=2, setup_hours_per_day=3, include=True,
            asset_numbers=["3102", "3103", "3104", "3105", "3106", "3107"],
        )
        data = build_dashboard(self.repo, months=1, today=date(2026, 2, 15))
        group = next(g for g in data["groups"] if g["asset_group"] == "Salvagnini")
        self.assertNotIn("3101", [a["asset_number"] for a in group["assets"]])
        self.assertNotIn("3101", data["all_asset_numbers"])

    def test_the_window_follows_charted_assets_not_stale_rules(self):
        """A decommissioned asset's history must not stretch the axis."""

        self.add_work_order("3102", "2026-06-15 15:00:00", 4.0)
        self.add_work_order("3101", "2026-01-15 15:00:00", 10.0)
        self.repo.save_group_schedule(
            "Salvagnini",
            schedule_hours_per_day=24, break_hours_per_day=1,
            lunch_hours_per_day=2, setup_hours_per_day=3, include=True,
            asset_numbers=["3102"],
        )
        # June is the only month a charted asset has data in, so it is both
        # bounds. January belongs to 3101, which is loaded for its linked hours
        # but is no longer charted, so it must not pull the window back.
        data = build_dashboard(self.repo, months=12, today=date(2026, 8, 6))
        self.assertEqual(data["months"], ["2026-06-01"])

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


class WriteFailureTests(AvailabilityTestCase):
    """A blocked write must arrive as advice, not as a stack trace.

    GREMLIN.db sits on a network share and serialises writers through
    ``BEGIN IMMEDIATE``, so "another user is saving" is an everyday event. Left
    as a raw ``sqlite3.OperationalError: database is locked`` it reaches the API
    as a generic 500; as a DatabaseWriteError it becomes the actionable 503 the
    rest of the app already uses.
    """

    def setUp(self):
        super().setUp()
        self._patched = repositories.availability_repo.DB_WRITE_TIMEOUT_SECONDS
        repositories.availability_repo.DB_WRITE_TIMEOUT_SECONDS = 1
        self.addCleanup(
            setattr, repositories.availability_repo, "DB_WRITE_TIMEOUT_SECONDS", self._patched
        )

    def _hold_the_writer_slot(self):
        blocker = sqlite3.connect(self.db_path, timeout=1)
        blocker.execute("PRAGMA busy_timeout = 0")
        blocker.execute("BEGIN IMMEDIATE")
        self.addCleanup(blocker.close)
        self.addCleanup(blocker.rollback)

    def test_a_locked_database_raises_the_actionable_error(self):
        self._hold_the_writer_slot()
        repo = AvailabilityRepository(self.db_path)
        with self.assertRaises(DatabaseWriteError) as ctx:
            repo.save_goal("Salvagnini", "2026-01-01", 0.9)
        message = str(ctx.exception)
        self.assertIn("another GREMLIN user", message)
        self.assertIn(str(self.db_path), message)

    def test_a_config_error_is_not_dressed_up_as_a_write_failure(self):
        repo = AvailabilityRepository(self.db_path)
        with self.assertRaises(AvailabilityConfigError):
            repo.save_goal("Salvagnini", "2026-01-01", 5.0)


class WorkOrderDrillDownTests(AvailabilityTestCase):
    """The payload behind one bar, checked against the bar it explains."""

    def bar(self, asset, month=date(2026, 1, 1), group="Salvagnini"):
        data = build_dashboard(self.repo, months=1, today=date(2026, 2, 5))
        rows = next(g for g in data["groups"] if g["asset_group"] == group)["rows"]
        return next(r for r in rows if r["asset_number"] == asset and r["month"] == month.isoformat())

    def drill(self, asset, month="2026-01-01", group="Salvagnini"):
        return build_work_order_detail(
            self.repo, asset_group=group, asset_number=asset, month=month
        )

    def test_the_totals_match_the_chart_and_the_workbook(self):
        self.seed_salvagnini_january()
        detail = self.drill("3102")
        self.assertEqual(detail["direct_downtime_hours"], 12.5)
        self.assertEqual(detail["linked_downtime_hours"], 24.43)
        self.assertAlmostEqual(detail["availability"], JANUARY_AVAILABILITY["3102"], places=12)
        self.assertEqual(detail["availability"], self.bar("3102")["availability"])
        self.assertEqual(detail["scheduled_hours"], 396.0)

    def test_the_listed_work_orders_add_up_to_the_bar(self):
        self.seed_salvagnini_january()
        detail = self.drill("3102")
        by_source = {"direct": 0.0, "linked": 0.0}
        for order in detail["work_orders"]:
            by_source[order["source"]] += order["counted_hours"]
        self.assertAlmostEqual(by_source["direct"], detail["direct_downtime_hours"], places=2)
        self.assertAlmostEqual(by_source["linked"], detail["linked_downtime_hours"], places=2)

    def test_linked_work_orders_name_the_machine_and_the_share(self):
        self.seed_salvagnini_january()
        detail = self.drill("3102")
        linked = {o["asset_number"]: o for o in detail["work_orders"] if o["source"] == "linked"}
        self.assertEqual(set(linked), {"3101", "3105", "3106", "3107"})
        self.assertEqual(linked["3101"]["downtime_hours"], 10.0)
        self.assertEqual(linked["3101"]["counted_hours"], 5.0)
        self.assertEqual(linked["3101"]["impact_factor"], 0.5)
        self.assertEqual(
            [a["display_name"] for a in detail["linked_assets"]], ["MV", "S4", "SMD", "ACN"]
        )

    def test_an_asset_with_no_rules_reports_no_linked_assets(self):
        self.seed_salvagnini_january()
        detail = self.drill("3101")
        self.assertEqual(detail["linked_assets"], [])
        self.assertTrue(all(o["source"] == "direct" for o in detail["work_orders"]))

    def test_an_asset_with_no_downtime_of_its_own_still_lists_its_linked_hours(self):
        """3104 recorded no downtime in January and is still charged 5 h.

        This is the case the view exists for: the summary shows a work order
        that cost nothing next to a number that is plainly not 100%, and the
        only thing that explains the gap is another machine's breakdown.
        """

        self.seed_salvagnini_january()
        detail = self.drill("3104")
        self.assertEqual(detail["direct_downtime_hours"], 0)
        self.assertEqual(detail["zero_downtime_wo_count"], 1)
        self.assertEqual(detail["linked_downtime_hours"], 5.0)

        by_source = {o["source"]: o for o in detail["work_orders"]}
        self.assertEqual(set(by_source), {"direct", "linked"})
        self.assertEqual(by_source["direct"]["asset_number"], "3104")
        self.assertEqual(by_source["direct"]["counted_hours"], 0.0)
        self.assertEqual(by_source["linked"]["asset_number"], "3101")
        self.assertEqual(by_source["linked"]["counted_hours"], 5.0)

    def test_a_bar_with_nothing_behind_it_lists_nothing(self):
        """The true 100%: no work orders, no rules, nothing to show."""

        self.seed_salvagnini_january()
        empty = self.drill("4001", group="Building 6 Finishing")
        self.assertEqual(empty["work_orders"], [])
        self.assertEqual(empty["linked_assets"], [])
        self.assertEqual(empty["total_wo_count"], 0)
        self.assertEqual(empty["availability"], 1.0)
        self.assertEqual(empty["note"], "No WO entries this month")

    def test_the_text_a_reader_needs_is_present(self):
        self.add_work_order(
            "3101", "2026-01-15 15:00:00", 6.0,
            name="MV hydraulic fault", status="Completed", type="6",
            description="Pressure alarm.", completionNotes="Replaced seal. RTS at 4:30 PM.",
        )
        order = self.drill("3101")["work_orders"][0]
        self.assertEqual(order["task_name"], "MV hydraulic fault")
        self.assertEqual(order["description"], "Pressure alarm.")
        self.assertEqual(order["completion_notes"], "Replaced seal. RTS at 4:30 PM.")
        self.assertEqual(order["status"], "Completed")
        self.assertTrue(order["task_id"])
        self.assertEqual(order["created"], "2026-01-15T09:00:00")

    def test_the_source_type_travels_alongside_the_classification(self):
        """Both, because they disagree and §2.1 is about which one lied.

        The workbook dropped 2,410 rows believing type 4 meant PM. A reader
        auditing a counted row needs to see what Limble called it *and* what
        this app called it, so the drill-down carries both and filters on
        neither.
        """

        self.add_work_order("3101", "2026-01-15 15:00:00", 3.0, type="4", name="Rebuild spindle")
        order = self.drill("3101")["work_orders"][0]
        self.assertEqual(order["type_raw"], "4")
        self.assertIn("record_class", order)

    def test_hours_are_sent_finer_than_they_are_displayed(self):
        """Downtime is minute-granular, so two decimals cannot be the wire format.

        Three one-minute orders are 0.0167 h each and 0.05 h together. Rounding
        each to the two decimals the table shows would make the rows visibly
        fail to reach the total, so the payload keeps enough precision for the
        client to choose a precision that adds up.
        """

        for _ in range(3):
            self.add_work_order("3101", "2026-01-15 15:00:00", 1 / 60)
        detail = self.drill("3101")
        self.assertEqual(detail["adjusted_downtime_hours"], 0.05)
        self.assertEqual([o["counted_hours"] for o in detail["work_orders"]], [0.0167] * 3)

    def test_month_labels_carry_the_year(self):
        """A single month has no neighbouring columns to date it by."""

        self.seed_salvagnini_january()
        self.assertEqual(self.drill("3101")["month_label"], "Jan 2026")

    def test_manual_overtime_reaches_the_header(self):
        self.seed_salvagnini_january()
        self.repo.save_manual_ot("3101", "2026-01-01", 40.0)
        detail = self.drill("3101")
        self.assertEqual(detail["manual_ot_hours"], 40.0)
        self.assertEqual(detail["adjusted_scheduled_hours"], 436.0)

    def test_a_bar_clamped_to_zero_still_itemises_its_hours(self):
        self.add_work_order("3101", "2026-01-15 15:00:00", 500.0)
        detail = self.drill("3101")
        self.assertTrue(detail["flagged"])
        self.assertEqual(detail["availability"], 0.0)
        self.assertEqual(detail["work_orders"][0]["counted_hours"], 500.0)

    def test_a_month_with_no_data_is_answerable_not_an_error(self):
        """Months outside the chart's window are still legitimate questions."""

        self.seed_salvagnini_january()
        detail = self.drill("3101", month="2026-07-01")
        self.assertEqual(detail["work_orders"], [])
        self.assertEqual(detail["month_label"], "Jul 2026")

    def test_an_unknown_group_asset_or_month_is_a_config_error(self):
        self.seed_salvagnini_january()
        with self.assertRaises(AvailabilityConfigError):
            self.drill("3101", group="Nope")
        with self.assertRaises(AvailabilityConfigError):
            self.drill("9999")
        with self.assertRaises(AvailabilityConfigError):
            self.drill("3101", month="last winter")
        with self.assertRaises(AvailabilityConfigError):
            self.drill("3101", month="")

    def test_an_excluded_group_is_not_drillable(self):
        with self.assertRaises(AvailabilityConfigError):
            self.drill("3101", group="Dilo & Enervac")

    def test_month_parsing_accepts_both_shapes(self):
        self.assertEqual(parse_month("2026-03"), date(2026, 3, 1))
        self.assertEqual(parse_month("2026-03-17"), date(2026, 3, 1))


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

    def test_the_work_order_endpoint_itemises_a_bar(self):
        self.seed_salvagnini_january()
        response = self.client.get(
            "/metrics/api/availability/work-orders"
            "?asset_group=Salvagnini&asset_number=3102&month=2026-01-01"
        )
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        data = response.get_json()
        self.assertAlmostEqual(data["availability"], JANUARY_AVAILABILITY["3102"], places=12)
        self.assertEqual(len(data["work_orders"]), 5)  # its own, plus four linked
        self.assertAlmostEqual(
            sum(o["counted_hours"] for o in data["work_orders"]),
            data["adjusted_downtime_hours"],
            places=2,
        )

    def test_a_group_name_with_an_ampersand_reaches_the_work_order_endpoint(self):
        response = self.client.get(
            "/metrics/api/availability/work-orders"
            "?asset_group=Dilo %26 Enervac&asset_number=3101&month=2026-01-01"
        )
        # Excluded group, so a 400 -- but a *routing* failure would be a 404,
        # and the message has to name the group it actually parsed.
        self.assertEqual(response.status_code, 400)
        self.assertIn("Dilo & Enervac", response.get_json()["error"])

    def test_a_bad_work_order_request_returns_400_with_a_readable_reason(self):
        for query, expected in (
            ("asset_group=Nope&asset_number=3101&month=2026-01-01", "Nope"),
            ("asset_group=Salvagnini&asset_number=9999&month=2026-01-01", "9999"),
            ("asset_group=Salvagnini&asset_number=3101&month=nonsense", "nonsense"),
            ("asset_group=Salvagnini&asset_number=3101", "month"),
            ("asset_group=Salvagnini&month=2026-01-01", "asset_number"),
        ):
            with self.subTest(query=query):
                response = self.client.get(f"/metrics/api/availability/work-orders?{query}")
                self.assertEqual(response.status_code, 400)
                self.assertIn(expected, response.get_json()["error"])

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

    def test_raw_records_are_mapped_on_demand_before_availability_reads_them(self):
        """Availability is the page's first request, so it must bootstrap itself.

        Right after an import the raw rows are present and mapped_cmms_record is
        still empty. Reporting "no work orders have been imported" then is not
        just unhelpful, it is the opposite of true -- and nothing retries the
        card once a later request populates the table.
        """

        self.add_work_order("3101", "2026-06-15 15:00:00", 2.0)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("DELETE FROM mapped_cmms_record")
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM mapped_cmms_record").fetchone()[0], 0)
        self.app_module._life_data_service = None

        response = self.client.get("/metrics/api/availability?months=1")
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertIsNone(data.get("empty_reason"))
        group = next(g for g in data["groups"] if g["asset_group"] == "Salvagnini")
        row = next(r for r in group["rows"] if r["asset_number"] == "3101")
        self.assertEqual(row["direct_downtime_hours"], 2.0)

    def test_the_metrics_page_still_renders(self):
        self.assertEqual(self.client.get("/metrics").status_code, 200)

    def test_the_settings_page_still_renders(self):
        self.assertEqual(self.client.get("/settings").status_code, 200)


if __name__ == "__main__":
    unittest.main()
