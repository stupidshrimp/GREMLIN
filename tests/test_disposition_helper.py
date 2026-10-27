"""The disposition helper: how it reads work orders, what it suggests, and what its batch runs save.

The plant is the synthetic 4002 paint line of ``disposition_helper_corpus``:
work orders written the way technicians write them, each with the disposition
an engineer gave it, so a test knows what the helper should say.
"""

import importlib
import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

from services import disposition_helper as dh
from services import disposition_helper_text as text
from services import disposition_standard as std
from services.life_data_service import LifeDataService
from services.population_analysis_service import PopulationAnalysisService
from services.standard_disposition_schema import ensure_standard_disposition_schema
from services.standard_disposition_service import StandardDispositionService
from tests import disposition_helper_corpus as corpus

PFS = corpus.PFS
S09, S14, S16 = "4002-S09", "4002-S14", "4002-S16"


def _coded(truth):
    return {key: value for key, value in truth.items() if key != "scenario"}


class ReadingTests(unittest.TestCase):
    """How a work order's words become features and evidence."""

    def setUp(self) -> None:
        self.lexicon = text.build_lexicon(std.CodeBook.from_seed())

    def keys(self, **texts):
        return {(m.key, m.negated) for m in text.read_work_order(texts, self.lexicon).mentions}

    def test_word_forms_meet_and_part_names_keep_their_ending(self):
        for words in (("replace", "replaced", "replacing"), ("leak", "leaks", "leaking", "leaked"),
                      ("clog", "clogged"), ("trip", "tripped", "trips"), ("valve", "valves"), ("hose", "hoses")):
            self.assertEqual(len({text.stem(word) for word in words}), 1, words)
        self.assertEqual(text.stem("bearings"), "bearing")
        self.assertEqual(text.stem("fittings"), "fitting")
        self.assertEqual(text.stem("speed"), "speed")
        self.assertEqual(text.stem("s09"), "s09")

    def test_different_wording_reads_as_one_code(self):
        for phrase in ("filter clogged", "filter plugged up", "filter blocked", "restricted filter"):
            self.assertIn(("FAILURE_MECHANISM:PLUG", False), self.keys(condition_found=phrase), phrase)
        for title in ("Oven won't light", "oven will not light", "no ignition on oven", "burner wont fire"):
            self.assertIn(("FAILURE_MODE:FTS", False), self.keys(task_name=title), title)
        self.assertIn(("ACTION:REPLACE", False), self.keys(action_taken="Swapped out the motor"))
        self.assertIn(("COMPONENT:Bearing", False), self.keys(condition_found="pillow block rough"))

    def test_a_code_is_recognised_by_its_own_name(self):
        codebook = std.CodeBook([*std.CodeBook.from_seed().entries(std.COMPONENT),
                                 std.CodeEntry(std.COMPONENT, "Rotor", "Rotor / spindle", sort_order=999)])
        lexicon = text.build_lexicon(codebook)
        keys = {m.key for m in text.read_work_order({"condition_found": "spindle seized"}, lexicon).mentions}
        self.assertIn("COMPONENT:Rotor", keys)

    def test_a_negated_condition_is_evidence_against_it(self):
        keys = self.keys(condition_found="Checked for leaks, no leaks found. Belt worn")
        self.assertIn(("FAILURE_MECHANISM:LEAK", True), keys)
        self.assertIn(("FAILURE_MECHANISM:WEAR", False), keys)
        # The negation stops at the end of its clause.
        self.assertNotIn(("FAILURE_MECHANISM:WEAR", True), keys)

    def test_no_fault_found_is_a_phrase_not_a_negation(self):
        keys = self.keys(action_taken="Tested, no fault found")
        self.assertIn(("FAILURE_MODE:NFF", False), keys)
        self.assertIn(("CATEGORY:NON_FAILURE", False), keys)

    def test_unknown_and_other_are_never_read_off_a_work_order(self):
        keys = {key for key, _ in self.keys(condition_found="other unknown issue")}
        self.assertFalse({"COMPONENT:Other", "COMPONENT:Unknown", "FAILURE_MECHANISM:UNK"} & keys)

    def test_a_misspelling_is_read_as_the_word_the_plant_uses(self):
        speller = text.Speller({"bearing": 40, "replac": 50, "conveyor": 30})
        self.assertEqual(speller.correct("bearng"), "bearing")
        self.assertEqual(speller.correct("conveyer"), "conveyor")
        # Nothing one edit away, a short word, or a part number: left alone.
        self.assertEqual(speller.correct("gizmo"), "gizmo")
        self.assertEqual(speller.correct("6205rs"), "6205rs")


class CalibrationTests(unittest.TestCase):
    def test_the_curve_rises_with_the_score_and_never_reads_certain(self):
        pairs = [(0.1, False)] * 8 + [(0.1, True)] * 2 + [(0.5, True)] * 6 + [(0.5, False)] * 4 + [(0.9, True)] * 20
        curve = dh.Isotonic.fit(pairs)
        self.assertAlmostEqual(curve(0.1), 3 / 12)
        self.assertAlmostEqual(curve(0.5), 7 / 12)
        self.assertAlmostEqual(curve(0.9), 21 / 22)
        self.assertLess(curve(0.95), 1.0)
        # Below anything it was fitted on, it falls away rather than borrowing
        # the lowest block's rate.
        self.assertAlmostEqual(curve(0.05), 3 / 24)

    def test_scores_out_of_order_are_pooled(self):
        curve = dh.Isotonic.fit([(0.2, True), (0.4, False), (0.6, True), (0.8, True)])
        self.assertLessEqual(curve(0.2), curve(0.4))
        self.assertLessEqual(curve(0.4), curve(0.6))

    def test_without_samples_a_score_is_discounted(self):
        self.assertAlmostEqual(dh.Isotonic()(0.8), 0.8 * dh.UNCALIBRATED_DISCOUNT)


class HelperFixture(unittest.TestCase):
    decided_share = 0.7

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.addCleanup(dh.reset_cache)
        background = dh.BACKGROUND_CALIBRATION
        dh.BACKGROUND_CALIBRATION = False
        self.addCleanup(setattr, dh, "BACKGROUND_CALIBRATION", background)
        self.life = LifeDataService(Path(tmp.name) / "gremlin.db", refresh_on_startup=False)
        self.std = StandardDispositionService(self.life)
        self.helper = dh.DispositionHelper(self.std)
        self.truth = corpus.build_plant(self.life, per_scenario=6)
        self.ids = list(self.truth)
        cut = int(len(self.ids) * self.decided_share)
        self.decided, self.backlog = self.ids[:cut], self.ids[cut:]
        self.std.save_dispositions([{"mapped_record_id": i, **_coded(self.truth[i])} for i in self.decided], decided_by="tim")

    def add_work_order(self, mapped_record_id, title, *, condition=None, action=None, notes=None, record_class="CORRECTIVE_WO",
                       created="2026-01-05", asset=PFS, asset_name=f"{PFS} {corpus.PFS_NAME}"):
        corpus.insert_records(self.life, [{
            "mapped_record_id": mapped_record_id, "asset_number": asset, "asset_name": asset_name,
            "asset_id": corpus.ASSET_IDS.get(asset, "999"), "task_name": title, "request_title": title,
            "requestor_description": None, "condition_found": condition, "cause": None, "action_taken": action,
            "completion_notes": notes, "created": created, "record_class": record_class,
        }])
        return mapped_record_id

    def calibrate(self):
        corpus_, labels, codebook = self.helper.knowledge(token=self.life.change_token())
        with self.life.connect() as conn:
            tree = self.std.asset_tree(conn)
        return self.helper._calibration(corpus_, labels, codebook, tree, wait=True)


class SuggestionTests(HelperFixture):
    def test_a_clear_work_order_is_read_field_by_field(self):
        record = self.add_work_order(
            50001, "S09 dry off oven won't light", condition="combustion air filter clogged with dust",
            action="cleaned filter and relit burner",
        )
        self.calibrate()
        suggestion = self.helper.suggest(record)
        values = {key: field["value"] for key, field in suggestion["fields"].items() if key in dh.SCORED_FIELDS}
        self.assertEqual(values, {
            "disposition_category": "FAILURE", "sub_asset": S09, "component_code": "Filter",
            "failure_mechanism_code": "CONT", "failure_mode_code": "FTS", "action_code": "CLEAN_SERVICE",
        })
        self.assertTrue(suggestion["calibrated"])
        # "Clogged" reads as plugged where the similar work orders say
        # contaminated: the mechanism is the field it is least sure of, and the
        # whole disposition is no surer than that.
        confidences = {key: suggestion["fields"][key]["confidence"] for key in dh.SCORED_FIELDS}
        self.assertEqual(min(confidences, key=confidences.get), "failure_mechanism_code")
        self.assertLessEqual(suggestion["overall_confidence"], confidences["failure_mechanism_code"])
        self.assertGreater(suggestion["overall_confidence"], 0.5)
        for key in dh.SCORED_FIELDS:
            for alternative in suggestion["fields"][key]["alternatives"]:
                self.assertLess(alternative["confidence"], confidences[key])
        # The disposition it proposes passes the standard's own checks as it stands.
        payload = suggestion["disposition"]
        self.assertEqual(payload["evidence"], "EXPLICIT")
        self.assertIsNone(payload["failure_cause_code"])
        self.assertEqual(self.std.save_dispositions([payload], decided_by="tim"), 1)
        # Its reasons quote the work order, and the similar work orders are listed.
        self.assertIn("similar work orders", " ".join(suggestion["fields"]["failure_mechanism_code"]["reasons"]))
        self.assertIn("“air filter” in the condition found", suggestion["fields"]["component_code"]["reasons"])
        self.assertTrue(suggestion["similar"])
        self.assertTrue(all(item["mapped_record_id"] != record for item in suggestion["similar"]))

    def test_a_sub_asset_named_outright_is_read_without_any_votes(self):
        # Nothing like it has been dispositioned: the votes are no help, but the
        # work order names the sub-asset by number.
        record = self.add_work_order(50002, "S16 gizmo flange wobbles")
        suggestion = self.helper.suggest(record)
        self.assertEqual(suggestion["fields"]["sub_asset"]["value"], S16)
        self.assertIn("“S16” in the work order", suggestion["fields"]["sub_asset"]["reasons"])
        # By name, too.
        record = self.add_work_order(50003, "Gizmo flange wobbles on the oven conveyor")
        self.assertEqual(self.helper.suggest(record)["fields"]["sub_asset"]["value"], S14)

    def test_no_fault_found_is_never_suggested_for_a_failure(self):
        record = self.add_work_order(50004, "Dry-off oven won't light", action="checked, no fault found")
        suggestion = self.helper.suggest(record)
        if suggestion["fields"]["disposition_category"]["value"] == std.FAILURE:
            self.assertNotEqual(suggestion["fields"]["failure_mode_code"]["value"], "NFF")

    def test_its_own_disposition_is_never_a_vote(self):
        record = self.decided[0]
        corpus_, labels, codebook = self.helper.knowledge(token=self.life.change_token())
        self.assertIn(record, labels)
        suggestion = self.helper.suggest(record)
        self.assertNotIn(record, [item["mapped_record_id"] for item in suggestion["similar"]])

    def test_a_new_decision_is_learnt_at_once(self):
        first = self.add_work_order(50005, "Zorblax unit humming", action="replaced zorblax")
        second = self.add_work_order(50006, "Zorblax unit humming again", action="replaced zorblax")
        before = self.helper.suggest(second)
        self.assertNotIn(first, [item["mapped_record_id"] for item in before["similar"]])
        self.std.save_dispositions([{
            "mapped_record_id": first, "disposition_category": "FAILURE", "sub_asset": S16,
            "component_code": "Motor", "failure_mechanism_code": "WEAR", "failure_mode_code": "VIB",
            "action_code": "REPLACE", "evidence": "INFERRED",
        }], decided_by="tim")
        after = self.helper.suggest(second)
        self.assertEqual(after["similar"][0]["mapped_record_id"], first)
        self.assertEqual(after["fields"]["component_code"]["value"], "Motor")

    def test_new_work_order_text_is_indexed_after_a_sync(self):
        corpus_before, _, _ = self.helper.knowledge(token=self.life.change_token())
        self.add_work_order(50007, "Brand new kind of fault")
        corpus_after, _, _ = self.helper.knowledge(token=self.life.change_token())
        self.assertIsNot(corpus_before, corpus_after)
        self.assertIn(50007, corpus_after.position)
        # A save changes no text: the index is kept.
        self.std.save_dispositions([{"mapped_record_id": 50007, "disposition_category": "HELD",
                                     "disposition_note": "no detail"}])
        self.assertIs(self.helper.knowledge(token=self.life.change_token())[0], corpus_after)

    def test_a_likely_duplicate_is_pointed_out(self):
        self.add_work_order(50008, "Prime booth exhaust fan down", condition="fan belt broken",
                            action="replaced belt", created="2026-02-01")
        record = self.add_work_order(50009, "Prime booth exhaust fan down", condition="fan belt broken",
                                     action="replaced belt", created="2026-02-02")
        suggestion = self.helper.suggest(record)
        self.assertEqual(suggestion["possible_duplicates"][0]["mapped_record_id"], 50008)
        self.assertEqual(suggestion["possible_duplicates"][0]["days_apart"], 1)
        # The category is still one the helper may propose: the engineer decides
        # whether it is a duplicate.
        self.assertIn(suggestion["fields"]["disposition_category"]["value"], std.CODED_CATEGORIES)

    def test_a_pm_reset_names_what_it_renewed(self):
        record = self.add_work_order(50010, "S09 Monthly PM - change dry-off oven filters", action="changed filters",
                                     record_class="PM")
        suggestion = self.helper.suggest(record)
        self.assertEqual(suggestion["disposition"]["disposition_category"], std.PM_RESET)
        self.assertEqual(suggestion["disposition"]["renewed_items"], ["Filter"])

    def test_a_cause_is_overridden_only_when_the_work_order_says_so(self):
        record = self.add_work_order(50011, "S11 exhaust fan belt broke", condition="belt snapped, wrong part installed",
                                     action="replaced belt")
        cause = self.helper.suggest(record)["fields"]["failure_cause_code"]
        if cause["value"] is not None:
            self.assertEqual(cause["value"], "MNT-ERR")
            self.assertIn("wrong part", cause["note"])
        plain = self.helper.suggest(self.add_work_order(50012, "S11 exhaust fan belt broke", action="replaced belt"))
        self.assertIsNone(plain["fields"]["failure_cause_code"]["value"])


class AccuracyTests(HelperFixture):
    def test_the_report_replays_decided_work_orders(self):
        report = self.helper.evaluate(PFS)
        self.assertEqual(report["replayed"], len(self.decided))
        self.assertTrue(report["enough"])
        by_field = {row["field"]: row for row in report["fields"]}
        self.assertEqual(set(by_field), set(dh.SCORED_FIELDS))
        for row in report["fields"]:
            self.assertGreaterEqual(row["accuracy"], 0.8, row)
            self.assertEqual([item["threshold"] for item in row["at"]], list(dh.EVALUATION_THRESHOLDS))
        coverage = [row["coverage"] for row in report["thresholds"]]
        self.assertEqual(coverage, sorted(coverage, reverse=True))
        self.assertEqual(report["in_use"]["assisted_saves"], 0)

    def test_in_use_counts_kept_suggestions_and_confirmed_auto_dispositions(self):
        record = self.backlog[0]
        suggestion = self.helper.suggest(record)
        kept = dict(suggestion["disposition"])
        kept["helper_suggestion"] = json.loads(self.helper.suggestion_record(suggestion))
        self.std.save_dispositions([kept], decided_by="tim")
        use = self.helper.in_use(PFS)
        self.assertEqual(use["assisted_saves"], 1)
        category = next(item for item in use["kept"] if item["field"] == "disposition_category")
        self.assertEqual((category["kept"], category["of"]), (1, 1))


class BatchTests(HelperFixture):
    def test_a_preview_saves_nothing(self):
        before = self.std.disposition_page(PFS)["counts"]
        preview = self.helper.batch_preview(PFS, min_confidence=0.6)
        self.assertEqual(preview["considered"], len(self.backlog))
        self.assertTrue(preview["calibrated"])
        self.assertGreater(preview["would_apply"], 0)
        self.assertEqual(sum(band["count"] for band in preview["histogram"]), len(self.backlog))
        self.assertEqual(self.std.disposition_page(PFS)["counts"], before)

    def test_a_run_saves_auto_dispositions_that_can_be_undone(self):
        preview = self.helper.batch_preview(PFS, min_confidence=0.6)
        result = self.helper.batch_apply(PFS, min_confidence=0.6, run_by="tim")
        self.assertEqual(result["applied"], preview["would_apply"])
        run_id = result["helper_run_id"]
        page = self.std.disposition_page(PFS, scope="auto", page_size=500)
        self.assertEqual(page["counts"]["auto"], result["applied"])
        self.assertEqual(len(page["rows"]), result["applied"])
        row = page["rows"][0]["disposition"]
        self.assertEqual(row["source"], "AUTO")
        self.assertEqual(row["helper_run_id"], run_id)
        self.assertEqual(row["decided_by"], "Disposition helper (tim)")
        self.assertRegex(row["review_flag"], r"at \d+% confidence")
        # The suggestion is kept with it.
        with self.life.connect() as conn:
            kept = conn.execute(
                "SELECT suggestion_json FROM wo_disposition WHERE is_current = 1 AND helper_run_id = ? LIMIT 1", (run_id,)
            ).fetchone()[0]
        self.assertIn("overall", json.loads(kept))

        # The helper never learns from its own unconfirmed dispositions.
        _, labels, _ = self.helper.knowledge(token=self.life.change_token())
        self.assertNotIn(page["rows"][0]["mapped_record_id"], labels)

        # An engineer confirms one (saves it again) before the run is undone.
        confirmed = page["rows"][0]
        values = {key: confirmed["disposition"][key] for key in (
            "disposition_category", "sub_asset", "component_code", "failure_mechanism_code", "failure_mode_code",
            "action_code", "evidence", "failure_cause_code", "renewed_items", "disposition_note")}
        self.std.save_dispositions([{"mapped_record_id": confirmed["mapped_record_id"], **values}], decided_by="jose")
        self.assertEqual(self.helper.in_use(PFS)["auto_confirmed_unchanged"], 1)

        runs = self.helper.runs(PFS)
        self.assertEqual(runs[0]["still_auto"], result["applied"] - 1)
        undone = self.helper.undo_run(run_id, undone_by="tim")
        self.assertEqual(undone, {"helper_run_id": run_id, "undone": result["applied"] - 1, "kept": 1})
        counts = self.std.disposition_page(PFS)["counts"]
        self.assertEqual(counts["auto"], 0)
        self.assertEqual(counts["not_reviewed"], len(self.backlog) - 1)
        kept_record = self.std.disposition_record(confirmed["mapped_record_id"])["disposition"]
        self.assertEqual((kept_record["source"], kept_record["decided_by"]), ("ENGINEER", "jose"))
        with self.assertRaises(ValueError):
            self.helper.undo_run(run_id)

    def test_undo_puts_back_what_was_there_before(self):
        record = self.backlog[0]
        self.std.save_dispositions([{"mapped_record_id": record, "disposition_category": "NOT_REVIEWED",
                                     "disposition_note": "looked, undecided"}], decided_by="tim")
        result = self.std.save_auto_dispositions(
            [{"mapped_record_id": record, **_coded(self.truth[record]), "review_flag": "Auto"}],
            run={"asset_number": PFS, "min_confidence": 0.9}, decided_by="Disposition helper",
        )
        self.assertEqual(result["applied"], 1)
        self.std.undo_helper_run(result["helper_run_id"])
        restored = self.std.disposition_record(record)["disposition"]
        self.assertEqual((restored["disposition_category"], restored["disposition_note"]),
                         ("NOT_REVIEWED", "looked, undecided"))
        history = self.std.disposition_history(record)
        self.assertEqual([row["source"] for row in history], ["AUTO", "ENGINEER"])
        self.assertEqual([row["is_current"] for row in history], [0, 1])

    def test_undoing_a_later_run_never_brings_back_an_earlier_undone_one(self):
        first = self.helper.batch_apply(PFS, min_confidence=0.6, run_by="tim")
        self.helper.undo_run(first["helper_run_id"])
        second = self.helper.batch_apply(PFS, min_confidence=0.6, run_by="tim")
        self.assertEqual(second["applied"], first["applied"])
        self.helper.undo_run(second["helper_run_id"])
        counts = self.std.disposition_page(PFS)["counts"]
        self.assertEqual((counts["auto"], counts["not_reviewed"]), (0, len(self.backlog)))

    def test_a_run_never_overwrites_a_decision(self):
        record = self.decided[0]
        result = self.std.save_auto_dispositions(
            [{"mapped_record_id": record, **_coded(self.truth[record])},
             {"mapped_record_id": self.backlog[0], "disposition_category": "FAILURE"}],
            run={"asset_number": PFS, "min_confidence": 0.9}, decided_by="Disposition helper",
        )
        self.assertEqual(result["applied"], 0)
        self.assertIsNone(result["helper_run_id"])
        reasons = [item["reason"] for item in result["skipped"]]
        self.assertEqual(reasons[0], "Dispositioned meanwhile; left as it is.")
        self.assertIn("FAILURE needs", reasons[1])
        self.assertEqual(self.helper.runs(PFS), [])

    def test_a_run_needs_a_measured_helper_and_a_sensible_bar(self):
        with self.assertRaisesRegex(ValueError, "between 60% and 100%"):
            self.helper.batch_preview(PFS, min_confidence=0.3)
        self.assertEqual(self.helper.batch_preview(PFS, min_confidence=95)["threshold"], 0.95)

    def test_a_run_reads_the_list_as_it_is_filtered(self):
        preview = self.helper.batch_preview(PFS, search="S09", min_confidence=0.6)
        expected = self.std.disposition_page(PFS, scope="not_reviewed", search="S09", page_size=500)["displayed_count"]
        self.assertEqual(preview["considered"], expected)


class UnmeasuredHelperTests(HelperFixture):
    decided_share = 0.1

    def test_too_few_decisions_to_measure_it_refuses_a_run(self):
        self.assertFalse(self.helper.batch_preview(PFS, min_confidence=0.9)["calibrated"])
        with self.assertRaisesRegex(ValueError, "needs at least 40 work orders"):
            self.helper.batch_apply(PFS, min_confidence=0.9)
        # It still suggests, flagged as an estimate.
        suggestion = self.helper.suggest(self.backlog[0])
        self.assertFalse(suggestion["calibrated"])
        self.assertTrue(any("estimates" in note for note in suggestion["notes"]))


class AnalysisTests(HelperFixture):
    def test_perform_an_analysis_counts_and_warns_about_unconfirmed_auto_dispositions(self):
        result = self.helper.batch_apply(PFS, min_confidence=0.6, run_by="tim")
        self.assertGreater(result["applied"], 0)
        analysis = PopulationAnalysisService(self.life, self.std)
        summary = analysis.summary({"assets": [PFS]})
        self.assertEqual(summary["readiness"]["auto_unconfirmed"], result["applied"])
        run = analysis.run_weibull({"assets": [PFS], "locations": [S09], "components": ["Filter"]},
                                   analysis_cutoff=date(2026, 10, 1))
        codes = [warning["code"] for warning in run["warnings"]]
        auto_in_stream = any(
            row["disposition"]["source"] == "AUTO" and row["disposition"]["sub_asset"] == S09
            and row["disposition"]["component_code"] == "Filter"
            and row["disposition"]["disposition_category"] in std.RENEWING_CATEGORIES
            for row in self.std.disposition_page(PFS, scope="auto", page_size=500)["rows"]
        )
        self.assertEqual("AUTO_DISPOSITIONS" in codes, auto_in_stream)


class SchemaTests(unittest.TestCase):
    def test_an_older_database_gains_the_helper_columns(self):
        with tempfile.TemporaryDirectory() as tmp:
            life = LifeDataService(Path(tmp) / "gremlin.db", refresh_on_startup=False)
            with life.write_connection() as conn:
                conn.execute("DROP INDEX idx_wo_disposition_helper_run")
                conn.execute("ALTER TABLE wo_disposition DROP COLUMN helper_run_id")
                conn.execute("ALTER TABLE wo_disposition DROP COLUMN suggestion_json")
                conn.execute("ALTER TABLE wo_disposition DROP COLUMN replaced_wo_disposition_id")
                conn.execute("DROP TABLE disposition_helper_run")
            with life.write_connection() as conn:
                ensure_standard_disposition_schema(conn)
                columns = {row[1] for row in conn.execute("PRAGMA table_info(wo_disposition)")}
                indexes = {row[1] for row in conn.execute("PRAGMA index_list(wo_disposition)")}
                tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            self.assertTrue({"helper_run_id", "suggestion_json", "replaced_wo_disposition_id"} <= columns)
            self.assertIn("idx_wo_disposition_helper_run", indexes)
            self.assertIn("disposition_helper_run", tables)


# ---- the API -------------------------------------------------------------------
API = "/life-data-analysis/api"


def _app(monkeypatch, tmp_path):
    monkeypatch.setenv("GREMLIN_DB_PATH", str(tmp_path / "gremlin.db"))
    monkeypatch.setenv("GREMLIN_ACCESS_DB_PATH", str(tmp_path / "accesscontrol.db"))
    monkeypatch.setattr(dh, "BACKGROUND_CALIBRATION", False)
    import app

    module = importlib.reload(app)
    life = module.get_life_data_service()
    truth = corpus.build_plant(life, per_scenario=6)
    ids = list(truth)
    module._standard_service().save_dispositions(
        [{"mapped_record_id": i, **_coded(truth[i])} for i in ids[: int(len(ids) * 0.7)]], decided_by="tim"
    )
    return module, ids


def _client(module, *, role):
    client = module.app.test_client()
    module.access_control.save_user(None, f"{role}1", "1357", role, "facilities", "engineer")
    with client.session_transaction() as session:
        session["user"] = module.access_control.authenticate(f"{role}1", "1357")
    return client


def test_anyone_who_may_read_sees_suggestions_but_only_an_editor_runs_the_helper(monkeypatch, tmp_path):
    module, ids = _app(monkeypatch, tmp_path)
    viewer = _client(module, role="viewer")
    suggestion = viewer.get(f"{API}/dispositions/suggest?mapped_record_id={ids[-1]}")
    assert suggestion.status_code == 200
    assert suggestion.get_json()["fields"]["disposition_category"]["value"] in std.CODED_CATEGORIES
    assert viewer.get(f"{API}/disposition-helper/accuracy?asset={PFS}").status_code == 200
    batch = {"asset": PFS, "min_confidence": 0.6}
    assert viewer.post(f"{API}/disposition-helper/preview", json=batch).status_code == 403
    assert viewer.post(f"{API}/disposition-helper/apply", json=batch).status_code == 403
    assert viewer.post(f"{API}/disposition-helper/undo", json={"helper_run_id": 1}).status_code == 403
    dh.reset_cache()


def test_an_editor_previews_runs_and_undoes_a_batch(monkeypatch, tmp_path):
    module, ids = _app(monkeypatch, tmp_path)
    editor = _client(module, role="editor")
    batch = {"asset": PFS, "min_confidence": 0.6, "search": "", "filters": {}}
    preview = editor.post(f"{API}/disposition-helper/preview", json=batch)
    assert preview.status_code == 200, preview.get_json()
    would = preview.get_json()["would_apply"]
    applied = editor.post(f"{API}/disposition-helper/apply", json=batch)
    assert applied.status_code == 200, applied.get_json()
    run_id = applied.get_json()["helper_run_id"]
    assert applied.get_json()["applied"] == would
    page = editor.get(f"{API}/dispositions?asset={PFS}&scope=auto").get_json()
    assert page["counts"]["auto"] == would
    runs = editor.get(f"{API}/disposition-helper/runs?asset={PFS}").get_json()["runs"]
    assert runs[0]["helper_run_id"] == run_id and runs[0]["run_by"] == "editor1"
    undone = editor.post(f"{API}/disposition-helper/undo", json={"helper_run_id": run_id})
    assert undone.get_json()["undone"] == would
    assert editor.post(f"{API}/disposition-helper/undo", json={"helper_run_id": run_id}).status_code == 400
    assert editor.post(f"{API}/disposition-helper/undo", json={"helper_run_id": 999}).status_code == 404
    assert editor.post(f"{API}/disposition-helper/preview", json={"min_confidence": 0.9}).status_code == 400
    # A save with a suggestion open keeps it, to measure the helper in use.
    suggestion = editor.get(f"{API}/dispositions/suggest?mapped_record_id={ids[-1]}").get_json()
    payload = {**suggestion["disposition"], "helper_suggestion": {"v": "1.0", "fields": {
        "disposition_category": {"value": suggestion["fields"]["disposition_category"]["value"], "confidence": 0.9},
    }}}
    assert editor.post(f"{API}/dispositions/save", json={"dispositions": [payload]}).status_code == 200
    accuracy = editor.get(f"{API}/disposition-helper/accuracy?asset={PFS}").get_json()
    assert accuracy["in_use"]["assisted_saves"] == 1
    dh.reset_cache()


def test_a_suggestion_names_a_record_that_exists(monkeypatch, tmp_path):
    module, _ = _app(monkeypatch, tmp_path)
    viewer = _client(module, role="viewer")
    assert viewer.get(f"{API}/dispositions/suggest?mapped_record_id=abc").status_code == 400
    assert viewer.get(f"{API}/dispositions/suggest?mapped_record_id=987654").status_code == 404
    assert viewer.get(f"{API}/dispositions/suggest?mapped_record_id={2**70}").status_code == 404
    dh.reset_cache()
