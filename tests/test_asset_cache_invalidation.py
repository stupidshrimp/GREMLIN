"""The app's asset list versus a sync writing underneath it.

A sync maps through a LifeDataService of its own, so the instance the web app
holds is told about the change from outside -- possibly while it is halfway
through rebuilding the very list that change invalidates.
"""

import tempfile
import threading
import unittest
from itertools import count
from pathlib import Path

from repositories.raw_repo import RawRepository
from services.life_data_service import LifeDataService


class _PausingCursor:
    """Hands the rows over, then lets something else happen before the caller
    can act on them."""

    def __init__(self, inner, pause):
        self._inner = inner
        self._pause = pause

    def fetchall(self):
        rows = self._inner.fetchall()
        self._pause()
        return rows

    def __getattr__(self, name):
        return getattr(self._inner, name)


class _PausingConnection:
    def __init__(self, inner, pause):
        self._inner = inner
        self._pause = pause

    def __enter__(self):
        self._inner.__enter__()
        return self

    def __exit__(self, *exc_info):
        return self._inner.__exit__(*exc_info)

    def execute(self, *args, **kwargs):
        return _PausingCursor(self._inner.execute(*args, **kwargs), self._pause)

    def __getattr__(self, name):
        return getattr(self._inner, name)


class AssetCacheInvalidationTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db_path = Path(self._tmp.name) / "gremlin.db"
        self._task_ids = count(1000)
        RawRepository(self.db_path).ensure_schema()
        self.service = LifeDataService(self.db_path, refresh_on_startup=False)
        self._add_asset("ASSET-A")

    def _add_asset(self, asset_number):
        """Import and map an asset the way a sync does: through its own service.

        Mapping through a second instance is the whole point -- it leaves the
        service under test holding a cache nothing has told it to drop, which is
        the situation the app is in after an on-demand sync.
        """

        repo = RawRepository(self.db_path)
        batch_id = repo.start_batch(notes="test")
        repo.upsert_records(batch_id, [{"taskID": next(self._task_ids), "Asset Number": asset_number}])
        LifeDataService(self.db_path, refresh_on_startup=False).refresh_mapped_cmms_records()

    def test_the_asset_list_is_cached_once_built(self):
        self.assertEqual(self.service.asset_numbers(), ["ASSET-A"])
        self.assertIsNotNone(self.service._asset_number_options_cache)

    def test_invalidation_makes_the_next_read_see_new_assets(self):
        self.assertEqual(self.service.asset_numbers(), ["ASSET-A"])
        self._add_asset("ASSET-B")
        # Without being told, the cache would still say ASSET-A alone.
        self.assertEqual(self.service.asset_numbers(), ["ASSET-A"])

        self.service.invalidate_caches()
        self.assertEqual(self.service.asset_numbers(), ["ASSET-A", "ASSET-B"])

    def test_a_build_overtaken_mid_flight_does_not_restore_the_stale_list(self):
        # The dangerous interleaving: a request reads the pre-sync rows, the
        # sync finishes and invalidates, and only then does the request store
        # what it read. Storing it would undo the invalidation and leave the new
        # assets hidden until something else refreshed -- so the store has to
        # notice it was overtaken.
        generation = self.service._asset_cache_generation
        self.service.invalidate_caches()
        self.service._store_asset_options(generation, [{"asset_number": "ASSET-A", "asset_name": "stale"}])

        self.assertIsNone(self.service._asset_number_options_cache)
        self._add_asset("ASSET-B")
        self.assertEqual(self.service.asset_numbers(), ["ASSET-A", "ASSET-B"])

    def test_reads_and_invalidations_can_run_together(self):
        """Reading the list while syncs invalidate it neither deadlocks nor raises.

        Deliberately modest about what it proves. The specific hazard behind the
        locked read -- testing the cache field and then iterating it separately,
        with an invalidation in between -- could not be provoked here: against
        the unlocked version, 1.6 million invalidations across three reader
        threads at a 1 microsecond switch interval produced no failure, because
        CPython has no thread-switch point between those two loads. The lock is
        kept regardless, since that guarantee is an implementation detail of one
        interpreter and not something this code should rely on. What this test
        does cover is that the locking added around the cache cannot deadlock
        two threads against each other, which is a hazard of the fix itself.
        """

        self.service.asset_numbers()
        failures = []
        stop = threading.Event()

        def read_repeatedly():
            try:
                while not stop.is_set():
                    self.assertIsInstance(self.service.asset_number_options(), list)
            except Exception as exc:  # noqa: BLE001 - the point of the test
                failures.append(repr(exc))

        readers = [threading.Thread(target=read_repeatedly) for _ in range(3)]
        for reader in readers:
            reader.start()
        try:
            for _ in range(500):
                self.service.invalidate_caches()
        finally:
            stop.set()
            for reader in readers:
                reader.join(timeout=10)

        self.assertEqual([reader.is_alive() for reader in readers], [False, False, False])
        self.assertEqual(failures, [])

    def test_a_build_that_was_not_overtaken_still_caches(self):
        generation = self.service._asset_cache_generation
        self.service._store_asset_options(generation, [{"asset_number": "ASSET-A", "asset_name": ""}])
        self.assertIsNotNone(self.service._asset_number_options_cache)

    def test_an_invalidation_during_a_real_read_is_not_undone(self):
        # The same race driven through the public method. The pause has to sit
        # between the rows being taken and the cache being written -- pausing
        # any earlier just means the read sees the new asset and there is no
        # race left to observe.
        rows_taken = threading.Event()
        invalidated = threading.Event()
        original_connect = self.service.connect

        def pause_after_read():
            if rows_taken.is_set():
                return
            rows_taken.set()
            self.assertTrue(invalidated.wait(timeout=5))

        def connect_and_pause():
            return _PausingConnection(original_connect(), pause_after_read)

        self.service.connect = connect_and_pause
        self.addCleanup(setattr, self.service, "connect", original_connect)

        result = {}
        reader = threading.Thread(target=lambda: result.update(assets=self.service.asset_numbers()))
        reader.start()
        try:
            self.assertTrue(rows_taken.wait(timeout=5))
            self._add_asset("ASSET-B")
            self.service.invalidate_caches()
        finally:
            invalidated.set()
            reader.join(timeout=10)

        self.assertFalse(reader.is_alive())
        # Whatever that read returned to its own caller, it must not have left a
        # pre-invalidation list behind as the cache.
        self.assertIsNone(self.service._asset_number_options_cache)
        self.assertEqual(self.service.asset_numbers(), ["ASSET-A", "ASSET-B"])


if __name__ == "__main__":
    unittest.main()
