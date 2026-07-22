import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from repositories.raw_repo import RawRepository


class RawRepositoryTests(unittest.TestCase):
    def test_duplicate_task_ids_are_preserved_instead_of_mass_updated(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "gremlin.db"
            conn = sqlite3.connect(db_path)
            conn.execute("CREATE TABLE import_batch (import_batch_id INTEGER PRIMARY KEY, status TEXT)")
            conn.execute(
                """
                CREATE TABLE raw_cmms_record (
                    raw_record_id INTEGER PRIMARY KEY,
                    import_batch_id INTEGER NOT NULL,
                    source_record_id TEXT,
                    raw_json TEXT NOT NULL,
                    raw_content_hash TEXT
                )
                """
            )
            conn.execute("INSERT INTO import_batch(import_batch_id, status) VALUES (1, 'COMPLETED')")
            conn.execute(
                "INSERT INTO raw_cmms_record(import_batch_id, source_record_id, raw_json) VALUES (1, '42', ?)",
                (json.dumps({"taskID": 42, "description": "historic row A"}),),
            )
            conn.execute(
                "INSERT INTO raw_cmms_record(import_batch_id, source_record_id, raw_json) VALUES (1, '42', ?)",
                (json.dumps({"taskID": 42, "description": "historic row B"}),),
            )
            conn.commit()
            conn.close()

            repo = RawRepository(db_path)
            result = repo.upsert_records(2, [{"taskID": 42, "description": "current Limble task"}])

            self.assertEqual(result, {"inserted": 1, "updated": 0, "skipped": 0})
            with sqlite3.connect(db_path) as conn:
                rows = [json.loads(row[0]) for row in conn.execute("SELECT raw_json FROM raw_cmms_record ORDER BY raw_record_id")]
            self.assertEqual(rows[0]["description"], "historic row A")
            self.assertEqual(rows[1]["description"], "historic row B")
            self.assertEqual(rows[2]["description"], "current Limble task")

    def test_duplicate_task_id_skips_when_payload_matches_later_duplicate(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "gremlin.db"
            conn = sqlite3.connect(db_path)
            conn.execute("CREATE TABLE import_batch (import_batch_id INTEGER PRIMARY KEY, status TEXT)")
            conn.execute(
                """
                CREATE TABLE raw_cmms_record (
                    raw_record_id INTEGER PRIMARY KEY,
                    import_batch_id INTEGER NOT NULL,
                    source_record_id TEXT,
                    raw_json TEXT NOT NULL,
                    raw_content_hash TEXT
                )
                """
            )
            conn.execute("INSERT INTO import_batch(import_batch_id, status) VALUES (1, 'COMPLETED')")
            conn.execute(
                "INSERT INTO raw_cmms_record(import_batch_id, source_record_id, raw_json) VALUES (1, '42', ?)",
                (json.dumps({"taskID": 42, "description": "historic row A"}),),
            )
            conn.execute(
                "INSERT INTO raw_cmms_record(import_batch_id, source_record_id, raw_json) VALUES (1, '42', ?)",
                (json.dumps({"taskID": 42, "description": "current Limble task"}),),
            )
            conn.commit()
            conn.close()

            repo = RawRepository(db_path)
            result = repo.upsert_records(2, [{"taskID": 42, "description": "current Limble task"}])

            self.assertEqual(result, {"inserted": 0, "updated": 0, "skipped": 1})
            self.assertEqual(repo.raw_record_count(), 2)

    def test_unique_task_id_update_preserves_existing_asset_identity_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "gremlin.db"
            repo = RawRepository(db_path)
            repo.ensure_schema()
            batch_id = repo.start_batch()
            repo.upsert_records(batch_id, [{
                "taskID": 7,
                "name": "old",
                "Asset Number": "Legacy-Parent-Asset",
                "Asset Name": "Legacy Parent Asset",
            }])

            next_batch_id = repo.start_batch()
            result = repo.upsert_records(next_batch_id, [{
                "taskID": 7,
                "name": "new",
                "assetID": 67,
                "Asset Number": "67",
                "Asset Name": "API Child Asset",
            }])

            self.assertEqual(result, {"inserted": 0, "updated": 1, "skipped": 0})
            with sqlite3.connect(db_path) as conn:
                raw = json.loads(conn.execute("SELECT raw_json FROM raw_cmms_record").fetchone()[0])
            self.assertEqual(raw["name"], "new")
            self.assertEqual(raw["assetID"], 67)
            self.assertEqual(raw["Asset Number"], "Legacy-Parent-Asset")
            self.assertEqual(raw["Asset Name"], "Legacy Parent Asset")

    def test_unique_task_id_updates_in_place(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "gremlin.db"
            repo = RawRepository(db_path)
            repo.ensure_schema()
            batch_id = repo.start_batch()
            self.assertEqual(repo.upsert_records(batch_id, [{"taskID": 7, "name": "old"}])["inserted"], 1)
            next_batch_id = repo.start_batch()
            result = repo.upsert_records(next_batch_id, [{"taskID": 7, "name": "new"}])
            self.assertEqual(result, {"inserted": 0, "updated": 1, "skipped": 0})
            self.assertEqual(repo.raw_record_count(), 1)


if __name__ == "__main__":
    unittest.main()
