"""A read transaction holds one snapshot across its queries.

The Disposition page and every analysis read several queries in one
LifeDataService.read_transaction(): the page of rows and its counts, a
population and its saved fit. A save or an import committing between two of
those queries would pair a count with rows it does not describe, so the read
holds a shared lock a writer cannot commit past until it ends.
"""

import sqlite3
import tempfile
import unittest
from pathlib import Path

from services.life_data_service import LifeDataService


class ReadTransactionTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.service = LifeDataService(Path(tmp.name) / "gremlin.db", refresh_on_startup=False)
        with self.service.write_connection() as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS import_batch (import_batch_id INTEGER PRIMARY KEY, status TEXT)")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS raw_cmms_record ("
                "raw_record_id INTEGER PRIMARY KEY, import_batch_id INTEGER NOT NULL DEFAULT 1, raw_json TEXT NOT NULL)"
            )
            conn.execute("INSERT INTO import_batch (import_batch_id, status) VALUES (1, 'COMPLETED')")
            for record_id in (1, 2, 3):
                conn.execute("INSERT INTO raw_cmms_record (raw_record_id, raw_json) VALUES (?, '{}')", (record_id,))
                conn.execute(
                    "INSERT INTO mapped_cmms_record (mapped_record_id, raw_record_id, import_batch_id, asset_number, "
                    "task_id, mapping_version) VALUES (?, ?, 1, 'A-1', ?, 'v4')",
                    (record_id, record_id, str(record_id)),
                )

    def test_a_read_transaction_holds_one_snapshot_across_its_queries(self):
        with self.service.read_transaction() as conn:
            before = conn.execute("SELECT COUNT(*) FROM mapped_cmms_record").fetchone()[0]
            self.assertEqual(before, 3)

            writer = sqlite3.connect(self.service.db_path, timeout=0.2)
            try:
                # Reserving the writer slot is allowed alongside a reader; it is the
                # commit that has to wait, and here it gives up on the busy timeout.
                writer.execute("BEGIN IMMEDIATE")
                writer.execute("DELETE FROM mapped_cmms_record")
                with self.assertRaises(sqlite3.OperationalError):
                    writer.commit()
                writer.rollback()
            finally:
                writer.close()

            # Same snapshot as the first query, despite the delete attempt in between.
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM mapped_cmms_record").fetchone()[0], before)

        # The lock is released on the way out, so the next write is free to proceed.
        with self.service.write_connection() as conn:
            conn.execute("DELETE FROM mapped_cmms_record WHERE mapped_record_id = 3")
        with self.service.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM mapped_cmms_record").fetchone()[0], 2)


if __name__ == "__main__":
    unittest.main()
