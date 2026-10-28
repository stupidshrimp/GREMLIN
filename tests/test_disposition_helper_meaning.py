"""The disposition helper's meaning matching: the model's plumbing, its cache, and what it changes.

Most of these run a small stand-in encoder, so they need neither the model's
90 MB nor its packages beyond numpy: it reads a few made-up words as meaning the
same, which is what the real model does for real ones. The last test runs the
real model when ``GREMLIN_TEST_EMBEDDING_MODEL_DIR`` names its folder.
"""

import os
import tempfile
import threading
import unittest
from pathlib import Path

import numpy as np

from services import disposition_helper as dh
from services import disposition_helper_embedding as embedding
from services import disposition_standard as std
from tests.test_disposition_helper import HelperFixture, PFS


class FakeEncoder:
    """Words to meaning: each group of words below is one meaning; any other word, one of its own."""

    GROUPS = (
        ("zibbing", "frobnicating"),
        ("sprocketeer", "vintlewheel"),
        ("light", "flame", "ignite"),
    )

    def __init__(self) -> None:
        self.calls = 0
        self.name = embedding.MODEL_NAME

    def _vector(self, textual: str) -> np.ndarray:
        vector = np.zeros(embedding.DIMENSIONS, dtype=np.float32)
        for word in textual.lower().replace(".", " ").split():
            for index, group in enumerate(self.GROUPS):
                if word in group:
                    vector[index] += 1.0
                    break
            else:
                vector[10 + sum(map(ord, word)) % (embedding.DIMENSIONS - 10)] += 0.2
        norm = np.linalg.norm(vector)
        return vector / norm if norm else vector

    def encode(self, texts):
        self.calls += len(texts)
        return np.stack([self._vector(textual) for textual in texts]) if texts else np.zeros((0, embedding.DIMENSIONS), np.float32)


class EmbeddingPlumbingTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        background = embedding.BACKGROUND
        embedding.BACKGROUND = False
        self.addCleanup(setattr, embedding, "BACKGROUND", background)

    def test_the_text_read_is_the_work_orders_own_words_once(self):
        textual = embedding.embedding_text({
            "task_name": "Oven down", "request_title": "oven down", "requestor_description": "  ",
            "condition_found": "filter clogged", "action_taken": "cleaned it.", "asset_name": "4002 Panel Finishing",
        })
        self.assertEqual(textual, "Oven down. filter clogged. cleaned it.")

    def test_it_says_why_meaning_matching_is_off(self):
        db = self.dir / "GREMLIN.db"
        os.environ["GREMLIN_EMBEDDINGS"] = "off"
        try:
            encoder, status = embedding.availability(db)
            self.assertIsNone(encoder)
            self.assertIn("GREMLIN_EMBEDDINGS=off", status["reason"])
        finally:
            del os.environ["GREMLIN_EMBEDDINGS"]
        if not embedding.missing_packages():
            encoder, status = embedding.availability(db)
            self.assertIsNone(encoder)
            self.assertIn("tools/fetch_embedding_model.py", status["reason"])
            self.assertEqual(status["model_dir"], str(self.dir / "embedding-model" / embedding.MODEL_NAME))
        encoder, status = embedding.availability(db, load=lambda path: FakeEncoder())
        self.assertIsNotNone(encoder)
        self.assertTrue(status["enabled"])

    def test_vectors_are_read_once_and_kept_by_their_text(self):
        cache = embedding.VectorCache(self.dir / "GREMLIN.embeddings.db")
        encoder = FakeEncoder()
        index = embedding.MeaningIndex([1, 2, 3], ["zibbing", "frobnicating", "sprocketeer"], encoder, cache)
        self.assertTrue(index.complete)
        self.assertEqual(encoder.calls, 3)
        scores = index.similarities(index.query("zibbing"))
        self.assertAlmostEqual(float(scores[1]), 1.0, places=3)
        self.assertEqual(index.nearest(scores, 1, exclude=0)[0][0], 1)
        # Read again from the cache; only the work order whose text changed is read anew.
        again = FakeEncoder()
        reread = embedding.MeaningIndex([1, 2, 3], ["zibbing", "frobnicating", "vintlewheel"], again, cache)
        self.assertEqual(again.calls, 1)
        self.assertAlmostEqual(float(reread.similarities(reread.query("sprocketeer"))[2]), 1.0, places=3)

    def test_unread_work_orders_are_left_out_until_read(self):
        gate = threading.Event()

        class SlowEncoder(FakeEncoder):
            def encode(self, texts):
                if len(texts) > 1:
                    gate.wait(5)
                return super().encode(texts)

        embedding.BACKGROUND = True
        index = embedding.MeaningIndex([1, 2], ["zibbing", "frobnicating"], SlowEncoder(),
                                       embedding.VectorCache(self.dir / "cache.db"))
        self.assertFalse(index.complete)
        scores = index.similarities(index.query("zibbing"))
        self.assertTrue(np.isnan(scores).all())
        self.assertEqual(index.nearest(scores, 5), [])
        gate.set()
        for _ in range(100):
            if index.complete:
                break
            threading.Event().wait(0.05)
        self.assertTrue(index.complete)

    def test_a_cache_it_cannot_write_keeps_vectors_in_memory(self):
        blocker = self.dir / "file"
        blocker.write_text("not a folder")
        cache = embedding.VectorCache(blocker / "sub" / "cache.db")
        self.assertFalse(cache.usable)
        index = embedding.MeaningIndex([1], ["zibbing"], FakeEncoder(), cache)
        self.assertTrue(index.complete)


class MeaningInTheHelperTests(HelperFixture):
    def setUp(self) -> None:
        loader = embedding.LOADER
        background = embedding.BACKGROUND
        embedding.LOADER = lambda path: FakeEncoder()
        embedding.BACKGROUND = False
        self.addCleanup(setattr, embedding, "LOADER", loader)
        self.addCleanup(setattr, embedding, "BACKGROUND", background)
        super().setUp()

    def label(self, mapped_record_id, **codes):
        self.std.save_dispositions([{
            "mapped_record_id": mapped_record_id, "disposition_category": "FAILURE", "sub_asset": None,
            "evidence": "INFERRED", **codes,
        }], decided_by="tim")

    def test_a_work_order_worded_unlike_any_other_finds_its_match_by_meaning(self):
        known = self.add_work_order(60001, "Vintlewheel frobnicating")
        self.std.save_dispositions([{
            "mapped_record_id": known, "disposition_category": "NON_FAILURE", "component_code": "Motor",
            "failure_mechanism_code": "UNK", "failure_mode_code": "NFF", "action_code": "INSPECT_ONLY",
            "evidence": "INFERRED",
        }], decided_by="tim")
        record = self.add_work_order(60002, "Sprocketeer zibbing")
        suggestion = self.helper.suggest(record)
        self.assertTrue(suggestion["meaning"]["enabled"])
        self.assertTrue(suggestion["meaning"]["complete"])
        first = suggestion["similar"][0]
        self.assertEqual(first["mapped_record_id"], known)
        self.assertLess(first["wording"], 0.1)  # only the record class in common
        self.assertGreater(first["meaning"], 0.9)
        self.assertEqual(suggestion["fields"]["component_code"]["value"], "Motor")
        # The category is read from the wording, which this match does not share:
        # it is not taken from a work order that only means the same.
        self.assertNotEqual(suggestion["fields"]["disposition_category"]["value"], std.NON_FAILURE)

    def test_turned_off_it_matches_words_alone(self):
        known = self.add_work_order(60003, "Vintlewheel frobnicating")
        self.label(known, component_code="Motor", failure_mechanism_code="WEAR", failure_mode_code="VIB",
                   action_code="REPLACE")
        record = self.add_work_order(60004, "Sprocketeer zibbing")
        os.environ["GREMLIN_EMBEDDINGS"] = "off"
        try:
            dh.reset_cache()
            suggestion = self.helper.suggest(record)
        finally:
            del os.environ["GREMLIN_EMBEDDINGS"]
        self.assertFalse(suggestion["meaning"]["enabled"])
        self.assertNotIn(known, [item["mapped_record_id"] for item in suggestion["similar"]])

    def test_the_report_compares_with_words_alone(self):
        report = self.helper.evaluate(PFS)
        self.assertTrue(report["meaning"]["enabled"])
        plain = report["without_meaning"]
        self.assertEqual([row["field"] for row in plain["fields"]], [row["field"] for row in report["fields"]])
        self.assertEqual(len(plain["thresholds"]), len(dh.EVALUATION_THRESHOLDS))
        status = self.helper.status()
        self.assertEqual((status["meaning"]["done"], status["meaning"]["total"]),
                         (len(self.helper.knowledge()[0].ids),) * 2)

    def test_the_calibration_is_refitted_once_every_work_order_is_read(self):
        corpus, labels, codebook = self.helper.knowledge(token=self.life.change_token())
        with self.life.connect() as conn:
            tree = self.std.asset_tree(conn)
        first = self.helper._calibration(corpus, labels, codebook, tree, wait=True)
        self.assertIs(self.helper._calibration(corpus, labels, codebook, tree, wait=True), first)
        corpus.meaning.done -= 1  # as though one were still being read
        second = self.helper._calibration(corpus, labels, codebook, tree, wait=True)
        self.assertIsNot(second, first)
        self.assertEqual(second.basis[2], (True, False))


@unittest.skipUnless(os.environ.get("GREMLIN_TEST_EMBEDDING_MODEL_DIR"), "set GREMLIN_TEST_EMBEDDING_MODEL_DIR to the model's folder")
class RealModelTests(unittest.TestCase):
    def test_the_model_reads_meaning(self):
        encoder = embedding.load_encoder(Path(os.environ["GREMLIN_TEST_EMBEDDING_MODEL_DIR"]))
        vectors = encoder.encode(["Burner won't light", "No flame on startup", "Conveyor chain is noisy"])
        self.assertEqual(vectors.shape, (3, embedding.DIMENSIONS))
        self.assertAlmostEqual(float(np.linalg.norm(vectors[0])), 1.0, places=4)
        self.assertGreater(float(vectors[0] @ vectors[1]), float(vectors[0] @ vectors[2]) + 0.3)
