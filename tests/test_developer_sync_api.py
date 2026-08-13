"""The developer dashboard's sync endpoints: who may start a sync, and what
the page is told about one that is already running."""

import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from repositories.raw_repo import RawRepository
from services import sync_service
from services.life_data_service import LifeDataService
from services.sync_service import STATE_RUNNING, STATE_SUCCEEDED, LimbleSyncRunner

from tests.test_sync_service import FakeLimbleClient, _fake_config, _wait_for

import app as app_module


class DeveloperSyncApiTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "gremlin.db"
        RawRepository(self.db_path).ensure_schema()

        env = mock.patch.dict(os.environ, {"GREMLIN_DB_PATH": str(self.db_path)})
        env.start()
        self.addCleanup(env.stop)

        # A runner per test, so one test's job cannot be another's surprise --
        # wired to the same success hook the app wires in production.
        self.runner = LimbleSyncRunner(on_success=app_module._on_sync_succeeded)
        runner_patch = mock.patch.object(app_module, "sync_runner", self.runner)
        runner_patch.start()
        self.addCleanup(runner_patch.stop)

        self._client = FakeLimbleClient([{"taskID": 1, "assetID": 7}], [{"assetID": 7, "name": "Pump"}])
        for target, attribute, replacement in (
            (sync_service, "LimbleClient", lambda config, log=print: self._client),
            (sync_service.LimbleConfig, "from_env", staticmethod(_fake_config)),
        ):
            patcher = mock.patch.object(target, attribute, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)

        existing = next((u for u in app_module.access_control.list_users() if u["username"] == "sync-test-admin"), None)
        if existing:
            app_module.access_control.save_user(existing["id"], "sync-test-admin", "test-pin", "admin")
        else:
            app_module.access_control.save_user(None, "sync-test-admin", "test-pin", "admin")

        app_module.app.config["TESTING"] = True
        self.client = app_module.app.test_client()

        self._gates = []
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(self._release)

    def _release(self):
        for gate in self._gates:
            gate.set()
        _wait_for(lambda: self.runner.status()["state"] != STATE_RUNNING)

    def _unlock(self):
        user = app_module.access_control.authenticate("sync-test-admin", "test-pin")
        with self.client.session_transaction() as session:
            session["user"] = user

    def _start(self, **body):
        return self.client.post("/developer/api/sync", json=body or {})

    # -- access ------------------------------------------------------------
    def test_an_admin_account_gates_both_reading_and_starting(self):
        self.assertEqual(self.client.get("/developer/api/sync").status_code, 403)
        self.assertEqual(self._start().status_code, 403)
        # Nothing ran on the way to being refused.
        self.assertEqual(self.runner.status()["state"], "idle")

    def test_a_start_must_be_json(self):
        self._unlock()
        # A cross-site form post arrives as form data; only JSON is accepted, so
        # a stray page cannot spend a developer's session on a database write.
        response = self.client.post("/developer/api/sync", data={"dry_run": "true"})
        self.assertEqual(response.status_code, 415)
        self.assertEqual(self.runner.status()["state"], "idle")

    def test_a_body_that_does_not_parse_is_refused_rather_than_defaulted(self):
        self._unlock()
        for body in ('{"dry_run": tr', "", "[1, 2]", '"just a string"'):
            with self.subTest(body=body):
                response = self.client.post(
                    "/developer/api/sync",
                    data=body,
                    content_type="application/json",
                )
                self.assertEqual(response.status_code, 400)
                self.assertIn("JSON object", response.get_json()["error"])
                # The dangerous reading of a truncated body is "no options
                # given", which would run a full writing sync.
                self.assertEqual(self.runner.status()["state"], "idle")

    def test_a_typo_in_dry_run_does_not_quietly_become_a_writing_sync(self):
        self._unlock()
        response = self._start(dry_run="treu")
        self.assertEqual(response.status_code, 400)
        self.assertIn("dry_run", response.get_json()["error"])
        self.assertEqual(self.runner.status()["state"], "idle")
        self.assertEqual(RawRepository(self.db_path).raw_record_count(), 0)

    def test_an_empty_object_is_a_valid_request_for_the_defaults(self):
        self._unlock()
        response = self.client.post("/developer/api/sync", json={})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(_wait_for(lambda: self.runner.status()["state"] != STATE_RUNNING))
        self.assertEqual(self.runner.status()["state"], STATE_SUCCEEDED)

    # -- starting ----------------------------------------------------------
    def test_a_start_returns_the_job_and_the_status_endpoint_follows_it(self):
        self._unlock()
        response = self._start(dry_run=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn(response.get_json()["state"], {STATE_RUNNING, STATE_SUCCEEDED})

        self.assertTrue(_wait_for(lambda: self.runner.status()["state"] != STATE_RUNNING))
        payload = self.client.get("/developer/api/sync").get_json()
        self.assertEqual(payload["job"]["state"], STATE_SUCCEEDED)
        self.assertTrue(payload["job"]["summary"]["dry_run"])
        self.assertEqual(payload["db_path"], str(self.db_path))
        self.assertTrue(payload["db_exists"])
        self.assertTrue(payload["credentials"]["configured"])

    def test_a_bad_date_is_a_bad_request_not_a_failed_job(self):
        self._unlock()
        response = self._start(since="whenever")
        self.assertEqual(response.status_code, 400)
        self.assertIn("whenever", response.get_json()["error"])
        self.assertEqual(self.runner.status()["state"], "idle")

    def test_a_second_start_is_refused_while_one_is_running(self):
        self._unlock()
        gate = threading.Event()
        self._gates.append(gate)
        self._client = FakeLimbleClient([{"taskID": 1}], [], gate=gate)

        self.assertEqual(self._start().status_code, 200)
        self.assertTrue(_wait_for(lambda: self.runner.status()["state"] == STATE_RUNNING))

        conflict = self._start()
        self.assertEqual(conflict.status_code, 409)
        self.assertIn("already running", conflict.get_json()["error"])

        gate.set()
        self.assertTrue(_wait_for(lambda: self.runner.status()["state"] != STATE_RUNNING))

    def test_a_sync_drops_the_asset_list_the_app_had_cached(self):
        # The sync maps through a LifeDataService of its own, so the app's
        # instance never learns the mapped table grew. Cached, that means a sync
        # reporting hundreds of mapped records leaves the Life Data page still
        # insisting the new assets do not exist.
        service = LifeDataService(self.db_path, refresh_on_startup=False)
        service.asset_number_options()
        self.assertIsNotNone(service._asset_number_options_cache)

        with mock.patch.object(app_module, "_life_data_service", service):
            self._unlock()
            self.assertEqual(self._start().status_code, 200)
            self.assertTrue(_wait_for(lambda: self.runner.status()["state"] != STATE_RUNNING))

        self.assertEqual(self.runner.status()["state"], STATE_SUCCEEDED)
        self.assertIsNone(service._asset_number_options_cache)
        # And the asset the sync imported is now reachable through the app's own
        # service, which is the outcome that actually matters.
        self.assertIn("7", service.asset_numbers())

    def test_a_demoted_admin_cannot_start_a_sync(self):
        self._unlock()
        user = app_module.access_control.authenticate("sync-test-admin", "test-pin")
        # Add another admin so demoting this test account cannot violate the
        # production last-administrator invariant.
        if not any(u["username"] == "sync-test-backup" for u in app_module.access_control.list_users()):
            app_module.access_control.save_user(None, "sync-test-backup", "backup-pin", "admin")
        app_module.access_control.save_user(user["id"], "sync-test-admin", "", "editor")
        response = self._start()
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.runner.status()["state"], "idle")

    def test_a_missing_database_is_refused_with_a_reason(self):
        self._unlock()
        with mock.patch.dict(os.environ, {"GREMLIN_DB_PATH": str(Path(self._tmp.name) / "gone.db")}):
            response = self._start()
        self.assertEqual(response.status_code, 400)
        self.assertIn("gone.db", response.get_json()["error"])


if __name__ == "__main__":
    unittest.main()
