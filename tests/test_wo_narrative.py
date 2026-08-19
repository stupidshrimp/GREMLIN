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

    def test_the_shape_limble_task_instructions_actually_use(self):
        # Verbatim from a Limble export: the prompt is `instructionText` and the
        # answer is `response`, and the label reads "Affected area" rather than
        # "Area Affected". The ACCA boxes sit at the end of a long safety
        # checklist whose other items must not be mistaken for them.
        found = extract_narrative({"taskID": 239728, "instructions": [
            {"instructionText": "Does this job require LOTO?", "response": "No", "type": "option list"},
            {"instructionText": "Test LOTO.", "response": "", "type": "checkbox"},
            {"instructionText": "Complete work to solve problem", "response": "1", "type": "checkbox"},
            {"instructionText": "Obtain and record all special tools before starting work.", "response": "", "type": "text box"},
            {"instructionText": "Affected area", "response": "Timing belt", "type": "text box", "order": "1", "instructionID": "48", "parentID": "47"},
            {"instructionText": "Condition", "response": "Worn", "type": "text box", "order": "2", "instructionID": "49", "parentID": "47"},
            {"instructionText": "Cause", "response": "Aging", "type": "text box", "order": "3", "instructionID": "50", "parentID": "47"},
            {"instructionText": "Action", "response": "Replaced timing belt.", "type": "text box", "order": "4", "instructionID": "51", "parentID": "47"},
        ]})
        self.assertEqual(found, {
            "area_affected": "Timing belt",
            "condition_found": "Worn",
            "cause": "Aging",
            "action_taken": "Replaced timing belt.",
        })

    def test_the_rich_text_editors_markup_is_stripped(self):
        # Limble's text boxes return the editor's markup, not plain text. Shown
        # verbatim these cells are a wall of tags, so the answer is recovered here.
        found = extract_narrative({"instructions": [
            {"instructionText": "Condition", "type": "text box", "response": (
                "<a _ngcontent-ng-c3498802757='' class='cursor ng-star-inserted' "
                "style='color: rgb(80, 131, 213); word-break: break-word;'></a>"
                "<div><a _ngcontent-ng-c3498802757='' class='cursor ng-star-inserted'>"
                "<div>It was leaking which spilled into confined space floor.</div>"
                "<div><br></div></a></div>"
            )},
            {"instructionText": "Cause", "type": "text box",
             "response": "Unknown,&nbsp; dropped it box, build.10"},
        ]})
        self.assertEqual(found["condition_found"], "It was leaking which spilled into confined space floor.")
        # &nbsp; decodes and the double space it leaves collapses.
        self.assertEqual(found["cause"], "Unknown, dropped it box, build.10")

    def test_adjacent_blocks_do_not_run_their_words_together(self):
        found = extract_narrative({"instructions": [
            {"instructionText": "Action", "type": "text box",
             "response": "<div>Replaced the seal</div><div>tested for leaks</div>"},
        ]})
        self.assertEqual(found["action_taken"], "Replaced the seal tested for leaks")

    def test_arithmetic_is_not_mistaken_for_markup(self):
        found = extract_narrative({"Condition": "temp < 50 > 40 on the gauge"})
        self.assertEqual(found["condition_found"], "temp < 50 > 40 on the gauge")

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


class InstructionFetchTests(unittest.TestCase):
    """The instructions phase is selective about which tasks it pays for."""

    class _StubClient:
        """A Limble client that records which tasks were asked for."""

        def __init__(self, instructions_by_task: dict[str, list]) -> None:
            self.instructions_by_task = instructions_by_task
            self.asked: list[str] = []

        def get_task_instructions(self, task_id):
            self.asked.append(str(task_id))
            return self.instructions_by_task.get(str(task_id), [])

    class _StubRepo:
        def __init__(self, stored: list[tuple[str, dict]]) -> None:
            self._stored = stored

        def iter_task_payloads(self):
            yield from self._stored

    def _acca(self, area: str) -> list[dict]:
        return [
            {"instructionText": "Affected area", "response": area, "type": "text box"},
            {"instructionText": "Condition", "response": "Worn", "type": "text box"},
            {"instructionText": "Cause", "response": "Aging", "type": "text box"},
            {"instructionText": "Action", "response": "Replaced", "type": "text box"},
        ]

    def _service(self, client, repo, **kwargs):
        from services.ingestion_service import IngestionService

        return IngestionService(
            limble_client=client,
            raw_repo=repo,
            fetch_instructions=True,
            log=lambda _message: None,
            **kwargs,
        )

    def test_the_four_answers_land_on_the_task(self):
        client = self._StubClient({"1": self._acca("Timing belt")})
        service = self._service(client, self._StubRepo([]))
        task = {"taskID": 1, "dateCompleted": 1750000000}
        service._attach_instructions([task])
        self.assertEqual(task["area_affected"], "Timing belt")
        self.assertEqual(task["action_taken"], "Replaced")
        # Only the four answers are kept; the checklist itself is not stored.
        self.assertNotIn("instructions", task)

    def test_an_open_task_is_not_paid_for(self):
        # The boxes are the closing write-up, so a task still open has nothing.
        client = self._StubClient({})
        service = self._service(client, self._StubRepo([]))
        service._attach_instructions([{"taskID": 1, "dateCompleted": 0}])
        self.assertEqual(client.asked, [])

    def test_a_task_already_carrying_all_four_is_not_refetched(self):
        stored = [("1", {
            "taskID": 1, "area_affected": "a", "condition_found": "b",
            "cause": "c", "action_taken": "d",
        })]
        client = self._StubClient({"1": self._acca("x"), "2": self._acca("y")})
        service = self._service(client, self._StubRepo(stored))
        service._attach_instructions([
            {"taskID": 1, "dateCompleted": 1750000000},
            {"taskID": 2, "dateCompleted": 1750000000},
        ])
        self.assertEqual(client.asked, ["2"])

    def test_a_partial_narrative_is_still_worth_refetching(self):
        stored = [("1", {"taskID": 1, "cause": "c"})]
        client = self._StubClient({"1": self._acca("x")})
        service = self._service(client, self._StubRepo(stored))
        service._attach_instructions([{"taskID": 1, "dateCompleted": 1750000000}])
        self.assertEqual(client.asked, ["1"])

    def test_the_cap_bounds_a_run_and_reports_what_is_left(self):
        client = self._StubClient({str(i): self._acca(str(i)) for i in range(5)})
        service = self._service(client, self._StubRepo([]), instructions_limit=2)
        counts = service._attach_instructions(
            [{"taskID": i, "dateCompleted": 1750000000} for i in range(5)]
        )
        self.assertEqual(len(client.asked), 2)
        self.assertEqual(counts["instructions_fetched"], 2)
        self.assertEqual(counts["instructions_remaining"], 3)

    def test_one_unreadable_task_does_not_lose_the_rest(self):
        class Failing(InstructionFetchTests._StubClient):
            def get_task_instructions(self, task_id):
                if str(task_id) == "1":
                    raise RuntimeError("500 from Limble")
                return super().get_task_instructions(task_id)

        client = Failing({"2": self._acca("Infeed")})
        service = self._service(client, self._StubRepo([]))
        tasks = [
            {"taskID": 1, "dateCompleted": 1750000000},
            {"taskID": 2, "dateCompleted": 1750000000},
        ]
        counts = service._attach_instructions(tasks)
        self.assertNotIn("area_affected", tasks[0])
        self.assertEqual(tasks[1]["area_affected"], "Infeed")
        self.assertEqual(counts["instructions_found"], 1)

    def test_nothing_is_fetched_unless_asked_for(self):
        client = self._StubClient({"1": self._acca("x")})
        from services.ingestion_service import IngestionService

        service = IngestionService(
            limble_client=client, raw_repo=self._StubRepo([]), log=lambda _m: None
        )
        self.assertEqual(service._attach_instructions([{"taskID": 1, "dateCompleted": 1}]), {})
        self.assertEqual(client.asked, [])
