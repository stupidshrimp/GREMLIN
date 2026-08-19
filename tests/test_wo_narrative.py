import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from services.life_data_service import (
    DISPLAY_COLUMNS,
    EXCEL_WO_DISPOSITION_COLUMNS,
    LifeDataService,
)
from services.wo_narrative import NARRATIVE_KEYS, extract_narrative


class ExtractNarrativeTests(unittest.TestCase):
    """The four boxes are found wherever the work order happened to carry them."""

    def test_top_level_task_keys(self):
        found = extract_narrative({
            "taskID": 5,
            "Area Affected": "North conveyor",
            "Condition": "Bearing running hot",
            "Cause": "Loss of lubrication",
            "Action": "Replaced bearing and re-greased",
        })
        self.assertEqual(found, {
            "area_affected": "North conveyor",
            "condition_found": "Bearing running hot",
            "cause": "Loss of lubrication",
            "action_taken": "Replaced bearing and re-greased",
        })

    def test_custom_field_collection(self):
        found = extract_narrative({
            "taskID": 5,
            "customFields": [
                {"name": "Area Affected", "value": "Infeed"},
                {"name": "Condition", "value": "Leaking"},
                {"name": "Cause", "value": "Seal wear"},
                {"name": "Action", "value": "Replaced seal"},
                {"name": "Unrelated field", "value": "should be ignored"},
            ],
        })
        self.assertEqual(found["area_affected"], "Infeed")
        self.assertEqual(found["cause"], "Seal wear")
        self.assertEqual(set(found), set(NARRATIVE_KEYS))

    def test_instruction_items_naming_prompt_and_answer_separately(self):
        # The shape a Limble task instruction takes: the box's prompt and the
        # tech's answer are two keys on one object, not a key and its value.
        found = extract_narrative({
            "taskID": 5,
            "instructions": [
                {"instructionID": 1, "description": "Area Affected", "answer": "Drive end"},
                {"instructionID": 2, "description": "Condition", "answer": "Excessive vibration"},
                {"instructionID": 3, "description": "Cause", "answer": "Misalignment"},
                {"instructionID": 4, "description": "Action", "answer": "Realigned coupling"},
            ],
        })
        self.assertEqual(found, {
            "area_affected": "Drive end",
            "condition_found": "Excessive vibration",
            "cause": "Misalignment",
            "action_taken": "Realigned coupling",
        })

    def test_label_spelling_and_punctuation_variants(self):
        # These labels are typed by whoever builds the template, so case, spacing,
        # punctuation and the usual affect/effect slip all have to resolve.
        found = extract_narrative({
            "AREA AFFECTED:": "a",
            "Condition Found": "b",
            "Root Cause": "c",
            "Action Taken": "d",
        })
        self.assertEqual(found, {
            "area_affected": "a",
            "condition_found": "b",
            "cause": "c",
            "action_taken": "d",
        })
        self.assertEqual(extract_narrative({"Area Effected": "x"}), {"area_affected": "x"})

    def test_top_level_field_beats_a_nested_entry_with_the_same_label(self):
        found = extract_narrative({
            "Cause": "on the task",
            "instructions": [{"description": "Cause", "answer": "in a checklist"}],
        })
        self.assertEqual(found["cause"], "on the task")

    def test_work_order_predating_the_template_change_yields_nothing(self):
        self.assertEqual(
            extract_narrative({"taskID": 9, "name": "Fix pump", "completionNotes": "done"}),
            {},
        )

    def test_blank_and_non_text_answers_are_not_recorded(self):
        # A blank box is unanswered, and a checkbox instruction is not one of
        # these text boxes -- rendering its True as the Cause would be worse than
        # leaving the cell empty.
        found = extract_narrative({"Cause": "   ", "Action": True, "Condition": None})
        self.assertEqual(found, {})

    def test_a_blank_slot_does_not_hide_the_answer_beside_it(self):
        # An object can serialise several supported keys with only one filled in.
        # Settling on the empty `value` would drop a narrative the tech did
        # record, and only one pair is read per object, so there is no second
        # chance to recover from that choice.
        self.assertEqual(
            extract_narrative({"instructions": [
                {"description": "Cause", "value": "", "answer": "Bearing wear"},
            ]}),
            {"cause": "Bearing wear"},
        )
        # Same on the label side: a blank `name` must not mask the real prompt.
        self.assertEqual(
            extract_narrative({"instructions": [
                {"name": "", "description": "Cause", "answer": "Bearing wear"},
            ]}),
            {"cause": "Bearing wear"},
        )
        # A numeric answer still counts; only genuinely blank text is stepped over.
        self.assertEqual(
            extract_narrative({"instructions": [
                {"description": "Condition", "value": "", "answer": 0},
            ]}),
            {"condition_found": "0"},
        )

    def test_a_label_with_no_answer_is_skipped(self):
        found = extract_narrative({"instructions": [{"description": "Cause"}]})
        self.assertEqual(found, {})
        # Every candidate blank is the same as no answer at all.
        self.assertEqual(
            extract_narrative({"instructions": [
                {"description": "Cause", "value": "", "answer": "   "},
            ]}),
            {},
        )

    def test_unrelated_area_field_is_not_read_as_the_affected_area(self):
        # "area" on its own is a plant location on plenty of CMMS payloads.
        self.assertEqual(extract_narrative({"area": "Building 4"}), {})

    def test_the_search_stops_before_a_pathological_payload_does(self):
        # A real payload nests twice at most (task -> instructions -> one item),
        # so reaching four levels is headroom; the point of the cap is that a
        # payload nested past that cannot turn one record's mapping into a walk.
        def buried(depth: int) -> dict:
            payload: dict = {}
            node = payload
            for _ in range(depth):
                node["child"] = {}
                node = node["child"]
            node["Cause"] = "deep"
            return payload

        self.assertEqual(extract_narrative(buried(4)), {"cause": "deep"})
        self.assertEqual(extract_narrative(buried(5)), {})
        self.assertEqual(extract_narrative(buried(400)), {})


class NarrativeMappingTests(unittest.TestCase):
    """The boxes survive the trip from raw JSON to the disposition tables."""

    def _service_with_task(self, task: dict) -> tuple[LifeDataService, Path]:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db_path = Path(tmp.name) / "gremlin.db"
        service = LifeDataService(db_path, refresh_on_startup=False)
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS import_batch "
                "(import_batch_id INTEGER PRIMARY KEY, status TEXT)"
            )
            conn.execute("INSERT INTO import_batch (import_batch_id, status) VALUES (0, 'COMPLETED')")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS raw_cmms_record ("
                "raw_record_id INTEGER PRIMARY KEY, import_batch_id INTEGER NOT NULL DEFAULT 0, "
                "source_record_id TEXT, raw_json TEXT NOT NULL, raw_content_hash TEXT)"
            )
            conn.execute(
                "INSERT INTO raw_cmms_record (raw_record_id, import_batch_id, raw_json) VALUES (1, 0, ?)",
                (json.dumps(task),),
            )
            conn.commit()
        service.refresh_mapped_cmms_records()
        return service, db_path

    def test_mapped_record_carries_the_narrative_columns(self):
        service, db_path = self._service_with_task({
            "taskID": 1,
            "assetID": 7,
            "Asset Number": "7",
            "type": "6",
            "name": "Pump failed",
            "instructions": [
                {"description": "Area Affected", "answer": "Discharge side"},
                {"description": "Condition", "answer": "No flow"},
                {"description": "Cause", "answer": "Impeller worn"},
                {"description": "Action", "answer": "Replaced impeller"},
            ],
        })
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT area_affected, condition_found, cause, action_taken "
                "FROM mapped_cmms_record WHERE raw_record_id = 1"
            ).fetchone()
        self.assertEqual(row["area_affected"], "Discharge side")
        self.assertEqual(row["condition_found"], "No flow")
        self.assertEqual(row["cause"], "Impeller worn")
        self.assertEqual(row["action_taken"], "Replaced impeller")

    def test_disposition_rows_expose_the_narrative(self):
        service, _ = self._service_with_task({
            "taskID": 1,
            "assetID": 7,
            "Asset Number": "7",
            "type": "6",
            "name": "Bearing replaced",
            "Cause": "Loss of lubrication",
            "Action": "Replaced bearing",
        })
        rows = service.disposition_rows("7", "wo")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["cause"], "Loss of lubrication")
        self.assertEqual(rows[0]["action_taken"], "Replaced bearing")
        # Blank boxes stay blank rather than becoming a placeholder string.
        self.assertIsNone(rows[0]["area_affected"])

    def test_disposition_search_matches_narrative_text(self):
        service, _ = self._service_with_task({
            "taskID": 1,
            "assetID": 7,
            "Asset Number": "7",
            "type": "6",
            "name": "Corrective repair",
            "Cause": "Misalignment",
        })
        self.assertEqual(len(service.disposition_rows("7", "wo", search="misalignment")), 1)
        self.assertEqual(len(service.disposition_rows("7", "wo", search="cavitation")), 0)
        self.assertEqual(service.disposition_row_count("7", "wo", search="misalignment"), 1)

    def test_narrative_predating_the_template_change_stays_blank(self):
        service, _ = self._service_with_task({
            "taskID": 1,
            "assetID": 7,
            "Asset Number": "7",
            "type": "6",
            "name": "Old repair",
            "completionNotes": "Everything in one box, as it used to be.",
        })
        row = service.disposition_rows("7", "wo")[0]
        for key in NARRATIVE_KEYS:
            self.assertIsNone(row[key])

    def test_excel_template_keeps_the_boxes_as_their_own_columns(self):
        # One stacked column on screen, four columns in the workbook, so each can
        # be sorted and filtered there.
        for key in NARRATIVE_KEYS:
            self.assertIn(key, EXCEL_WO_DISPOSITION_COLUMNS)
            self.assertNotIn(key, DISPLAY_COLUMNS)


if __name__ == "__main__":
    unittest.main()
