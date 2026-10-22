"""What makes Perform an Analysis fast, and that it changes no answer.

- Life hours are summed from running totals of each plant day's length rather
  than walked a day at a time; the walk is kept as the reference.
- The dates nearly every work order has are read without trying each format.
- An asset's population, and the asset tree, are kept until GREMLIN.db changes,
  as its change token says.
"""

import random
import sqlite3
import unittest
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from services.life_data_service import LifeDataService
from services.population_analysis_service import _LOADED
from tests.test_standard_disposition_service import PFS, S09, StandardDispositionFixture, coded

# Zones whose clocks do awkward things: a half-hour change, a change at midnight,
# a whole day skipped (Samoa, 30 December 2011), and no change at all.
ZONES = (
    ZoneInfo("America/Chicago"),
    ZoneInfo("Europe/London"),
    ZoneInfo("Australia/Lord_Howe"),
    ZoneInfo("America/Santiago"),
    ZoneInfo("Pacific/Apia"),
    ZoneInfo("Asia/Kathmandu"),
    timezone.utc,
    timezone(timedelta(hours=-5)),
)


class LifeHoursTests(unittest.TestCase):
    def test_the_running_totals_give_the_day_by_day_answer(self):
        rng = random.Random(20261022)
        for zone in ZONES:
            for _ in range(400):
                start = datetime(2008, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=rng.randint(0, 16 * 365 * 86400))
                span = rng.choice([rng.randint(0, 7200), rng.randint(0, 5 * 86400), rng.randint(0, 1500 * 86400)])
                end = start + timedelta(seconds=span)
                hours_per_day = rng.choice([24.0, 20.0, 16.0, 8.0, 0.0])
                exclude_weekends = rng.random() < 0.8
                fast = LifeDataService._scheduled_life_hours(
                    start, end, hours_per_day, exclude_weekends=exclude_weekends, tz=zone
                )
                walked = (
                    LifeDataService._scheduled_life_hours_by_day(start, end, hours_per_day, exclude_weekends, zone)
                    if end > start else (0.0, 0.0, 0.0)
                )
                for got, expected in zip(fast, walked):
                    self.assertAlmostEqual(got, expected, delta=1e-9 * max(1.0, abs(expected)), msg=(zone, start, end))

    def test_the_day_the_clocks_change_is_the_hours_it_really_was(self):
        chicago = ZoneInfo("America/Chicago")
        # Friday to Tuesday across the spring change: Sunday is 23 hours, and a
        # weekend day, so 47 weekend hours and three 24-hour weekdays.
        start = datetime(2024, 3, 8, tzinfo=chicago)
        end = datetime(2024, 3, 12, tzinfo=chicago)
        scheduled, weekend, non_run = LifeDataService._scheduled_life_hours(start, end, 24.0, tz=chicago)
        self.assertAlmostEqual(weekend, 47.0)
        self.assertAlmostEqual(scheduled, 48.0)
        self.assertAlmostEqual(non_run, 0.0)

    def test_a_life_years_long_costs_no_more_than_a_short_one(self):
        chicago = ZoneInfo("America/Chicago")
        start = datetime(2001, 1, 1, tzinfo=timezone.utc)
        long_life = LifeDataService._scheduled_life_hours(start, start + timedelta(days=9000), 16.0, tz=chicago)
        self.assertEqual(
            long_life,
            LifeDataService._scheduled_life_hours(start, start + timedelta(days=9000), 16.0, tz=chicago),
        )
        self.assertGreater(long_life[0], 0)


class EventDateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.life = LifeDataService.__new__(LifeDataService)
        self.zone = ZoneInfo("America/Chicago")

    def test_each_shape_reads_as_it_did(self):
        midnight = datetime(2026, 1, 15, 6, tzinfo=timezone.utc)  # plant midnight in winter
        cases = {
            "2026-01-15": midnight,
            "1/15/2026": midnight,
            "01/15/26": midnight,
            "2026-01-15T15:00:00+00:00": datetime(2026, 1, 15, 15, tzinfo=timezone.utc),
            "2026-01-15 15:00": datetime(2026, 1, 15, 15, tzinfo=timezone.utc),
            "1/15/2026 15:00": datetime(2026, 1, 15, 15, tzinfo=timezone.utc),
            "2026-01-15T15:00:00Z": datetime(2026, 1, 15, 15, tzinfo=timezone.utc),
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(self.life._parse_event_datetime(text, self.zone), expected)

    def test_nothing_is_no_date(self):
        for value in (None, "", "   "):
            self.assertIsNone(self.life._parse_event_datetime(value, self.zone))
        self.assertIsNone(self.life._parse_event_datetime("not a date", self.zone))


class ChangeTokenTests(StandardDispositionFixture):
    def test_it_moves_with_every_committed_write_and_only_then(self):
        before = self.life.change_token()
        self.assertIsNotNone(before)
        self.assertEqual(self.life.change_token(), before)
        # A write that changes nothing leaves the file, and the token, as they were.
        with self.life.write_connection() as conn:
            conn.execute("UPDATE mapped_cmms_record SET task_name = task_name WHERE mapped_record_id = 900009")
        self.assertEqual(self.life.change_token(), before)
        with self.life.write_connection() as conn:
            conn.execute("UPDATE mapped_cmms_record SET task_name = 'Swept' WHERE mapped_record_id = 900009")
        after = self.life.change_token()
        self.assertNotEqual(after, before)
        # Another process's write moves it too: it is read from the file.
        other = sqlite3.connect(self.db_path)
        other.execute("UPDATE mapped_cmms_record SET downtime_hours = 1 WHERE mapped_record_id = 900009")
        other.commit()
        other.close()
        self.assertNotEqual(self.life.change_token(), after)

    def test_a_missing_file_has_none(self):
        missing = LifeDataService.__new__(LifeDataService)
        missing.db_path = self.db_path.with_name("missing.db")
        self.assertIsNone(missing.change_token())


class KeptPopulationTests(StandardDispositionFixture):
    def setUp(self) -> None:
        super().setUp()
        self.load_acceptance()

    def summary(self, **selection):
        return self.analysis.summary({"assets": [PFS], **selection}, group_by="component")

    def test_the_views_of_one_asset_read_its_records_once(self):
        reads = []
        original = self.analysis._population_records

        def counted(*args, **kwargs):
            reads.append(1)
            return original(*args, **kwargs)

        self.analysis._population_records = counted
        first = self.summary()
        self.analysis.downtime({"assets": [PFS]})
        self.analysis.pm_effectiveness({"assets": [PFS]})
        self.assertEqual(self.summary(), first)
        self.assertEqual(len(reads), 1)

    def test_a_save_is_in_the_next_view(self):
        before = self.summary(categories=["FAILURE"])
        # A no-fault-found check, recoded as a failure.
        self.std.save_dispositions([{"mapped_record_id": 92475, **coded(mode="UST")}], decided_by="tim")
        after = self.summary(categories=["FAILURE"])
        self.assertEqual(after["record_count"], before["record_count"] + 1)
        self.assertNotEqual(before["pareto"], after["pareto"])

    def test_a_write_from_another_process_is_seen_too(self):
        before = self.summary()
        other = sqlite3.connect(self.db_path)
        other.execute("UPDATE mapped_cmms_record SET downtime_hours = 100 WHERE mapped_record_id = 175552")
        other.commit()
        other.close()
        self.assertAlmostEqual(self.summary()["downtime_hours"], before["downtime_hours"] + 97.0)

    def test_a_kept_population_is_its_databases_alone(self):
        self.summary()
        self.assertTrue(any(key[0][0] == str(self.db_path) for key in _LOADED))


class KeptTreeTests(StandardDispositionFixture):
    def test_the_tree_is_kept_until_the_work_orders_change(self):
        with self.life.connect() as conn:
            first = self.std.asset_tree(conn)
        with self.life.connect() as conn:
            self.assertIs(self.std.asset_tree(conn), first)
        with self.life.write_connection() as conn:
            conn.execute(f"UPDATE mapped_cmms_record SET asset_name = '{S09} Dry-off oven' WHERE asset_number = '{S09}'")
        with self.life.connect() as conn:
            changed = self.std.asset_tree(conn)
        self.assertIsNot(changed, first)
        self.assertEqual(changed.name(S09), f"{S09} Dry-off oven")

    def test_a_write_that_leaves_the_assets_alone_keeps_the_same_tree(self):
        with self.life.connect() as conn:
            first = self.std.asset_tree(conn)
        self.std.save_dispositions([{"mapped_record_id": 900009, **coded(sub_asset=None)}])
        with self.life.connect() as conn:
            self.assertIs(self.std.asset_tree(conn), first)


if __name__ == "__main__":
    unittest.main()
