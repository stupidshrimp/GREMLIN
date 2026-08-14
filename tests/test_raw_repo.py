import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from repositories.raw_repo import RawRepository, _merge_preserved_fields, _utc_now_text


class MergePreservedFieldsTests(unittest.TestCase):
    def test_fresh_downtime_drops_stale_downtime_provenance(self):
        # A row imported by the retired --downtime-unit path carries provenance
        # describing how its stored (minutes) downtime was scaled. A refresh that
        # supplies a fresh raw downtime (seconds) invalidates that provenance; it
        # must be dropped so the mapper does not treat the new seconds value as
        # already-normalised minutes.
        existing = {
            "taskID": 5,
            "downtime": 210.0,
            "downtime_source_value": 12600,
            "downtime_source_unit": "seconds",
        }
        merged = _merge_preserved_fields(existing, {"taskID": 5, "downtime": 12600})
        self.assertEqual(merged["downtime"], 12600)
        self.assertNotIn("downtime_source_value", merged)
        self.assertNotIn("downtime_source_unit", merged)

    def test_narrow_refresh_keeps_downtime_provenance(self):
        # A narrow payload that omits downtime must not disturb the stored value
        # or its provenance.
        existing = {
            "taskID": 5,
            "downtime": 210.0,
            "downtime_source_value": 12600,
            "downtime_source_unit": "seconds",
        }
        merged = _merge_preserved_fields(existing, {"taskID": 5, "description": "narrow"})
        self.assertEqual(merged["downtime"], 210.0)
        self.assertEqual(merged["downtime_source_unit"], "seconds")


class RawRepositoryTests(unittest.TestCase):
    def test_batch_timestamps_retain_fractional_seconds(self):
        self.assertRegex(_utc_now_text(), r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{6}$")

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

    def test_duplicate_task_id_append_keeps_incoming_asset_identity_fields(self):
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
                (json.dumps({"taskID": 42, "Asset Number": "legacy-asset-a", "description": "historic A"}),),
            )
            conn.execute(
                "INSERT INTO raw_cmms_record(import_batch_id, source_record_id, raw_json) VALUES (1, '42', ?)",
                (json.dumps({"taskID": 42, "Asset Number": "legacy-asset-b", "description": "historic B"}),),
            )
            conn.commit()
            conn.close()

            repo = RawRepository(db_path)
            result = repo.upsert_records(2, [{
                "taskID": 42,
                "Asset Number": "incoming-current-asset",
                "description": "current Limble task",
            }])

            self.assertEqual(result, {"inserted": 1, "updated": 0, "skipped": 0})
            with sqlite3.connect(db_path) as conn:
                rows = [json.loads(row[0]) for row in conn.execute("SELECT raw_json FROM raw_cmms_record ORDER BY raw_record_id")]
            self.assertEqual(rows[0]["Asset Number"], "legacy-asset-a")
            self.assertEqual(rows[1]["Asset Number"], "legacy-asset-b")
            self.assertEqual(rows[2]["Asset Number"], "incoming-current-asset")

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

    def test_update_does_not_drop_fields_the_incoming_payload_omits(self):
        # A Limble /tasks refresh carries a narrower payload than the stored row
        # (the list endpoint omits completion notes, completed dates, downtime,
        # etc.). The sync must add/update fields without blanking curated data the
        # API simply did not return, otherwise a single sync guts every row.
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "gremlin.db"
            repo = RawRepository(db_path)
            repo.ensure_schema()
            batch_id = repo.start_batch()
            repo.upsert_records(batch_id, [{
                "taskID": 7,
                "name": "Pump repair",
                "completionNotes": "replaced mechanical seal",
                "dateCompleted": 1700000000,
                "completedDate_Final": "2023-11-14T22:13:20+00:00",
                "downtime": 45,
            }])

            next_batch_id = repo.start_batch()
            # Narrower refresh: name changes, everything else is absent.
            result = repo.upsert_records(next_batch_id, [{"taskID": 7, "name": "Pump repair (rev)"}])

            self.assertEqual(result, {"inserted": 0, "updated": 1, "skipped": 0})
            with sqlite3.connect(db_path) as conn:
                raw = json.loads(conn.execute("SELECT raw_json FROM raw_cmms_record").fetchone()[0])
            self.assertEqual(raw["name"], "Pump repair (rev)")
            self.assertEqual(raw["completionNotes"], "replaced mechanical seal")
            self.assertEqual(raw["dateCompleted"], 1700000000)
            self.assertEqual(raw["completedDate_Final"], "2023-11-14T22:13:20+00:00")
            self.assertEqual(raw["downtime"], 45)

    def test_update_empty_incoming_value_does_not_blank_existing_field(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "gremlin.db"
            repo = RawRepository(db_path)
            repo.ensure_schema()
            batch_id = repo.start_batch()
            repo.upsert_records(batch_id, [{
                "taskID": 7,
                "name": "Pump repair",
                "completionNotes": "replaced mechanical seal",
            }])

            next_batch_id = repo.start_batch()
            # An explicitly empty completionNotes must not wipe the curated value.
            repo.upsert_records(next_batch_id, [{
                "taskID": 7,
                "name": "Pump repair",
                "completionNotes": "",
            }])

            with sqlite3.connect(db_path) as conn:
                raw = json.loads(conn.execute("SELECT raw_json FROM raw_cmms_record").fetchone()[0])
            self.assertEqual(raw["completionNotes"], "replaced mechanical seal")

    def test_cleared_source_date_drops_stale_derived_date(self):
        # A reopened task comes back with its source date cleared to 0. The derived
        # completedDate_Final (which the mapper treats as "completed") must not
        # linger from the previous payload, or the row stays falsely completed.
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "gremlin.db"
            repo = RawRepository(db_path)
            repo.ensure_schema()
            batch_id = repo.start_batch()
            repo.upsert_records(batch_id, [{
                "taskID": 7,
                "dateCompleted": 1700000000,
                "completedDate_Final": "2023-11-14T22:13:20+00:00",
                "completedDateTime": "2023-11-14T22:13:20+00:00",
            }])

            next_batch_id = repo.start_batch()
            # Reopened: source present but cleared, no derived value emitted.
            repo.upsert_records(next_batch_id, [{"taskID": 7, "dateCompleted": 0}])

            with sqlite3.connect(db_path) as conn:
                raw = json.loads(conn.execute("SELECT raw_json FROM raw_cmms_record").fetchone()[0])
            self.assertEqual(raw["dateCompleted"], 0)
            self.assertNotIn("completedDate_Final", raw)
            self.assertNotIn("completedDateTime", raw)

    def test_derived_date_preserved_when_source_is_omitted(self):
        # A narrower refresh that omits the source date entirely must keep the
        # existing derived date, distinguishing "omitted" from "cleared".
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "gremlin.db"
            repo = RawRepository(db_path)
            repo.ensure_schema()
            batch_id = repo.start_batch()
            repo.upsert_records(batch_id, [{
                "taskID": 7,
                "name": "Pump repair",
                "dateCompleted": 1700000000,
                "completedDate_Final": "2023-11-14T22:13:20+00:00",
            }])

            next_batch_id = repo.start_batch()
            # Source omitted entirely (narrow payload); only name changes.
            repo.upsert_records(next_batch_id, [{"taskID": 7, "name": "Pump repair (rev)"}])

            with sqlite3.connect(db_path) as conn:
                raw = json.loads(conn.execute("SELECT raw_json FROM raw_cmms_record").fetchone()[0])
            self.assertEqual(raw["completedDate_Final"], "2023-11-14T22:13:20+00:00")
            self.assertEqual(raw["dateCompleted"], 1700000000)

    def test_cleared_source_date_drops_legacy_derived_aliases(self):
        # A legacy row may store a mapper-supported alias (dateCompleted_Final).
        # A reopen must drop the alias too, not just the canonical derived field,
        # or the mapper still reads the row as completed.
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "gremlin.db"
            repo = RawRepository(db_path)
            repo.ensure_schema()
            batch_id = repo.start_batch()
            repo.upsert_records(batch_id, [{
                "taskID": 7,
                "dateCompleted": 1700000000,
                "dateCompleted_Final": "2023-11-14T22:13:20+00:00",
            }])

            next_batch_id = repo.start_batch()
            repo.upsert_records(next_batch_id, [{"taskID": 7, "dateCompleted": 0}])

            with sqlite3.connect(db_path) as conn:
                raw = json.loads(conn.execute("SELECT raw_json FROM raw_cmms_record").fetchone()[0])
            self.assertEqual(raw["dateCompleted"], 0)
            self.assertNotIn("dateCompleted_Final", raw)

    def test_blank_source_date_clears_stored_source(self):
        # Limble may clear a date to None/"" rather than 0. The stored source
        # timestamp must clear too, so it stays consistent with the derived cleanup
        # (no stale dateCompleted left behind for source-fallback consumers).
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "gremlin.db"
            repo = RawRepository(db_path)
            repo.ensure_schema()
            batch_id = repo.start_batch()
            repo.upsert_records(batch_id, [{
                "taskID": 7,
                "dateCompleted": 1700000000,
                "completedDate_Final": "2023-11-14T22:13:20+00:00",
            }])

            next_batch_id = repo.start_batch()
            repo.upsert_records(next_batch_id, [{"taskID": 7, "dateCompleted": None}])

            with sqlite3.connect(db_path) as conn:
                raw = json.loads(conn.execute("SELECT raw_json FROM raw_cmms_record").fetchone()[0])
            self.assertIn("dateCompleted", raw)
            self.assertIn(raw["dateCompleted"], (None, "", 0))
            self.assertNotIn("completedDate_Final", raw)

    def test_non_date_blank_incoming_still_protected(self):
        # The source-date exemption must not weaken the general guard: a blank
        # incoming value for a non-date field still cannot blank curated data.
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "gremlin.db"
            repo = RawRepository(db_path)
            repo.ensure_schema()
            batch_id = repo.start_batch()
            repo.upsert_records(batch_id, [{"taskID": 7, "completionNotes": "curated note"}])

            next_batch_id = repo.start_batch()
            repo.upsert_records(next_batch_id, [{"taskID": 7, "completionNotes": None}])

            with sqlite3.connect(db_path) as conn:
                raw = json.loads(conn.execute("SELECT raw_json FROM raw_cmms_record").fetchone()[0])
            self.assertEqual(raw["completionNotes"], "curated note")

    def test_new_task_files_under_curated_asset_number_from_matched_history(self):
        # A brand-new task arrives from Limble labelled with only the bare numeric
        # assetID. If history groups that asset under a curated Asset Number, the new
        # task must be filed there too, otherwise new work orders land under a numeric
        # asset the user never looks at (the "few new WOs" symptom).
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "gremlin.db"
            repo = RawRepository(db_path)
            repo.ensure_schema()
            batch_id = repo.start_batch()
            # Legacy history: curated Asset Number, no numeric assetID of its own.
            repo.upsert_records(batch_id, [{
                "taskID": 7,
                "name": "old corrective",
                "Asset Number": "PUMP-01",
            }])

            next_batch_id = repo.start_batch()
            # A refresh of task 7 (now carrying the numeric assetID) alongside a
            # brand-new task 999 on the same physical asset, labelled numerically.
            result = repo.upsert_records(next_batch_id, [
                {"taskID": 7, "name": "old corrective", "assetID": 67, "Asset Number": "67"},
                {"taskID": 999, "name": "new corrective", "assetID": 67, "Asset Number": "67"},
            ])

            self.assertEqual(result, {"inserted": 1, "updated": 1, "skipped": 0})
            with sqlite3.connect(db_path) as conn:
                rows = {
                    json.loads(row[0])["taskID"]: json.loads(row[0])
                    for row in conn.execute("SELECT raw_json FROM raw_cmms_record")
                }
            # Refreshed history keeps its curated number (pinning); the brand-new task
            # is filed under the same curated asset instead of the numeric "67".
            self.assertEqual(rows[7]["Asset Number"], "PUMP-01")
            self.assertEqual(rows[999]["Asset Number"], "PUMP-01")

    def test_numeric_misfiled_asset_number_self_heals_on_update(self):
        # A row an earlier (pre-fix) sync already inserted under the bare numeric id
        # is relabelled with the curated number on the next sync, so mis-filed work
        # orders rejoin their asset without a manual backfill. Seed the post-buggy
        # state directly: a curated history row that now carries the numeric assetID
        # (as a matched refresh would add), plus a new-task row stuck under that id.
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "gremlin.db"
            repo = RawRepository(db_path)
            repo.ensure_schema()
            with sqlite3.connect(db_path) as conn:
                conn.execute("INSERT INTO import_batch(import_batch_id, status) VALUES (1, 'COMPLETED')")
                conn.execute(
                    "INSERT INTO raw_cmms_record(import_batch_id, source_record_id, raw_json) VALUES (1, '7', ?)",
                    (json.dumps({"taskID": 7, "assetID": 67, "Asset Number": "PUMP-01"}),),
                )
                conn.execute(
                    "INSERT INTO raw_cmms_record(import_batch_id, source_record_id, raw_json) VALUES (1, '999', ?)",
                    (json.dumps({"taskID": 999, "assetID": 67, "Asset Number": "67"}),),
                )
                conn.commit()

            next_batch_id = repo.start_batch()
            result = repo.upsert_records(next_batch_id, [
                {"taskID": 7, "assetID": 67, "Asset Number": "67"},
                {"taskID": 999, "assetID": 67, "Asset Number": "67"},
            ])

            self.assertEqual(result["inserted"], 0)
            with sqlite3.connect(db_path) as conn:
                rows = {
                    json.loads(row[0])["taskID"]: json.loads(row[0])
                    for row in conn.execute("SELECT raw_json FROM raw_cmms_record")
                }
            self.assertEqual(rows[7]["Asset Number"], "PUMP-01")
            self.assertEqual(rows[999]["Asset Number"], "PUMP-01")

    def test_bridge_reads_asset_pairing_from_a_later_duplicate_row(self):
        # A duplicated task ID is a supported legacy state. When only a later
        # duplicate carries the assetID + curated Asset Number pairing, the bridge
        # must still learn it, so a brand-new task on that asset is filed under the
        # curated number rather than the bare numeric id.
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "gremlin.db"
            repo = RawRepository(db_path)
            repo.ensure_schema()
            with sqlite3.connect(db_path) as conn:
                conn.execute("INSERT INTO import_batch(import_batch_id, status) VALUES (1, 'COMPLETED')")
                # First duplicate: curated number, but no numeric assetID.
                conn.execute(
                    "INSERT INTO raw_cmms_record(import_batch_id, source_record_id, raw_json) VALUES (1, '42', ?)",
                    (json.dumps({"taskID": 42, "Asset Number": "PUMP-01"}),),
                )
                # Later duplicate: the only row carrying the assetID + curated pair.
                conn.execute(
                    "INSERT INTO raw_cmms_record(import_batch_id, source_record_id, raw_json) VALUES (1, '42', ?)",
                    (json.dumps({"taskID": 42, "assetID": 67, "Asset Number": "PUMP-01"}),),
                )
                conn.commit()

            next_batch_id = repo.start_batch()
            # A brand-new task on asset 67; task 42 is not in this batch, so the
            # pairing can only come from the later duplicate via source 1.
            repo.upsert_records(next_batch_id, [
                {"taskID": 999, "assetID": 67, "Asset Number": "67"},
            ])

            with sqlite3.connect(db_path) as conn:
                raw = json.loads(
                    conn.execute("SELECT raw_json FROM raw_cmms_record WHERE source_record_id = '999'").fetchone()[0]
                )
            self.assertEqual(raw["Asset Number"], "PUMP-01")

    def test_new_task_on_unknown_asset_keeps_numeric_asset_number(self):
        # An asset with no curated history keeps the numeric id -- the bridge only
        # relabels when history actually knows a curated number for that assetID.
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "gremlin.db"
            repo = RawRepository(db_path)
            repo.ensure_schema()
            batch_id = repo.start_batch()
            repo.upsert_records(batch_id, [{"taskID": 7, "assetID": 67, "Asset Number": "PUMP-01"}])

            next_batch_id = repo.start_batch()
            result = repo.upsert_records(next_batch_id, [
                {"taskID": 1000, "assetID": 5000, "Asset Number": "5000"},
            ])

            self.assertEqual(result, {"inserted": 1, "updated": 0, "skipped": 0})
            with sqlite3.connect(db_path) as conn:
                raw = json.loads(
                    conn.execute("SELECT raw_json FROM raw_cmms_record WHERE source_record_id = '1000'").fetchone()[0]
                )
            self.assertEqual(raw["Asset Number"], "5000")


if __name__ == "__main__":
    unittest.main()
