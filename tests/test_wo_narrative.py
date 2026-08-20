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
from repositories.raw_repo import INSTRUCTIONS_READ_ERROR
from services.ingestion_service import INSTRUCTIONS_READ_AT
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
            # Every chunk the phase asked to have written down, in order.
            self.checkpoints: list[dict[str, dict]] = []

        def iter_task_payloads(self):
            yield from self._stored

        def record_instruction_read(self, reads):
            self.checkpoints.append({key: dict(fields) for key, fields in reads.items()})
            return len(reads)

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

    def test_a_task_already_read_is_not_read_again(self):
        stored = [("1", {"taskID": 1, INSTRUCTIONS_READ_AT: 1_780_000_000})]
        client = self._StubClient({"1": self._acca("x"), "2": self._acca("y")})
        service = self._service(client, self._StubRepo(stored))
        service._attach_instructions([
            {"taskID": 1, "dateCompleted": 1_750_000_000, "lastEdited": 1_750_000_000},
            {"taskID": 2, "dateCompleted": 1_750_000_000, "lastEdited": 1_750_000_000},
        ])
        self.assertEqual(client.asked, ["2"])

    def test_a_legacy_narrative_is_read_once_more_to_earn_a_stamp(self):
        # Stored by a build that kept answers without recording the attempt.
        # Treating those as read forever would freeze them: no later edit could
        # ever exceed that marker, so a correction made in Limble would never
        # reach the tables. Read once instead, which stamps it, after which the
        # ordinary rules apply.
        stored = [("1", {
            "taskID": 1, "area_affected": "a", "condition_found": "b",
            "cause": "c", "action_taken": "d",
        })]
        client = self._StubClient({"1": self._acca("x")})
        task = {"taskID": 1, "dateCompleted": 1_750_000_000, "lastEdited": 1_750_000_000}
        service = self._service(client, self._StubRepo(stored))
        service._attach_instructions([task])
        self.assertEqual(client.asked, ["1"])
        self.assertIn(INSTRUCTIONS_READ_AT, task, "the re-read must leave a real stamp behind")

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

    def test_it_reads_by_default_and_can_be_turned_off(self):
        # On by default because the cost is the first walk through the history,
        # not the nightly run -- a flag nobody remembers would mean tables that
        # quietly stop being true.
        from services.ingestion_service import IngestionService

        client = self._StubClient({"1": self._acca("x")})
        IngestionService(
            limble_client=client, raw_repo=self._StubRepo([]), log=lambda _m: None
        )._attach_instructions([{"taskID": 1, "dateCompleted": 1}])
        self.assertEqual(client.asked, ["1"])

        off = self._StubClient({"1": self._acca("x")})
        service = IngestionService(
            limble_client=off, raw_repo=self._StubRepo([]),
            fetch_instructions=False, log=lambda _m: None,
        )
        self.assertEqual(service._attach_instructions([{"taskID": 1, "dateCompleted": 1}]), {})
        self.assertEqual(off.asked, [])

    def test_a_client_that_cannot_read_instructions_does_not_lose_the_sync(self):
        # The narrative is an addition to a sync, never the point of one.
        from services.ingestion_service import IngestionService

        class ListEndpointsOnly:
            pass

        service = IngestionService(
            limble_client=ListEndpointsOnly(), raw_repo=self._StubRepo([]), log=lambda _m: None
        )
        self.assertEqual(service._attach_instructions([{"taskID": 1, "dateCompleted": 1}]), {})

    def test_a_capped_backfill_advances_instead_of_rereading_its_own_head(self):
        # Plenty of completed work orders legitimately have no boxes filled in.
        # Selecting on "has a narrative" meant those were never satisfied, and
        # since the newest are read first they sat at the head of the queue --
        # so every capped run spent its whole budget on the same tasks and the
        # rest of the history was never reached.
        client = self._StubClient({})  # nothing has a narrative
        stored: dict[str, dict] = {}

        class Repo:
            def iter_task_payloads(self):
                yield from stored.items()

        tasks = [
            {"taskID": i, "dateCompleted": 1_750_000_000 + i, "lastEdited": 1_750_000_000 + i}
            for i in range(5)
        ]
        rounds = []
        for _ in range(3):
            client.asked.clear()
            service = self._service(client, Repo(), instructions_limit=2)
            service._attach_instructions(tasks)
            rounds.append(list(client.asked))
            for task in tasks:  # what the upsert would persist
                if INSTRUCTIONS_READ_AT in task:
                    stored[str(task["taskID"])] = dict(task)

        self.assertEqual(rounds, [["4", "3"], ["2", "1"], ["0"]])

    def test_a_task_edited_since_it_was_read_is_worth_asking_about_again(self):
        # A work order can be closed with its boxes blank and filled in later.
        read_at = 1_750_000_000
        stored = [("1", {"taskID": 1, INSTRUCTIONS_READ_AT: read_at})]
        client = self._StubClient({"1": self._acca("Infeed")})
        service = self._service(client, self._StubRepo(stored))

        service._attach_instructions([{"taskID": 1, "dateCompleted": 1, "lastEdited": read_at - 60}])
        self.assertEqual(client.asked, [], "unchanged since it was read")

        service._attach_instructions([{"taskID": 1, "dateCompleted": 1, "lastEdited": read_at + 60}])
        self.assertEqual(client.asked, ["1"], "edited since it was read")

    def test_a_nonsense_cap_reads_nothing_rather_than_everything(self):
        # Treating a negative cap as "no cap" would turn a flag whose purpose is
        # to bound the run into an unbounded walk of the whole history.
        client = self._StubClient({str(i): self._acca(str(i)) for i in range(5)})
        service = self._service(client, self._StubRepo([]), instructions_limit=-1)
        service._attach_instructions([{"taskID": i, "dateCompleted": 1_750_000_000} for i in range(5)])
        self.assertEqual(client.asked, [])

    def test_a_task_that_always_fails_does_not_block_the_backfill(self):
        # A deleted task, or one this key cannot see, returns the same error
        # every time. Left unstamped it stays at the head of a newest-first queue
        # and a capped run never reaches anything behind it -- the same stall as
        # a blank narrative, on the path that raises instead of returning.
        class Failing(InstructionFetchTests._StubClient):
            def get_task_instructions(self, task_id):
                super().get_task_instructions(task_id)
                if str(task_id) == "4":
                    raise RuntimeError("404 Not Found")
                return []

        client = Failing({})
        stored: dict[str, dict] = {}

        class Repo:
            def iter_task_payloads(self):
                yield from stored.items()

        tasks = [
            {"taskID": i, "dateCompleted": 1_750_000_000 + i, "lastEdited": 1_750_000_000 + i}
            for i in range(5)
        ]
        rounds = []
        for _ in range(5):
            client.asked.clear()
            self._service(client, Repo(), instructions_limit=1)._attach_instructions(tasks)
            rounds.append(list(client.asked))
            for task in tasks:
                if INSTRUCTIONS_READ_AT in task:
                    stored[str(task["taskID"])] = dict(task)

        self.assertEqual(rounds, [["4"], ["3"], ["2"], ["1"], ["0"]])
        self.assertTrue(tasks[4][INSTRUCTIONS_READ_ERROR], "the failure is recorded, not silently forgotten")

    def test_a_failed_read_is_eventually_tried_again(self):
        # Stamping is what stops a dead task blocking the queue, but stamping
        # alone would have meant an outage during a backfill quietly costing
        # those work orders their narrative for good -- the client has already
        # exhausted its own retries, yet "usually permanent" is not "always".
        from services.ingestion_service import RETRY_A_FAILED_READ_AFTER, _utc_now

        def asked_after(age: float) -> list:
            stored = {"1": {
                "taskID": 1,
                INSTRUCTIONS_READ_AT: _utc_now() - age,
                INSTRUCTIONS_READ_ERROR: "timed out",
            }}

            class Repo:
                def iter_task_payloads(self):
                    yield from stored.items()

            client = self._StubClient({"1": self._acca("Infeed")})
            self._service(client, Repo())._attach_instructions(
                [{"taskID": 1, "dateCompleted": 1_750_000_000, "lastEdited": 1_750_000_000}]
            )
            return client.asked

        self.assertEqual(asked_after(RETRY_A_FAILED_READ_AFTER / 2), [], "too soon to be worth a request")
        self.assertEqual(asked_after(RETRY_A_FAILED_READ_AFTER + 60), ["1"], "the outage is made good")

    def test_a_read_that_found_nothing_is_not_retried(self):
        # Only failures come back around. A work order whose boxes are genuinely
        # blank was read correctly, and asking again would cost a request a day
        # forever for an answer that is not going to change.
        from services.ingestion_service import RETRY_A_FAILED_READ_AFTER, _utc_now

        # Read long enough ago to be past the retry window, and untouched since.
        stored = {"1": {"taskID": 1, INSTRUCTIONS_READ_AT: _utc_now() - RETRY_A_FAILED_READ_AFTER * 10}}

        class Repo:
            def iter_task_payloads(self):
                yield from stored.items()

        client = self._StubClient({})
        self._service(client, Repo())._attach_instructions(
            [{"taskID": 1, "dateCompleted": 1_750_000_000, "lastEdited": 1_750_000_000}]
        )
        self.assertEqual(client.asked, [])

    def test_a_failed_read_is_retried_behind_everything_never_tried(self):
        # Recorded so it stops blocking, not abandoned: a transient outage during
        # a backfill must not quietly cost those tasks their narrative forever.
        stored = {"4": {"taskID": 4, INSTRUCTIONS_READ_AT: 1, INSTRUCTIONS_READ_ERROR: "boom"}}

        class Repo:
            def iter_task_payloads(self):
                yield from stored.items()

        client = self._StubClient({})
        # Task 4 is both the newest and long overdue a retry, so it is eligible --
        # the point is that being eligible does not put it in front.
        self._service(client, Repo(), instructions_limit=2)._attach_instructions([
            {"taskID": 4, "dateCompleted": 1_780_000_000, "lastEdited": 1},
            {"taskID": 3, "dateCompleted": 1_750_000_000, "lastEdited": 1},
            {"taskID": 2, "dateCompleted": 1_740_000_000, "lastEdited": 1},
        ])
        self.assertEqual(client.asked, ["3", "2"], "never-tried tasks come first")

    def test_a_box_cleared_in_limble_stops_being_shown(self):
        # A successful read saw the whole task, so a box it did not return was
        # cleared. The merge preserves what a payload omits, so the answer has to
        # be written empty or the tables keep showing evidence the work order no
        # longer makes.
        client = self._StubClient({"1": [
            {"instruction": "Affected area", "response": "Timing belt", "type": "text box"},
            {"instruction": "Condition", "response": "Worn", "type": "text box"},
        ]})
        task = {"taskID": 1, "dateCompleted": 1_750_000_000}
        self._service(client, self._StubRepo([]))._attach_instructions([task])
        self.assertEqual(task["area_affected"], "Timing belt")
        self.assertEqual(task["cause"], "", "a box with no answer is written empty, not left out")
        self.assertEqual(task["action_taken"], "")

    def test_a_capped_run_takes_the_most_recently_completed_first(self):
        # The boxes only exist on work orders closed since the template started
        # asking for them, so a capped run that took tasks in arrival order would
        # spend itself on history that predates the change and return nothing.
        client = self._StubClient({str(i): self._acca(str(i)) for i in range(4)})
        service = self._service(client, self._StubRepo([]), instructions_limit=2)
        service._attach_instructions([
            {"taskID": 0, "dateCompleted": 1_700_000_000},   # oldest
            {"taskID": 1, "dateCompleted": 1_780_000_000},   # newest
            {"taskID": 2, "dateCompleted": 1_750_000_000},
            {"taskID": 3, "dateCompleted": 1_770_000_000_000},  # newest, in ms
        ])
        self.assertEqual(client.asked, ["1", "3"])


class InstructionResponseShapeTests(unittest.TestCase):
    """What the client does with an answer that is not the promised list.

    The endpoint documents an array of instruction items. A successful call that
    returns something else is not an empty task, and the difference matters more
    here than it usually would: the reader treats an empty answer as proof the
    boxes were cleared, writes that over what it had, and records the task as
    read so it stops asking.
    """

    def _client_returning(self, payload):
        from integrations.limble import LimbleClient

        client = LimbleClient.__new__(LimbleClient)
        client._request = lambda *_args, **_kwargs: payload
        return client

    def test_an_unexpected_shape_is_an_error_rather_than_an_empty_task(self):
        from integrations.limble import LimbleResponseError

        for payload in ({"message": "scheduled maintenance"}, "down for maintenance", 503, None):
            with self.subTest(payload=type(payload).__name__):
                client = self._client_returning(payload)
                with self.assertRaises(LimbleResponseError):
                    client.get_task_instructions("4")

    def test_the_error_says_which_task_and_what_came_back(self):
        from integrations.limble import LimbleResponseError

        with self.assertRaises(LimbleResponseError) as caught:
            self._client_returning({"message": "nope"}).get_task_instructions("4")
        message = str(caught.exception)
        self.assertIn("4", message)
        self.assertIn("dict", message)

    def test_a_task_with_no_instructions_is_still_an_ordinary_empty_read(self):
        # _request answers an empty body with [], and a task that genuinely has
        # no instructions answers with []. Neither is an error.
        self.assertEqual(self._client_returning([]).get_task_instructions("4"), [])

    def test_junk_alongside_real_items_is_dropped_rather_than_raised(self):
        # The read plainly succeeded; one unusable entry does not undo that.
        items = self._client_returning(
            [{"instruction": "Cause", "response": "Wear"}, None, "?", 7]
        ).get_task_instructions("4")
        self.assertEqual(items, [{"instruction": "Cause", "response": "Wear"}])

    def test_an_array_of_nothing_usable_is_an_error_rather_than_an_empty_task(self):
        # The same mistake as the outer shape, one level in: an array that
        # cannot be instructions is not a task whose boxes are blank.
        from integrations.limble import LimbleResponseError

        for payload in (["scheduled maintenance"], [None], [None, "x", 7], [[]]):
            with self.subTest(payload=payload):
                with self.assertRaises(LimbleResponseError):
                    self._client_returning(payload).get_task_instructions("4")

    def test_that_error_says_how_many_came_back_and_what_they_were(self):
        from integrations.limble import LimbleResponseError

        with self.assertRaises(LimbleResponseError) as caught:
            self._client_returning(["scheduled maintenance"]).get_task_instructions("4")
        message = str(caught.exception)
        self.assertIn("1 item(s)", message)
        self.assertIn("str", message)

    def test_only_the_shape_is_judged_here_never_the_content(self):
        # A template that never asked for the four boxes returns perfectly good
        # instruction objects carrying none of them. That is a blank narrative,
        # not a failed read, and it must come back normally.
        items = self._client_returning(
            [{"instruction": "Does this job require LOTO?", "response": "No"},
             {"instruction": "Complete work to solve problem", "response": "1"}]
        ).get_task_instructions("4")
        self.assertEqual(len(items), 2)
        self.assertEqual(extract_narrative({"instructions": items}), {})
        # Nor is an empty object judged: indistinguishable from the above.
        self.assertEqual(self._client_returning([{}]).get_task_instructions("4"), [{}])

    def test_an_all_junk_array_does_not_clear_what_was_already_read(self):
        from services.ingestion_service import INSTRUCTIONS_READ_AT
        from repositories.raw_repo import INSTRUCTIONS_READ_ERROR

        outer = self

        class Wrapped(InstructionFetchTests._StubClient):
            def get_task_instructions(self, task_id):
                self.asked.append(str(task_id))
                return outer._client_returning(["scheduled maintenance"]).get_task_instructions(task_id)

        task = {
            "taskID": 1,
            "dateCompleted": 1_750_000_000,
            "area_affected": "Timing belt",
            "cause": "Aging",
        }
        service = InstructionFetchTests()._service(Wrapped({}), InstructionFetchTests._StubRepo([]))
        counts = service._attach_instructions([task])

        self.assertEqual(task["area_affected"], "Timing belt")
        self.assertEqual(task["cause"], "Aging")
        self.assertIn("none of them instruction objects", task[INSTRUCTIONS_READ_ERROR])
        self.assertIn(INSTRUCTIONS_READ_AT, task, "still stamped, so it cannot block the queue")
        self.assertEqual(counts["instructions_failed"], 1)

    def test_an_unreadable_response_does_not_clear_what_was_already_read(self):
        # The whole point of raising: an unreadable answer has to reach the phase
        # as a failure, so the narrative survives and the read is tried again.
        from services.ingestion_service import INSTRUCTIONS_READ_AT
        from repositories.raw_repo import INSTRUCTIONS_READ_ERROR

        class Wrapped(InstructionFetchTests._StubClient):
            def get_task_instructions(self, task_id):
                self.asked.append(str(task_id))
                from integrations.limble import LimbleClient

                client = LimbleClient.__new__(LimbleClient)
                client._request = lambda *_a, **_k: {"message": "scheduled maintenance"}
                return LimbleClient.get_task_instructions(client, task_id)

        task = {
            "taskID": 1,
            "dateCompleted": 1_750_000_000,
            "area_affected": "Timing belt",
            "cause": "Aging",
        }
        service = InstructionFetchTests()._service(Wrapped({}), InstructionFetchTests._StubRepo([]))
        counts = service._attach_instructions([task])

        self.assertEqual(task["area_affected"], "Timing belt", "an unread task keeps its narrative")
        self.assertEqual(task["cause"], "Aging")
        self.assertIn("expected a list", task[INSTRUCTIONS_READ_ERROR])
        self.assertIn(INSTRUCTIONS_READ_AT, task, "still stamped, so it cannot block the queue")
        self.assertEqual(counts["instructions_failed"], 1)


class InstructionCheckpointTests(unittest.TestCase):
    """Reads are written down as the phase goes, not only when it finishes."""

    def _service(self, client, repo, **kwargs):
        return InstructionFetchTests()._service(client, repo, **kwargs)

    def _completed_tasks(self, count):
        return [{"taskID": n, "dateCompleted": 1_750_000_000 - n} for n in range(count)]

    def test_a_long_phase_writes_as_it_goes(self):
        from services.ingestion_service import _INSTRUCTIONS_CHECKPOINT_EVERY

        every = _INSTRUCTIONS_CHECKPOINT_EVERY
        client = InstructionFetchTests._StubClient({})
        repo = InstructionFetchTests._StubRepo([])
        self._service(client, repo)._attach_instructions(self._completed_tasks(every * 2 + 3))

        sizes = [len(chunk) for chunk in repo.checkpoints]
        self.assertEqual(sizes, [every, every, 3], "two full chunks, then the remainder")

    def test_a_short_phase_still_writes_once_at_the_end(self):
        client = InstructionFetchTests._StubClient({"1": self._acca()})
        repo = InstructionFetchTests._StubRepo([])
        self._service(client, repo)._attach_instructions([{"taskID": 1, "dateCompleted": 1_750_000_000}])

        self.assertEqual(len(repo.checkpoints), 1)
        self.assertEqual(list(repo.checkpoints[0]), ["1"])

    def test_a_checkpoint_carries_the_answers_and_the_stamp(self):
        from services.ingestion_service import INSTRUCTIONS_READ_AT

        client = InstructionFetchTests._StubClient({"1": self._acca()})
        repo = InstructionFetchTests._StubRepo([])
        self._service(client, repo)._attach_instructions([{"taskID": 1, "dateCompleted": 1_750_000_000}])

        written = repo.checkpoints[0]["1"]
        self.assertEqual(written["area_affected"], "Timing belt")
        self.assertIn(INSTRUCTIONS_READ_AT, written)

    def test_an_interrupted_phase_keeps_what_it_had_already_written(self):
        from services.ingestion_service import _INSTRUCTIONS_CHECKPOINT_EVERY

        every = _INSTRUCTIONS_CHECKPOINT_EVERY

        class Dies(InstructionFetchTests._StubClient):
            def get_task_instructions(self, task_id):
                if len(self.asked) >= every + 2:
                    raise KeyboardInterrupt("worker timed out")
                return super().get_task_instructions(task_id)

        repo = InstructionFetchTests._StubRepo([])
        with self.assertRaises(KeyboardInterrupt):
            self._service(Dies({}), repo)._attach_instructions(self._completed_tasks(every * 2))

        self.assertEqual([len(chunk) for chunk in repo.checkpoints], [every])

    def test_a_dry_run_reads_but_writes_nothing(self):
        client = InstructionFetchTests._StubClient({"1": self._acca()})
        repo = InstructionFetchTests._StubRepo([])
        task = {"taskID": 1, "dateCompleted": 1_750_000_000}
        self._service(client, repo)._attach_instructions([task], checkpoint=False)

        self.assertEqual(client.asked, ["1"], "the phase still runs")
        self.assertEqual(task["area_affected"], "Timing belt", "and still reports what it read")
        self.assertEqual(repo.checkpoints, [], "but a dry run must not touch the database")

    def test_a_checkpoint_that_fails_does_not_lose_the_sync(self):
        # Insurance must not become the risk: the run's own upsert writes these
        # same fields, so a locked database costs repeated requests, not a sync.
        class Refuses(InstructionFetchTests._StubRepo):
            def record_instruction_read(self, reads):
                raise RuntimeError("database is locked")

        logged: list[str] = []
        client = InstructionFetchTests._StubClient({"1": self._acca()})
        service = self._service(client, Refuses([]))
        service._log = logged.append
        task = {"taskID": 1, "dateCompleted": 1_750_000_000}
        counts = service._attach_instructions([task])

        self.assertEqual(task["area_affected"], "Timing belt")
        self.assertEqual(counts["instructions_found"], 1)
        self.assertTrue(any("database is locked" in line for line in logged), logged)

    def test_a_repository_without_the_checkpoint_still_syncs(self):
        # An older repository, or a stand-in that only knows the read side.
        class Older:
            def iter_task_payloads(self):
                return iter(())

        client = InstructionFetchTests._StubClient({"1": self._acca()})
        task = {"taskID": 1, "dateCompleted": 1_750_000_000}
        counts = self._service(client, Older())._attach_instructions([task])
        self.assertEqual(counts["instructions_found"], 1)

    def _acca(self):
        return InstructionFetchTests()._acca("Timing belt")


class EndToEndTests(unittest.TestCase):
    """A real sync, from the API payload to what the disposition table reads.

    Everything between is exercised for real: the instructions fetch, the
    distillation onto the task, the raw upsert, the mapping into
    mapped_cmms_record, and the query the disposition page runs. Only the browser
    is absent -- the JS renders whatever these rows carry.
    """

    class _Client:
        """A Limble client serving one completed corrective work order."""

        TASK = {
            "taskID": 239728,
            "assetID": 2288,
            "Asset Number": "2288",
            "type": "6",
            "name": "Worn magazine timing belt /WIP5F6",
            "status": "Completed",
            "dateCompleted": 1753000000,
            "createdDate": 1750000000,
            "downtime": 108900,
            "completionNotes": "Replaced timing belt. RTS.",
        }

        # The live shape, keys exactly as GET /tasks/{id}/instructions returns
        # them, with the four boxes at the end of the safety checklist.
        INSTRUCTIONS = [
            {"instruction": "Does this job require LOTO?", "response": "No", "type": "option list",
             "instructionID": 6, "parentInstructionID": 2, "options": [], "instructionFiles": []},
            {"instruction": "Complete work to solve problem", "response": "1", "type": "checkbox",
             "instructionID": 3, "parentInstructionID": 0, "options": [], "instructionFiles": []},
            {"instruction": "Affected area", "response": "Timing belt", "type": "text box",
             "instructionID": 48, "parentInstructionID": 47, "options": [], "instructionFiles": []},
            {"instruction": "Condition", "response": "Worn", "type": "text box",
             "instructionID": 49, "parentInstructionID": 47, "options": [], "instructionFiles": []},
            {"instruction": "Cause", "response": "Aging", "type": "text box",
             "instructionID": 50, "parentInstructionID": 47, "options": [], "instructionFiles": []},
            {"instruction": "Action", "response": "<div>Replaced timing belt.</div>", "type": "text box",
             "instructionID": 51, "parentInstructionID": 47, "options": [], "instructionFiles": []},
        ]

        def get_tasks(self, updated_since=None, *, on_page=None):
            return [dict(self.TASK)]

        def get_assets(self, *, on_page=None):
            return [{"assetID": 2288, "name": "Mazak CNC Horizontal Machining Center"}]

        def get_task_instructions(self, task_id):
            return [dict(item) for item in self.INSTRUCTIONS]

    def test_a_sync_puts_the_narrative_where_the_disposition_table_reads_it(self):
        from repositories.raw_repo import RawRepository
        from services.ingestion_service import IngestionService

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db_path = Path(tmp.name) / "gremlin.db"
        LifeDataService(db_path, refresh_on_startup=False)  # build the schema

        summary = IngestionService(
            limble_client=self._Client(),
            raw_repo=RawRepository(db_path),
            fetch_instructions=True,
            log=lambda _message: None,
        ).sync_all()

        self.assertEqual(summary["instructions_fetched"], 1)
        self.assertEqual(summary["instructions_found"], 1)

        # The stored payload carries the four answers, not the 6-item checklist.
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            stored = json.loads(conn.execute("SELECT raw_json FROM raw_cmms_record").fetchone()["raw_json"])
        self.assertEqual(stored["cause"], "Aging")
        self.assertNotIn("instructions", stored)

        # And the disposition page's own query returns them, markup stripped.
        rows = LifeDataService(db_path, refresh_on_startup=False).disposition_rows("2288", "wo")
        self.assertEqual(len(rows), 1, "the work order should be eligible for WO disposition")
        row = rows[0]
        self.assertEqual(row["area_affected"], "Timing belt")
        self.assertEqual(row["condition_found"], "Worn")
        self.assertEqual(row["cause"], "Aging")
        self.assertEqual(row["action_taken"], "Replaced timing belt.")

    def test_an_interrupted_sync_keeps_the_instructions_it_had_already_read(self):
        # The checkpoint's only job, through the real repository rather than a
        # stand-in: reads survive a run that never reaches its own upsert. A
        # stubbed repo cannot show this, because a checkpoint that quietly wrote
        # nothing would look identical -- the end-of-run upsert stores the same
        # fields, so the loss is only visible when the run does not get there.
        from repositories.raw_repo import RawRepository
        from services.ingestion_service import (
            INSTRUCTIONS_READ_AT,
            _INSTRUCTIONS_CHECKPOINT_EVERY,
            IngestionService,
        )

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db_path = Path(tmp.name) / "gremlin.db"
        LifeDataService(db_path, refresh_on_startup=False)

        every = _INSTRUCTIONS_CHECKPOINT_EVERY
        instructions = self._Client.INSTRUCTIONS

        class ManyTasks(EndToEndTests._Client):
            """Enough completed work orders to cross a checkpoint, then dies."""

            def __init__(self):
                self.asked: list = []

            def get_tasks(self, updated_since=None, *, on_page=None):
                return [
                    dict(self.TASK, taskID=239728 + n, dateCompleted=1753000000 - n)
                    for n in range(every * 2)
                ]

            def get_task_instructions(self, task_id):
                if len(self.asked) >= every + 2:
                    raise KeyboardInterrupt("worker timed out")
                self.asked.append(task_id)
                return [dict(item) for item in instructions]

        # A first sync writes the task rows, without reading any instructions.
        IngestionService(
            limble_client=ManyTasks(), raw_repo=RawRepository(db_path),
            fetch_instructions=False, log=lambda _m: None,
        ).sync_all()

        killed = ManyTasks()
        with self.assertRaises(KeyboardInterrupt):
            IngestionService(
                limble_client=killed, raw_repo=RawRepository(db_path),
                fetch_instructions=True, log=lambda _m: None,
            ).sync_all()

        stored = dict(RawRepository(db_path).iter_task_payloads())
        kept = [key for key, payload in stored.items() if payload.get("cause")]
        self.assertEqual(
            len(kept), every,
            f"the {every} reads written down before the kill should have survived it",
        )
        for key in kept:
            self.assertIn(INSTRUCTIONS_READ_AT, stored[key], "and be marked read, so nobody pays twice")

        # Which is the point: the next run picks up where this one stopped. With
        # the checkpointed tasks behind it there are few enough left that this
        # one finishes rather than being killed.
        resumed = ManyTasks()
        IngestionService(
            limble_client=resumed, raw_repo=RawRepository(db_path),
            fetch_instructions=True, log=lambda _m: None,
        ).sync_all()
        self.assertFalse(
            set(resumed.asked) & {int(key) for key in kept},
            "a resumed run must not re-read what the killed one already stored",
        )
        self.assertEqual(len(resumed.asked), every, "it reads exactly the ones still outstanding")

    def test_a_second_sync_does_not_pay_to_read_the_same_task_again(self):
        from repositories.raw_repo import RawRepository
        from services.ingestion_service import IngestionService

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db_path = Path(tmp.name) / "gremlin.db"
        LifeDataService(db_path, refresh_on_startup=False)

        client = self._Client()
        asked: list = []
        original = client.get_task_instructions
        client.get_task_instructions = lambda task_id: (asked.append(task_id), original(task_id))[1]

        for _ in range(2):
            IngestionService(
                limble_client=client,
                raw_repo=RawRepository(db_path),
                fetch_instructions=True,
                log=lambda _message: None,
            ).sync_all()

        self.assertEqual(len(asked), 1, "the second sync should skip a task already answered")
        rows = LifeDataService(db_path, refresh_on_startup=False).disposition_rows("2288", "wo")
        self.assertEqual(rows[0]["cause"], "Aging", "a re-sync must not blank what it did not re-fetch")
