import os
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from repositories.raw_repo import RawRepository
from services import sync_service
from services.ingestion_service import PHASE_ASSETS, PHASE_MAP, PHASE_TASKS, PHASE_TRANSFORM, PHASE_WRITE
from services.ingestion_service import DEFAULT_INSTRUCTIONS_LIMIT, PHASE_INSTRUCTIONS
from services.sync_service import (
    STATE_FAILED,
    STATE_IDLE,
    STATE_RUNNING,
    STATE_SUCCEEDED,
    LimbleSyncRunner,
    SyncAlreadyRunningError,
    SyncOptionError,
    SyncOptions,
    parse_since,
    phase_share,
    read_import_history,
    read_run_timings,
    record_run_timing,
    sync_seconds_worth_recording,
    run_timings_path,
    summarise_run_timings,
)


class FakeLimbleClient:
    """Stands in for LimbleClient: paginates from memory, no HTTP."""

    def __init__(self, tasks, assets, *, page_size=2, gate=None, error=None, delay=0.0):
        self.tasks = tasks
        self.assets = assets
        self.page_size = page_size
        self.gate = gate
        self.error = error
        self.delay = delay
        self.seen_updated_since = None
        self.instructions = {}

    def get_task_instructions(self, task_id):
        return self.instructions.get(str(task_id), [])

    def get_tasks(self, updated_since=None, *, on_page=None):
        self.seen_updated_since = updated_since
        if self.error is not None:
            raise self.error
        if self.gate is not None:
            # Hold the sync inside its first phase so a test can inspect a
            # genuinely in-flight job.
            self.gate.wait(timeout=10)
        if self.delay:
            time.sleep(self.delay)
        return self._paginate(self.tasks, on_page)

    def get_assets(self, *, on_page=None):
        return self._paginate(self.assets, on_page)

    def _paginate(self, items, on_page):
        fetched = 0
        for page, start in enumerate(range(0, len(items), self.page_size), start=1):
            fetched += len(items[start : start + self.page_size])
            if on_page is not None:
                on_page(fetched, page)
        return list(items)


def _fake_config():
    return sync_service.LimbleConfig(client_id="id", client_secret="secret", base_url="https://example.test/v2")


def _wait_for(predicate, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


class ParseSinceTests(unittest.TestCase):
    def test_accepts_dates_datetimes_and_timestamps(self):
        self.assertIsNone(parse_since(None))
        self.assertIsNone(parse_since("   "))
        self.assertEqual(parse_since("1700000000"), 1700000000)
        self.assertEqual(parse_since("2026-01-01"), parse_since("2026-01-01T00:00:00Z"))

    def test_rejects_nonsense_with_a_message_the_page_can_show(self):
        with self.assertRaises(SyncOptionError) as caught:
            parse_since("last tuesday")
        self.assertIn("last tuesday", str(caught.exception))


class DotenvTests(unittest.TestCase):
    """The loader promises that rotating .env takes effect without a restart, so
    a forced reload has to replace what an earlier pass put in the environment."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.env_file = Path(self._tmp.name) / ".env"
        self.fallback_file = Path(self._tmp.name) / "fallback.env"

        # Point the loader at temporary files instead of the real working
        # directory and repo checkout.
        self.candidates = [self.env_file, self.fallback_file]
        files = mock.patch.object(sync_service, "_dotenv_candidates", lambda: list(self.candidates))
        files.start()
        self.addCleanup(files.stop)
        environ = mock.patch.dict(os.environ, {}, clear=False)
        environ.start()
        self.addCleanup(environ.stop)
        # The module-level cache is process state; reset it around each test.
        self.addCleanup(sync_service._dotenv_loaded_scopes.clear)
        self.addCleanup(sync_service._dotenv_applied.clear)
        sync_service._dotenv_loaded_scopes.clear()
        sync_service._dotenv_applied.clear()
        for key in ("LIMBLE_CLIENT_ID", "LIMBLE_CLIENT_SECRET", "GREMLIN_DB_PATH", "GREMLIN_ADMIN_PIN", "GREMLIN_SECRET_KEY"):
            os.environ.pop(key, None)

    def test_a_rotated_secret_is_picked_up_without_a_restart(self):
        self.env_file.write_text("LIMBLE_CLIENT_SECRET=old-secret\n", encoding="utf-8")
        sync_service.load_dotenv_files()
        self.assertEqual(os.environ["LIMBLE_CLIENT_SECRET"], "old-secret")

        self.env_file.write_text("LIMBLE_CLIENT_SECRET=new-secret\n", encoding="utf-8")
        # An unforced reload is the polling path: cached, so it changes nothing.
        sync_service.load_dotenv_files()
        self.assertEqual(os.environ["LIMBLE_CLIENT_SECRET"], "old-secret")

        sync_service.load_dotenv_files(force=True)
        self.assertEqual(os.environ["LIMBLE_CLIENT_SECRET"], "new-secret")

    def test_a_real_environment_variable_still_beats_the_file(self):
        os.environ["LIMBLE_CLIENT_SECRET"] = "set-by-the-operator"
        self.env_file.write_text("LIMBLE_CLIENT_SECRET=from-file\n", encoding="utf-8")
        sync_service.load_dotenv_files()
        sync_service.load_dotenv_files(force=True)
        self.assertEqual(os.environ["LIMBLE_CLIENT_SECRET"], "set-by-the-operator")

    def test_the_first_file_wins_even_on_a_forced_reload(self):
        # The working directory's .env is the deployment's own configuration; a
        # repo checkout's copy is only a fallback. Letting the fallback win on
        # the forced path -- which the nightly job uses -- would point a sync at
        # a different database or Limble account.
        self.env_file.write_text("GREMLIN_DB_PATH=/deployment/GREMLIN.db\n", encoding="utf-8")
        self.fallback_file.write_text("GREMLIN_DB_PATH=/checkout/GREMLIN.db\n", encoding="utf-8")

        sync_service.load_dotenv_files()
        self.assertEqual(os.environ["GREMLIN_DB_PATH"], "/deployment/GREMLIN.db")
        sync_service.load_dotenv_files(force=True)
        self.assertEqual(os.environ["GREMLIN_DB_PATH"], "/deployment/GREMLIN.db")

    def test_the_fallback_file_still_supplies_keys_the_first_omits(self):
        self.env_file.write_text("LIMBLE_CLIENT_ID=from-deployment\n", encoding="utf-8")
        self.fallback_file.write_text(
            "LIMBLE_CLIENT_ID=from-checkout\nLIMBLE_CLIENT_SECRET=from-checkout\n", encoding="utf-8"
        )
        sync_service.load_dotenv_files(force=True)
        self.assertEqual(os.environ["LIMBLE_CLIENT_ID"], "from-deployment")
        self.assertEqual(os.environ["LIMBLE_CLIENT_SECRET"], "from-checkout")

    def test_the_dashboard_reads_credentials_without_re_pointing_the_app(self):
        # A .env is whole-application configuration. The app consults
        # GREMLIN_DB_PATH on every request but builds LifeDataService once, so a
        # status poll that imported one could leave the sync writing to a
        # different database than the analysis pages are reading.
        self.env_file.write_text(
            "GREMLIN_DB_PATH=/from/dotenv/GREMLIN.db\n"
            "LIMBLE_CLIENT_ID=from-dotenv\n"
            "LIMBLE_CLIENT_SECRET=from-dotenv\n",
            encoding="utf-8",
        )
        described = sync_service.describe_credentials()
        self.assertTrue(described["configured"])
        self.assertNotIn("GREMLIN_DB_PATH", os.environ)

    def test_the_scheduled_job_still_gets_the_whole_file(self):
        # The CLI is a fresh process that has built nothing yet, and pointing it
        # with GREMLIN_DB_PATH in .env is how the scheduled job is configured.
        self.env_file.write_text("GREMLIN_DB_PATH=/from/dotenv/GREMLIN.db\n", encoding="utf-8")
        sync_service.load_dotenv_files(force=True)
        self.assertEqual(os.environ["GREMLIN_DB_PATH"], "/from/dotenv/GREMLIN.db")

    def test_a_restricted_load_does_not_satisfy_a_later_full_one(self):
        self.env_file.write_text(
            "GREMLIN_DB_PATH=/from/dotenv/GREMLIN.db\nLIMBLE_CLIENT_ID=from-dotenv\n", encoding="utf-8"
        )
        sync_service.load_dotenv_files(only_prefix=sync_service.LIMBLE_ENV_PREFIX)
        self.assertNotIn("GREMLIN_DB_PATH", os.environ)
        # Caching is per scope, so the CLI's unrestricted load still reads.
        sync_service.load_dotenv_files()
        self.assertEqual(os.environ["GREMLIN_DB_PATH"], "/from/dotenv/GREMLIN.db")

    def test_the_app_takes_its_own_settings_from_the_file(self):
        # The administrator bootstrap PIN lives in .env so an operator can set it without
        # touching Windows environment variables. It is read once at startup,
        # before anything could be holding a contradicting value.
        self.env_file.write_text(
            "GREMLIN_ADMIN_PIN=my-own-pin\nGREMLIN_SECRET_KEY=stable-key\n", encoding="utf-8"
        )
        sync_service.load_dotenv_files(only_keys=sync_service.APP_ENV_KEYS)
        self.assertEqual(os.environ["GREMLIN_ADMIN_PIN"], "my-own-pin")
        self.assertEqual(os.environ["GREMLIN_SECRET_KEY"], "stable-key")

    def test_the_app_scope_leaves_everything_else_in_the_file_alone(self):
        self.env_file.write_text(
            "GREMLIN_ADMIN_PIN=my-own-pin\nGREMLIN_DB_PATH=/from/dotenv/GREMLIN.db\n"
            "LIMBLE_CLIENT_ID=from-dotenv\n",
            encoding="utf-8",
        )
        sync_service.load_dotenv_files(only_keys=sync_service.APP_ENV_KEYS)
        self.assertEqual(os.environ["GREMLIN_ADMIN_PIN"], "my-own-pin")
        # The database path is the one setting a running app must not pick up
        # this way, and credentials have their own scope.
        self.assertNotIn("GREMLIN_DB_PATH", os.environ)
        self.assertNotIn("LIMBLE_CLIENT_ID", os.environ)

    def test_an_admin_pin_exported_by_the_host_beats_the_file(self):
        os.environ["GREMLIN_ADMIN_PIN"] = "set-by-the-host"
        self.env_file.write_text("GREMLIN_ADMIN_PIN=from-file\n", encoding="utf-8")
        sync_service.load_dotenv_files(only_keys=sync_service.APP_ENV_KEYS)
        self.assertEqual(os.environ["GREMLIN_ADMIN_PIN"], "set-by-the-host")

    def test_each_scope_reads_the_file_for_itself(self):
        self.env_file.write_text(
            "GREMLIN_ADMIN_PIN=my-own-pin\nLIMBLE_CLIENT_ID=from-dotenv\n", encoding="utf-8"
        )
        sync_service.load_dotenv_files(only_keys=sync_service.APP_ENV_KEYS)
        self.assertNotIn("LIMBLE_CLIENT_ID", os.environ)
        # The app-scoped load must not persuade the credential-scoped one that
        # the file has already been dealt with.
        sync_service.load_dotenv_files(only_prefix=sync_service.LIMBLE_ENV_PREFIX)
        self.assertEqual(os.environ["LIMBLE_CLIENT_ID"], "from-dotenv")
        self.assertEqual(os.environ["GREMLIN_ADMIN_PIN"], "my-own-pin")

    def test_credentials_added_to_a_running_app_are_noticed(self):
        # The page disables the Run button when credentials are missing, and
        # starting a sync is what forces a reload -- so a cache that outlived a
        # newly written .env would leave no way to recover but a restart.
        self.assertFalse(sync_service.describe_credentials()["configured"])

        self.env_file.write_text(
            "LIMBLE_CLIENT_ID=added-later\nLIMBLE_CLIENT_SECRET=added-later\n", encoding="utf-8"
        )
        described = sync_service.describe_credentials()
        self.assertTrue(described["configured"])
        self.assertIsNone(described["detail"])

    def test_a_credential_deleted_from_the_file_stops_being_used(self):
        # Removing a secret from .env is how it is withdrawn. A loader that only
        # adds and replaces would keep a revoked credential in use, and keep
        # reporting it as configured, until someone restarted the process.
        self.env_file.write_text(
            "LIMBLE_CLIENT_ID=doomed\nLIMBLE_CLIENT_SECRET=doomed\n", encoding="utf-8"
        )
        sync_service.load_dotenv_files()
        self.assertTrue(sync_service.describe_credentials()["configured"])

        self.env_file.write_text("LIMBLE_CLIENT_ID=doomed\n", encoding="utf-8")
        sync_service.load_dotenv_files(force=True)
        self.assertNotIn("LIMBLE_CLIENT_SECRET", os.environ)
        self.assertFalse(sync_service.describe_credentials()["configured"])

    def test_a_deletion_does_not_take_a_value_somebody_else_owns(self):
        self.env_file.write_text("LIMBLE_CLIENT_SECRET=from-file\n", encoding="utf-8")
        sync_service.load_dotenv_files()
        # Something else in the process took ownership of the variable.
        os.environ["LIMBLE_CLIENT_SECRET"] = "set-at-runtime"
        self.env_file.write_text("", encoding="utf-8")
        sync_service.load_dotenv_files(force=True)
        self.assertEqual(os.environ["LIMBLE_CLIENT_SECRET"], "set-at-runtime")

    def test_a_deletion_stays_inside_the_scope_that_was_asked_for(self):
        self.env_file.write_text(
            "GREMLIN_DB_PATH=/from/dotenv/GREMLIN.db\nLIMBLE_CLIENT_ID=from-dotenv\n", encoding="utf-8"
        )
        sync_service.load_dotenv_files()
        self.env_file.write_text("", encoding="utf-8")
        # The dashboard's credential-scoped reload must not evict the database
        # path the CLI's unrestricted load applied.
        sync_service.load_dotenv_files(force=True, only_prefix=sync_service.LIMBLE_ENV_PREFIX)
        self.assertNotIn("LIMBLE_CLIENT_ID", os.environ)
        self.assertEqual(os.environ["GREMLIN_DB_PATH"], "/from/dotenv/GREMLIN.db")

    def test_a_value_changed_outside_the_loader_is_left_alone(self):
        self.env_file.write_text("LIMBLE_CLIENT_SECRET=from-file\n", encoding="utf-8")
        sync_service.load_dotenv_files()
        # Something else in the process now owns this variable.
        os.environ["LIMBLE_CLIENT_SECRET"] = "set-at-runtime"
        self.env_file.write_text("LIMBLE_CLIENT_SECRET=rotated\n", encoding="utf-8")
        sync_service.load_dotenv_files(force=True)
        self.assertEqual(os.environ["LIMBLE_CLIENT_SECRET"], "set-at-runtime")


class SyncOptionsTests(unittest.TestCase):
    def test_instructions_are_read_by_default_bounded_and_can_be_turned_off(self):
        # On by default because the cost is the first walk through the history,
        # not the nightly run: a task records that it was read and is not read
        # again, so a sync left to itself keeps the narrative current. Bounded
        # by default because that first walk would otherwise take days.
        default = SyncOptions.from_payload({})
        self.assertTrue(default.fetch_instructions)
        self.assertEqual(default.instructions_limit, DEFAULT_INSTRUCTIONS_LIMIT)

        capped = SyncOptions.from_payload({"instructions_limit": "250"})
        self.assertEqual(capped.instructions_limit, 250)

        off = SyncOptions.from_payload({"fetch_instructions": False})
        self.assertFalse(off.fetch_instructions)

    def test_a_bad_instruction_cap_is_refused_while_the_caller_is_still_here(self):
        for bad in ("many", -1, 1.5):
            with self.subTest(limit=bad):
                with self.assertRaises(SyncOptionError):
                    SyncOptions.from_payload({"instructions_limit": bad})

    def test_the_opt_in_phase_does_not_shrink_an_ordinary_syncs_share(self):
        # An ordinary sync does everything a sync normally does, so it is a whole
        # one -- adding an opt-in phase to the roster must not make it report
        # otherwise, or every full-run estimate extrapolated from it is wrong.
        self.assertEqual(phase_share(SyncOptions()), 1.0)
        # Reading instructions or not makes no difference to the share: that
        # phase is paced by the API's rate limit rather than by the size of the
        # sync, and the timing recorded alongside subtracts its duration for the
        # same reason.
        self.assertEqual(phase_share(SyncOptions(fetch_instructions=False)), 1.0)
        # A run that skips a real phase still reports less than a whole sync --
        # including when it read instructions, which must not paper over the gap.
        self.assertLess(phase_share(SyncOptions(fetch_assets=False)), 1.0)

    def test_payload_flags_are_inverted_into_positive_options(self):
        options = SyncOptions.from_payload({"dry_run": True, "no_assets": True, "no_map": True, "since": " 2026-01-01 "})
        self.assertTrue(options.dry_run)
        self.assertFalse(options.fetch_assets)
        self.assertFalse(options.refresh_mapping)
        self.assertEqual(options.since, "2026-01-01")

    def test_defaults_match_the_nightly_job(self):
        options = SyncOptions.from_payload({})
        self.assertFalse(options.dry_run)
        self.assertTrue(options.fetch_assets)
        self.assertTrue(options.refresh_mapping)
        self.assertFalse(options.include_templates)
        self.assertIsNone(options.since)

    def test_a_bad_date_is_rejected_while_the_caller_is_still_listening(self):
        with self.assertRaises(SyncOptionError):
            SyncOptions.from_payload({"since": "not-a-date"})

    def test_a_flag_that_is_not_a_boolean_is_a_bad_request(self):
        # The dangerous reading of a typo is the quiet one: "treu" is not a
        # request for a writing sync, it is somebody asking for a preview.
        for payload in ({"dry_run": "treu"}, {"dry_run": 1}, {"no_assets": []}, {"no_map": "maybe"}):
            with self.subTest(payload=payload):
                with self.assertRaises(SyncOptionError) as caught:
                    SyncOptions.from_payload(payload)
                self.assertIn(next(iter(payload)), str(caught.exception))

    def test_an_option_name_that_is_not_understood_is_a_bad_request(self):
        # Dropping it decides the request the dangerous way: {"dryrun": true} is
        # somebody trying to avoid a writing sync, not asking for one.
        with self.assertRaises(SyncOptionError) as caught:
            SyncOptions.from_payload({"dryrun": True})
        self.assertIn("dryrun", str(caught.exception))

        with self.assertRaises(SyncOptionError) as caught:
            SyncOptions.from_payload({"dry_run": True, "no_asset": True})
        self.assertIn("no_asset", str(caught.exception))
        # The message names what would have been accepted.
        self.assertIn("no_assets", str(caught.exception))

    def test_every_documented_option_is_accepted(self):
        options = SyncOptions.from_payload(
            {"dry_run": True, "no_assets": True, "no_map": True, "include_templates": True, "since": "2026-01-01"}
        )
        self.assertTrue(options.dry_run)
        self.assertFalse(options.fetch_assets)
        self.assertFalse(options.refresh_mapping)
        self.assertTrue(options.include_templates)
        self.assertEqual(options.since, "2026-01-01")

    def test_the_spellings_a_person_would_type_are_understood(self):
        self.assertTrue(SyncOptions.from_payload({"dry_run": True}).dry_run)
        self.assertTrue(SyncOptions.from_payload({"dry_run": "true"}).dry_run)
        self.assertTrue(SyncOptions.from_payload({"dry_run": " Yes "}).dry_run)
        self.assertFalse(SyncOptions.from_payload({"dry_run": "false"}).dry_run)
        self.assertFalse(SyncOptions.from_payload({"dry_run": None}).dry_run)
        self.assertFalse(SyncOptions.from_payload({}).dry_run)

    def test_skipped_phases_drop_out_of_the_plan(self):
        full = [phase.key for phase in SyncOptions().active_phases()]
        self.assertEqual(
            full,
            [PHASE_TASKS, PHASE_ASSETS, PHASE_INSTRUCTIONS, PHASE_TRANSFORM, PHASE_WRITE, PHASE_MAP],
        )
        without = [phase.key for phase in SyncOptions(fetch_instructions=False).active_phases()]
        self.assertEqual(without, [PHASE_TASKS, PHASE_ASSETS, PHASE_TRANSFORM, PHASE_WRITE, PHASE_MAP])

        dry = [phase.key for phase in SyncOptions(dry_run=True, fetch_instructions=False).active_phases()]
        # A dry run writes nothing, so it cannot map anything either.
        self.assertEqual(dry, [PHASE_TASKS, PHASE_ASSETS, PHASE_TRANSFORM])

        lean = [
            phase.key for phase in
            SyncOptions(fetch_assets=False, refresh_mapping=False, fetch_instructions=False).active_phases()
        ]
        self.assertEqual(lean, [PHASE_TASKS, PHASE_TRANSFORM, PHASE_WRITE])


class ImportHistoryTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db_path = Path(self._tmp.name) / "gremlin.db"
        RawRepository(self.db_path).ensure_schema()

    def _insert(self, status, started, completed, rows=None):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT INTO import_batch (source_system, status, import_started_at, import_completed_at, raw_row_count)"
                " VALUES ('Limble', ?, ?, ?, ?)",
                (status, started, completed, rows),
            )

    def test_missing_database_reports_no_history_rather_than_failing(self):
        history = read_import_history(Path(self._tmp.name) / "nope.db")
        self.assertFalse(history["available"])
        self.assertIsNone(history["last_completed_at"])

    def test_the_newest_completed_batch_describes_the_database(self):
        self._insert("COMPLETED", "2026-08-01 02:00:00", "2026-08-01 02:01:00", 10)
        self._insert("FAILED", "2026-08-04 02:00:00", "2026-08-04 02:30:00", 0)
        self._insert("COMPLETED", "2026-08-05 02:00:00", "2026-08-05 02:01:30", 30)

        history = read_import_history(self.db_path)
        self.assertEqual(history["last_completed_batch_id"], 3)
        self.assertEqual(history["last_row_count"], 30)
        self.assertEqual(history["last_completed_at"], "2026-08-05 02:01:30")
        # A batch row opens only once fetching is done, so it says nothing about
        # how long a sync takes and must not pretend to.
        self.assertNotIn("median_seconds", history)

    def test_latest_finished_batch_includes_a_failed_post_write_import(self):
        self._insert("COMPLETED", "2026-08-05 02:00:00", "2026-08-05 02:01:00", 10)
        self._insert("FAILED", "2026-08-05 03:00:00", "2026-08-05 03:01:00", 12)

        history = read_import_history(self.db_path)

        # Success reporting remains anchored to the completed import, while the
        # cache generation publishes the later terminal write attempt.
        self.assertEqual(history["last_completed_batch_id"], 1)
        self.assertEqual(history["last_finished_batch_id"], 2)
        self.assertEqual(history["last_finished_at"], "2026-08-05 03:01:00")
        self.assertEqual(history["last_finished_status"], "FAILED")

    def test_batch_id_distinguishes_completions_in_the_same_second(self):
        completed = "2026-08-05 02:01:30"
        self._insert("COMPLETED", "2026-08-05 02:00:00", completed, 10)
        self._insert("COMPLETED", "2026-08-05 02:00:30", completed, 20)

        history = read_import_history(self.db_path)

        self.assertEqual(history["last_completed_batch_id"], 2)
        self.assertEqual(history["last_completed_at"], completed)
        self.assertEqual(history["last_row_count"], 20)

    def test_completion_time_wins_when_batches_finish_out_of_order(self):
        self._insert("COMPLETED", "2026-08-05 02:00:00", "2026-08-05 02:03:00", 10)
        self._insert("COMPLETED", "2026-08-05 02:00:30", "2026-08-05 02:02:00", 20)

        history = read_import_history(self.db_path)

        self.assertEqual(history["last_completed_batch_id"], 1)
        self.assertEqual(history["last_completed_at"], "2026-08-05 02:03:00")
        self.assertEqual(history["last_row_count"], 10)

    def test_fractional_completion_time_wins_within_the_same_second(self):
        self._insert("COMPLETED", "2026-08-05 02:00:00", "2026-08-05 02:01:30.900000", 10)
        self._insert("COMPLETED", "2026-08-05 02:00:30", "2026-08-05 02:01:30.100000", 20)

        history = read_import_history(self.db_path)

        # Batch 1 started first but finished last. Fractional timestamps ensure
        # its later mapping commit becomes the cache generation instead of
        # allowing the higher start-assigned ID to win a whole-second tie.
        self.assertEqual(history["last_completed_batch_id"], 1)
        self.assertEqual(history["last_finished_batch_id"], 1)
        self.assertEqual(history["last_completed_at"], "2026-08-05 02:01:30.900000")

    def test_an_open_batch_is_reported_as_in_flight(self):
        started = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(time.time() - 120))
        self._insert("STARTED", started, None, None)
        in_flight = read_import_history(self.db_path)["in_flight"]
        self.assertIsNotNone(in_flight)
        self.assertEqual(in_flight["status"], "STARTED")
        self.assertGreater(in_flight["age_seconds"], 60)

    def _legacy_database(self, rows):
        """A legacy import_batch with no status column, which the writer still supports."""

        path = Path(self._tmp.name) / "legacy.db"
        path.unlink(missing_ok=True)
        with sqlite3.connect(path) as conn:
            conn.execute(
                "CREATE TABLE import_batch (import_batch_id INTEGER PRIMARY KEY, "
                "import_started_at TEXT, import_completed_at TEXT, raw_row_count INTEGER)"
            )
            conn.executemany(
                "INSERT INTO import_batch (import_started_at, import_completed_at, raw_row_count)"
                " VALUES (?, ?, ?)",
                rows,
            )
        return path

    def test_a_completed_legacy_batch_is_not_mistaken_for_one_in_progress(self):
        # Without a status column there is nothing to read but the timestamps.
        # Treating a missing status as "unfinished" would warn about a sync in
        # progress after every import, and report no last import at all.
        started = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(time.time() - 300))
        completed = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(time.time() - 240))
        history = read_import_history(self._legacy_database([(started, completed, 42)]))

        self.assertIsNone(history["in_flight"])
        self.assertEqual(history["last_completed_batch_id"], 1)
        self.assertEqual(history["last_completed_at"], completed)
        self.assertEqual(history["last_row_count"], 42)

    def test_an_open_legacy_batch_is_still_reported_in_flight(self):
        started = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(time.time() - 120))
        history = read_import_history(self._legacy_database([(started, None, None)]))
        self.assertIsNotNone(history["in_flight"])
        self.assertIsNone(history["in_flight"]["status"])

    def test_a_batch_left_open_by_a_crash_is_not_reported_forever(self):
        self._insert("STARTED", "2019-01-01 02:00:00", None, None)
        self.assertIsNone(read_import_history(self.db_path)["in_flight"])


class SyncRunnerTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "gremlin.db"
        RawRepository(self.db_path).ensure_schema()
        self.runner = LimbleSyncRunner()
        self.tasks = [{"taskID": index, "assetID": 7, "name": f"task {index}"} for index in range(1, 8)]
        self.assets = [{"assetID": 7, "name": "Pump"}]

        # The patches have to outlive the call that starts a sync: the worker
        # thread builds its client a moment later, and an un-patched one would
        # try to reach the real Limble API.
        self._client = None
        # The worker builds its client a moment after start() returns, so the
        # field is read on the worker's clock, not the test's. Signalling when
        # it has been read lets _run wait for that hand-off before a later start
        # is allowed to replace it.
        self._client_taken = threading.Event()

        def _take_client(config, log=print):
            client = self._client
            self._client_taken.set()
            return client

        for target, attribute, replacement in (
            (sync_service, "LimbleClient", _take_client),
            (sync_service.LimbleConfig, "from_env", staticmethod(_fake_config)),
        ):
            patcher = mock.patch.object(target, attribute, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)

        # Release any gate and let the worker finish before the temporary
        # directory it is writing into disappears.
        self._gates = []
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(self._release_gates)

    def _release_gates(self):
        for gate in self._gates:
            gate.set()
        _wait_for(lambda: self.runner.status()["state"] != STATE_RUNNING)

    def _run(self, client, options=None, wait=True):
        """Start a sync against the fake client and (by default) wait for it."""

        options = options if options is not None else SyncOptions(refresh_mapping=False)
        self._client_taken.clear()
        self._client = client
        if client.gate is not None:
            self._gates.append(client.gate)
        status = self.runner.start(self.db_path, options, log=lambda _message: None)
        # Do not return while this client is still unclaimed. A second start that
        # overwrote the field first would hand its own client to the worker
        # already running -- an ungated one finishes immediately, and a test
        # about refusing concurrent syncs would then watch the first one end.
        self.assertTrue(self._client_taken.wait(timeout=10), "the worker never took its client")
        if wait:
            self.assertTrue(_wait_for(lambda: self.runner.status()["state"] != STATE_RUNNING), "sync never finished")
        return status

    def test_starts_idle(self):
        self.assertEqual(self.runner.status()["state"], STATE_IDLE)

    def test_a_successful_run_reports_a_summary_and_a_full_bar(self):
        self._run(FakeLimbleClient(self.tasks, self.assets))
        status = self.runner.status()

        self.assertEqual(status["state"], STATE_SUCCEEDED)
        self.assertIsNone(status["error"])
        self.assertEqual(status["progress"], 1.0)
        self.assertEqual(status["summary"]["fetched_tasks"], 7)
        self.assertEqual(status["summary"]["inserted"], 7)
        self.assertEqual(status["summary"]["fetched_assets"], 1)
        self.assertIsNotNone(status["finished_at"])
        # The rows really landed, rather than the job merely claiming so.
        self.assertEqual(RawRepository(self.db_path).raw_record_count(), 7)

    def test_status_is_json_safe(self):
        self._run(FakeLimbleClient(self.tasks, self.assets))
        import json

        json.dumps(self.runner.status())
        json.dumps(self.runner.describe(self.db_path))

    def test_progress_advances_through_the_phases_without_going_backwards(self):
        seen = []
        original = LimbleSyncRunner._on_progress

        def recording(runner_self, phase, current, total):
            original(runner_self, phase, current, total)
            seen.append((phase, runner_self.status()["progress"]))

        with mock.patch.object(LimbleSyncRunner, "_on_progress", recording):
            self._run(FakeLimbleClient(self.tasks, self.assets))

        phases = [phase for phase, _fraction in seen]
        self.assertEqual(phases[0], PHASE_TASKS)
        self.assertIn(PHASE_ASSETS, phases)
        self.assertIn(PHASE_WRITE, phases)
        fractions = [fraction for _phase, fraction in seen]
        self.assertEqual(fractions, sorted(fractions), "the bar moved backwards")
        self.assertTrue(all(0.0 <= fraction <= 1.0 for fraction in fractions))

    def test_a_fetch_phase_is_interpolated_from_the_previous_run(self):
        self._run(FakeLimbleClient(self.tasks, self.assets))
        # The second run knows what the first one saw, so its fetch phase can
        # report "3 of ~7" instead of an unbounded count.
        gate = threading.Event()
        self._run(FakeLimbleClient(self.tasks, self.assets, gate=gate), wait=False)
        try:
            status = self.runner.status()
            self.assertEqual(status["state"], STATE_RUNNING)
            self.assertEqual(status["expected"][PHASE_TASKS], 7)
        finally:
            gate.set()
            self.assertTrue(_wait_for(lambda: self.runner.status()["state"] != STATE_RUNNING))

    def test_only_one_sync_runs_at_a_time(self):
        gate = threading.Event()
        self._run(FakeLimbleClient(self.tasks, self.assets, gate=gate), wait=False)
        try:
            self.assertTrue(_wait_for(lambda: self.runner.status()["state"] == STATE_RUNNING))
            with self.assertRaises(SyncAlreadyRunningError):
                self._run(FakeLimbleClient(self.tasks, self.assets), wait=False)
        finally:
            gate.set()
            self.assertTrue(_wait_for(lambda: self.runner.status()["state"] != STATE_RUNNING))
        self.assertEqual(self.runner.status()["state"], STATE_SUCCEEDED)

    def test_a_recorded_worker_is_always_a_started_one(self):
        # The takeover path below is only safe because start() launches the
        # worker before releasing the lock: a recorded-but-unstarted thread is
        # not alive, so a request arriving in that window would read a healthy
        # job as abandoned and run a second sync alongside it.
        gate = threading.Event()
        self._run(FakeLimbleClient(self.tasks, self.assets, gate=gate), wait=False)
        try:
            self.assertTrue(self.runner._thread.is_alive())
            with self.assertRaises(SyncAlreadyRunningError):
                self.runner.start(self.db_path, SyncOptions(), log=lambda _message: None)
        finally:
            gate.set()
            self.assertTrue(_wait_for(lambda: self.runner.status()["state"] != STATE_RUNNING))

    def test_a_job_whose_worker_died_does_not_block_the_next_sync(self):
        # Only an interpreter-level failure gets past the worker's own except
        # clause, but if one does, the runner must not refuse every later sync
        # until somebody restarts the app.
        dead = threading.Thread(target=lambda: None)
        dead.start()
        dead.join()
        self.runner._job = {"state": STATE_RUNNING}
        self.runner._thread = dead

        self._run(FakeLimbleClient(self.tasks, self.assets))
        self.assertEqual(self.runner.status()["state"], STATE_SUCCEEDED)

    def test_a_failed_fetch_is_reported_instead_of_disappearing(self):
        self._run(FakeLimbleClient(self.tasks, self.assets, error=RuntimeError("Limble said no")))
        status = self.runner.status()
        self.assertEqual(status["state"], STATE_FAILED)
        self.assertIn("Limble said no", status["error"])
        self.assertTrue(any("Limble said no" in message["text"] for message in status["messages"]))
        # A failed sync must not leave the panel claiming the data arrived.
        self.assertIsNone(status["summary"])
        self.assertLess(status["progress"], 1.0)

    def test_a_dry_run_writes_nothing(self):
        self._run(FakeLimbleClient(self.tasks, self.assets), options=SyncOptions(dry_run=True))
        status = self.runner.status()
        self.assertEqual(status["state"], STATE_SUCCEEDED)
        self.assertTrue(status["summary"]["dry_run"])
        self.assertEqual(RawRepository(self.db_path).raw_record_count(), 0)

    def test_import_is_not_completed_until_mapping_finishes(self):
        mapping_started = threading.Event()
        release_mapping = threading.Event()

        def blocking_refresh(_service):
            mapping_started.set()
            release_mapping.wait(timeout=10)
            return 7

        with mock.patch(
            "services.life_data_service.LifeDataService.refresh_mapped_cmms_records",
            blocking_refresh,
        ):
            self._run(
                FakeLimbleClient(self.tasks, self.assets),
                options=SyncOptions(refresh_mapping=True),
                wait=False,
            )
            try:
                self.assertTrue(mapping_started.wait(timeout=10), "sync never reached mapping")
                history = read_import_history(self.db_path)
                self.assertIsNone(history["last_completed_at"])
                self.assertIsNotNone(history["in_flight"])
                self.assertEqual(self.runner.status()["state"], STATE_RUNNING)
            finally:
                release_mapping.set()

            self.assertTrue(_wait_for(lambda: self.runner.status()["state"] != STATE_RUNNING))
            self.assertEqual(self.runner.status()["state"], STATE_SUCCEEDED)
            self.assertIsNotNone(read_import_history(self.db_path)["last_completed_at"])

    def test_mapping_setup_failure_closes_the_import_batch(self):
        with mock.patch(
            "services.ingestion_service.IngestionService._missing_mapping_columns",
            side_effect=RuntimeError("cannot inspect mapping schema"),
        ):
            self._run(
                FakeLimbleClient(self.tasks, self.assets),
                options=SyncOptions(refresh_mapping=True),
            )

        self.assertEqual(self.runner.status()["state"], STATE_FAILED)
        self.assertIn("cannot inspect mapping schema", self.runner.status()["error"])
        history = read_import_history(self.db_path)
        self.assertIsNone(history["in_flight"])
        with RawRepository(self.db_path).connect() as conn:
            batch = conn.execute(
                "SELECT status, raw_row_count FROM import_batch ORDER BY import_batch_id DESC LIMIT 1"
            ).fetchone()
        self.assertEqual(tuple(batch), ("FAILED", len(self.tasks)))

    def test_a_real_sync_refuses_a_missing_database_but_a_dry_run_does_not(self):
        missing = Path(self._tmp.name) / "not-there.db"
        with self.assertRaises(SyncOptionError) as caught:
            self.runner.start(missing, SyncOptions())
        # Refused before the fetch, not after minutes of downloading.
        self.assertIn(str(missing), str(caught.exception))
        self.assertEqual(self.runner.status()["state"], STATE_IDLE)

        self._client = FakeLimbleClient(self.tasks, self.assets)
        self.runner.start(missing, SyncOptions(dry_run=True), log=lambda _message: None)
        self.assertTrue(_wait_for(lambda: self.runner.status()["state"] != STATE_RUNNING))
        self.assertEqual(self.runner.status()["state"], STATE_SUCCEEDED)
        self.assertFalse(missing.exists())

    def test_the_since_filter_reaches_the_client(self):
        client = FakeLimbleClient(self.tasks, self.assets)
        self._run(client, options=SyncOptions(refresh_mapping=False, since="2026-01-01"))
        self.assertEqual(client.seen_updated_since, parse_since("2026-01-01"))

    def test_the_estimate_is_scaled_to_the_phases_this_run_will_actually_do(self):
        record_run_timing(self.db_path, seconds=100.0, share=1.0, counts={PHASE_TASKS: 500})

        gate = threading.Event()
        self._run(FakeLimbleClient(self.tasks, self.assets, gate=gate), options=SyncOptions(dry_run=True), wait=False)
        try:
            status = self.runner.status()
            # A dry run skips the write and mapping phases, so it should be
            # expected to take only the fetch/transform share of a full sync.
            self.assertLess(status["estimated_total_seconds"], 100)
            self.assertGreater(status["estimated_total_seconds"], 50)
            self.assertIn("timed sync", status["estimate_basis"])
            self.assertIsNotNone(status["estimated_remaining_seconds"])
            # And the count to interpolate against survives a restart, because
            # it was read back from the file rather than from memory.
            self.assertEqual(status["expected"][PHASE_TASKS], 500)
        finally:
            gate.set()
            self.assertTrue(_wait_for(lambda: self.runner.status()["state"] != STATE_RUNNING))

    def test_no_estimate_is_offered_before_any_sync_has_been_timed(self):
        gate = threading.Event()
        self._run(FakeLimbleClient(self.tasks, self.assets, gate=gate), wait=False)
        try:
            status = self.runner.status()
            self.assertIsNone(status["estimated_total_seconds"])
            self.assertIsNone(status["estimated_remaining_seconds"])
            self.assertIsNone(status["estimate_basis"])
        finally:
            gate.set()
            self.assertTrue(_wait_for(lambda: self.runner.status()["state"] != STATE_RUNNING))

    def test_a_completed_run_is_timed_for_the_next_one_to_learn_from(self):
        self._run(FakeLimbleClient(self.tasks, self.assets, delay=0.15))
        timings = read_run_timings(self.db_path)
        self.assertEqual(len(timings), 1)
        self.assertGreater(timings[0]["full_seconds"], 0)
        self.assertEqual(timings[0][PHASE_TASKS], 7)
        # Recorded beside the database it describes, not beside the code.
        self.assertEqual(run_timings_path(self.db_path).parent, self.db_path.parent)

        summary = summarise_run_timings(self.db_path)
        self.assertEqual(summary["runs"], 1)
        self.assertEqual(summary["counts"][PHASE_TASKS], 7)

    def test_nothing_is_left_to_do_once_the_status_says_succeeded(self):
        # A watcher that sees a terminal state is entitled to act on it -- start
        # another sync, or delete the database directory, as these tests do. Any
        # work published after that point races whatever it triggers.
        announced = []
        self.runner._on_success = lambda: announced.append(True)

        self._run(FakeLimbleClient(self.tasks, self.assets, delay=0.15))

        # Sampled the instant the status went terminal, not afterwards.
        self.assertEqual(self.runner.status()["state"], STATE_SUCCEEDED)
        self.assertEqual(len(read_run_timings(self.db_path)), 1)
        self.assertEqual(announced, [True])

    def test_a_dry_run_does_not_announce_a_change_that_did_not_happen(self):
        announced = []
        self.runner._on_success = lambda: announced.append(True)
        self._run(FakeLimbleClient(self.tasks, self.assets), options=SyncOptions(dry_run=True))
        self.assertEqual(self.runner.status()["state"], STATE_SUCCEEDED)
        self.assertEqual(announced, [])

    def test_a_broken_listener_does_not_spoil_a_good_sync(self):
        def explode():
            raise RuntimeError("cache refresh blew up")

        self.runner._on_success = explode
        self._run(FakeLimbleClient(self.tasks, self.assets))
        status = self.runner.status()
        self.assertEqual(status["state"], STATE_SUCCEEDED)
        self.assertEqual(RawRepository(self.db_path).raw_record_count(), 7)
        self.assertTrue(any("cache refresh blew up" in message["text"] for message in status["messages"]))

    def test_a_failed_run_is_not_timed(self):
        self._run(FakeLimbleClient(self.tasks, self.assets, error=RuntimeError("nope")))
        self.assertEqual(read_run_timings(self.db_path), [])

    def test_an_unwritable_timings_file_costs_an_estimate_and_nothing_else(self):
        run_timings_path(self.db_path).write_text("{ this is not json", encoding="utf-8")
        self.assertEqual(read_run_timings(self.db_path), [])
        self.assertIsNone(summarise_run_timings(self.db_path)["median_full_seconds"])

        with mock.patch.object(Path, "write_text", side_effect=OSError("read-only volume")):
            self._run(FakeLimbleClient(self.tasks, self.assets, delay=0.15))
        # The sync still succeeded; only the timing was lost.
        self.assertEqual(self.runner.status()["state"], STATE_SUCCEEDED)
        self.assertEqual(RawRepository(self.db_path).raw_record_count(), 7)

    def test_a_partial_run_is_stored_as_what_a_full_one_would_have_cost(self):
        # Two runs of different shapes must be comparable, so a dry run is
        # recorded normalised to a full sync rather than as its own short time.
        record_run_timing(self.db_path, seconds=63.0, share=0.63)
        record_run_timing(self.db_path, seconds=100.0, share=1.0)
        durations = [run["full_seconds"] for run in read_run_timings(self.db_path)]
        self.assertEqual(durations, [100.0, 100.0])
        self.assertEqual(summarise_run_timings(self.db_path)["median_full_seconds"], 100.0)

    def test_only_the_newest_timings_are_kept(self):
        for seconds in range(1, 9):
            record_run_timing(self.db_path, seconds=float(seconds), share=1.0)
        timings = read_run_timings(self.db_path)
        self.assertEqual(len(timings), 5)
        self.assertEqual([run["seconds"] for run in timings], [8.0, 7.0, 6.0, 5.0, 4.0])

    def test_describe_reports_credentials_without_leaking_them(self):
        with mock.patch.object(
            sync_service.LimbleConfig, "from_env", staticmethod(mock.Mock(side_effect=ValueError("credentials missing")))
        ):
            described = self.runner.describe(self.db_path)
        self.assertFalse(described["credentials"]["configured"])
        self.assertEqual(described["credentials"]["detail"], "credentials missing")
        self.assertTrue(described["db_exists"])

        with mock.patch.object(sync_service.LimbleConfig, "from_env", staticmethod(_fake_config)):
            described = self.runner.describe(self.db_path)
        self.assertTrue(described["credentials"]["configured"])
        self.assertEqual(described["credentials"]["base_url"], "https://example.test/v2")
        self.assertNotIn("secret", str(described))

    def test_describe_exposes_the_latest_finished_batch_for_cache_invalidation(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT INTO import_batch "
                "(source_system, status, import_started_at, import_completed_at, raw_row_count) "
                "VALUES ('Limble', 'FAILED', '2026-08-05 03:00:00', '2026-08-05 03:01:00', 12)"
            )
            batch_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

        described = self.runner.describe(self.db_path)

        self.assertEqual(described["history"]["last_finished_batch_id"], batch_id)

    def test_another_process_importing_is_reported_even_while_we_run(self):
        # The overlap worth warning about is precisely this one, and it used to
        # be hidden: any open batch was assumed to be ours whenever a local sync
        # was running, though a local run has no batch at all until its fetching
        # is done -- minutes in.
        started = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(time.time() - 60))
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT INTO import_batch (source_system, status, import_started_at) VALUES ('Limble', 'STARTED', ?)",
                (started,),
            )

        gate = threading.Event()
        self._run(FakeLimbleClient(self.tasks, self.assets, gate=gate), wait=False)
        try:
            self.assertTrue(_wait_for(lambda: self.runner.status()["state"] == STATE_RUNNING))
            described = self.runner.describe(self.db_path)
            self.assertEqual(described["job"]["state"], STATE_RUNNING)
            self.assertIsNotNone(described["in_flight_batch"])
        finally:
            gate.set()
            self.assertTrue(_wait_for(lambda: self.runner.status()["state"] != STATE_RUNNING))

    def test_our_own_batch_is_not_reported_as_somebody_elses(self):
        # Once this run opens its own batch, that row must not be read back as
        # a competing import.
        self._run(FakeLimbleClient(self.tasks, self.assets))
        batch_id = self.runner.status()["summary"]["import_batch_id"]
        with sqlite3.connect(self.db_path) as conn:
            # Reopen it, as it would look mid-write.
            conn.execute("UPDATE import_batch SET status = 'STARTED', import_completed_at = NULL WHERE import_batch_id = ?", (batch_id,))

        self.assertIsNotNone(read_import_history(self.db_path)["in_flight"])
        self.assertIsNone(read_import_history(self.db_path, exclude_batch_id=batch_id)["in_flight"])

    def test_an_open_batch_is_reported_when_nothing_runs_here(self):
        # An import batch left open by another process -- the nightly scheduled
        # job is the realistic case -- is worth warning about, because a second
        # sync would repeat its work.
        started = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(time.time() - 60))
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT INTO import_batch (source_system, status, import_started_at) VALUES ('Limble', 'STARTED', ?)",
                (started,),
            )
        self.assertIsNotNone(self.runner.describe(self.db_path)["in_flight_batch"])


class ScheduledJobTimingTests(unittest.TestCase):
    """The nightly CLI job is where most syncs happen, so it must contribute the
    timings the dashboard's estimate is built from."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db_path = Path(self._tmp.name) / "gremlin.db"
        RawRepository(self.db_path).ensure_schema()

    def test_a_scheduled_run_records_a_timing_the_dashboard_can_use(self):
        from jobs import sync_limble

        client = FakeLimbleClient([{"taskID": 1, "assetID": 7}], [{"assetID": 7, "name": "Pump"}], delay=0.15)
        with mock.patch.object(sync_limble, "LimbleClient", lambda config: client), mock.patch.object(
            sync_limble.LimbleConfig, "from_env", staticmethod(lambda **kwargs: _fake_config())
        ):
            args = sync_limble.build_arg_parser().parse_args(["--db", str(self.db_path), "--no-map"])
            summary = sync_limble.run(args)

        self.assertEqual(summary["inserted"], 1)
        timings = read_run_timings(self.db_path)
        self.assertEqual(len(timings), 1)
        self.assertEqual(timings[0][PHASE_TASKS], 1)
        # Recorded as what a full sync would cost, even though this one skipped
        # the mapping refresh, so it is comparable with a run from the page.
        self.assertLess(timings[0]["share"], 1.0)
        self.assertEqual(timings[0]["full_seconds"], round(timings[0]["seconds"] / timings[0]["share"], 1))


if __name__ == "__main__":
    unittest.main()


class InstructionLimitCliTests(unittest.TestCase):
    """The CLI cap is validated where the message can still reach a person."""

    def test_a_negative_cap_is_refused_rather_than_read_as_no_cap(self):
        from jobs.sync_limble import build_arg_parser

        parser = build_arg_parser()
        self.assertEqual(parser.parse_args(["--instructions-limit", "250"]).instructions_limit, 250)
        for bad in ("-1", "many", "1.5"):
            with self.subTest(value=bad):
                with self.assertRaises(SystemExit):
                    parser.parse_args(["--instructions-limit", bad])


class RecordedDurationTests(unittest.TestCase):
    """Both entry points take the instructions phase back out of the timing."""

    def test_the_instructions_phase_is_not_charged_to_an_ordinary_sync(self):
        # It is paced by the API's rate limit rather than by the size of the
        # sync, so one on-demand backfill would otherwise skew every ETA the
        # dashboard went on to show.
        self.assertEqual(sync_seconds_worth_recording(600.0, {"instructions_seconds": 540.0}), 60.0)

    def test_a_summary_without_the_phase_is_charged_in_full(self):
        for summary in (None, {}, {"instructions_seconds": None}, {"instructions_seconds": "nonsense"}):
            with self.subTest(summary=summary):
                self.assertEqual(sync_seconds_worth_recording(60.0, summary), 60.0)

    def test_a_duration_can_never_come_out_negative(self):
        self.assertEqual(sync_seconds_worth_recording(10.0, {"instructions_seconds": 99.0}), 0.0)
