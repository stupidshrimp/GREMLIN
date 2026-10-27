"""Meaning matching for the disposition helper: a small embedding model run on this machine.

The helper's word matching (``disposition_helper_text``) finds work orders that
use the same words, or the same thesaurus concepts. "Burner won't light" and
"no flame on startup" share neither. An embedding model reads each work order
into 384 numbers that stand for what it *means*, learnt from a great deal of
general English, so those two land close together without anyone having
written the pair into a thesaurus.

The model is ``sentence-transformers/all-MiniLM-L6-v2`` (Apache-2.0), run
through ONNX Runtime on the CPU: nothing is sent anywhere. It is optional. Without
the packages (``numpy``, ``onnxruntime``, ``tokenizers``) or the model files
(``tools/fetch_embedding_model.py`` puts them in place) the helper reads words
only, exactly as before, and says why.

Every work order is read into its vector once and kept, by a hash of the text it
was read from, in a cache file beside GREMLIN.db (``GREMLIN.embeddings.db``)
rather than in GREMLIN.db itself: it is derived data any machine can rebuild,
and writing it there would move GREMLIN.db's change token, throwing away every
cache keyed on it, all the while the backlog was being read. Work orders not
read yet are read in the background (about 2-3 ms each), and are matched by
their words alone until they are.

Settings (environment or ``.env``):

* ``GREMLIN_EMBEDDINGS`` -- ``off`` turns meaning matching off.
* ``GREMLIN_EMBEDDING_MODEL_DIR`` -- the model's folder (``onnx/model.onnx`` and
  ``tokenizer.json``); by default ``embedding-model/all-MiniLM-L6-v2`` beside
  GREMLIN.db.
* ``GREMLIN_EMBEDDING_CACHE_PATH`` -- the vector cache; by default
  ``GREMLIN.embeddings.db`` beside GREMLIN.db.
"""

from __future__ import annotations

import hashlib
import importlib.util
import os
import re
import sqlite3
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

MODEL_NAME = "all-MiniLM-L6-v2"
MODEL_REPO = "sentence-transformers/all-MiniLM-L6-v2"
# Pinned: another export of the same model can tokenize or pool differently,
# which would make every cached vector disagree with new ones.
MODEL_REVISION = "c9745ed1d9f207416be6d2e6f8de32d1f16199bf"
MODEL_FILES = {
    "onnx/model.onnx": "6fd5d72fe4589f189f8ebc006442dbb529bb7ce38f8082112682524616046452",
    "tokenizer.json": "be50c3628f2bf5bb5e3a7f17b1f74611b2561a3a27eeab05e5aa30f411572037",
}
DIMENSIONS = 384
# A work order is read up to this many word pieces: the title, the narrative and
# the notes, which is all but the longest of them.
MAX_TOKENS = 256
# Small batches: the model's working memory grows with the batch, and on a CPU a
# bigger one is no faster (16 held about 220 MB where 64 held 425 MB).
BATCH_SIZE = 16
CACHE_FILENAME = "GREMLIN.embeddings.db"
REQUIRED_PACKAGES = ("numpy", "onnxruntime", "tokenizers")
# The parts of a work order its meaning is read from, in this order. The asset's
# own name is left out: it says where, which the sub-asset reading covers, and
# every work order on the asset would share it.
TEXT_FIELDS = (
    "task_name", "request_title", "requestor_description", "area_affected", "condition_found", "cause",
    "action_taken", "completion_notes",
)
# Whether a missing vector is read in the background (tests read them at once).
BACKGROUND = True
# What loads the model from its folder, when not the real one (tests give
# another, so the plumbing is tested without the model's 90 MB).
LOADER: Callable[[Path], Any] | None = None


def settings(db_path: str | os.PathLike) -> dict[str, Any]:
    """Where the model and the cache are for the GREMLIN.db at ``db_path``, and whether meaning matching is on."""

    switch = (os.environ.get("GREMLIN_EMBEDDINGS") or "").strip().lower()
    base = Path(db_path)
    return {
        "enabled": switch not in ("0", "off", "false", "no", "disabled"),
        "model_dir": Path(os.environ.get("GREMLIN_EMBEDDING_MODEL_DIR") or base.parent / "embedding-model" / MODEL_NAME),
        "cache_path": Path(os.environ.get("GREMLIN_EMBEDDING_CACHE_PATH") or base.with_name(CACHE_FILENAME)),
    }


def missing_packages() -> list[str]:
    return [name for name in REQUIRED_PACKAGES if importlib.util.find_spec(name) is None]


def missing_files(model_dir: Path) -> list[str]:
    return [name for name in MODEL_FILES if not (model_dir / name).is_file()]


def embedding_text(row: Mapping[str, Any]) -> str:
    """The text a work order's meaning is read from: its title, request, narrative and notes.

    A part that repeats an earlier one (a request title that is the task title)
    is read once.
    """

    keys = set(row.keys())
    parts: list[str] = []
    seen: set[str] = set()
    for name in TEXT_FIELDS:
        value = row[name] if name in keys else None
        textual = re.sub(r"\s+", " ", str(value or "")).strip()
        if not textual or textual.casefold() in seen:
            continue
        seen.add(textual.casefold())
        parts.append(textual.rstrip(".") + ".")
    return " ".join(parts)


def text_hash(textual: str) -> str:
    return hashlib.sha1(f"{MODEL_NAME}\n{textual}".encode("utf-8")).hexdigest()


class Encoder:
    """The model: text in, a unit vector of meaning out."""

    def __init__(self, model_dir: Path) -> None:
        import numpy as np
        import onnxruntime as ort
        from tokenizers import Tokenizer

        self.np = np
        options = ort.SessionOptions()
        # Half the machine at most: the helper shares the server with everyone's pages.
        options.intra_op_num_threads = max(1, (os.cpu_count() or 2) // 2)
        # No memory arena: it keeps the largest batch's memory for good.
        options.enable_cpu_mem_arena = False
        options.log_severity_level = 3
        self.session = ort.InferenceSession(
            str(model_dir / "onnx" / "model.onnx"), sess_options=options, providers=["CPUExecutionProvider"]
        )
        self.inputs = {item.name for item in self.session.get_inputs()}
        self.tokenizer = Tokenizer.from_file(str(model_dir / "tokenizer.json"))
        self.tokenizer.enable_truncation(MAX_TOKENS)
        self.tokenizer.enable_padding()
        self.name = MODEL_NAME

    def encode(self, texts: Sequence[str]) -> Any:
        """Unit vectors, one row per text (mean of the token vectors, as the model was trained)."""

        np = self.np
        if not texts:
            return np.zeros((0, DIMENSIONS), dtype=np.float32)
        encoded = self.tokenizer.encode_batch([text or " " for text in texts])
        ids = np.array([item.ids for item in encoded], dtype=np.int64)
        mask = np.array([item.attention_mask for item in encoded], dtype=np.int64)
        feeds = {"input_ids": ids, "attention_mask": mask}
        if "token_type_ids" in self.inputs:
            feeds["token_type_ids"] = np.zeros_like(ids)
        hidden = self.session.run(None, feeds)[0]
        weights = mask[..., None].astype(np.float32)
        pooled = (hidden * weights).sum(axis=1) / np.clip(weights.sum(axis=1), 1e-9, None)
        norms = np.linalg.norm(pooled, axis=1, keepdims=True)
        return (pooled / np.clip(norms, 1e-12, None)).astype(np.float32)


_ENCODERS: dict[str, Any] = {}
_ENCODER_LOCK = threading.Lock()


def load_encoder(model_dir: Path) -> Encoder:
    """The model in ``model_dir``, loaded once per process."""

    key = str(Path(model_dir).resolve())
    with _ENCODER_LOCK:
        encoder = _ENCODERS.get(key)
        if encoder is None:
            encoder = _ENCODERS[key] = Encoder(Path(model_dir))
        return encoder


def availability(db_path: str | os.PathLike, *, load: Callable[[Path], Any] | None = None) -> tuple[Any | None, dict[str, Any]]:
    """The encoder to use, or None, and a status saying which and why."""

    config = settings(db_path)
    status: dict[str, Any] = {"enabled": False, "model": MODEL_NAME, "model_dir": str(config["model_dir"]), "reason": None}
    if not config["enabled"]:
        status["reason"] = "Turned off (GREMLIN_EMBEDDINGS=off)."
        return None, status
    missing = missing_packages()
    if missing and load is None:
        status["reason"] = f"The {', '.join(missing)} package{'s are' if len(missing) > 1 else ' is'} not installed."
        return None, status
    if load is None and missing_files(config["model_dir"]):
        status["reason"] = (
            f"The model is not in {config['model_dir']}; run tools/fetch_embedding_model.py to put it there."
        )
        return None, status
    try:
        encoder = (load or load_encoder)(config["model_dir"])
    except Exception as exc:  # noqa: BLE001 - a model that will not load leaves word matching
        status["reason"] = f"The model could not be loaded: {exc}"
        return None, status
    status["enabled"] = True
    return encoder, status


class VectorCache:
    """Each work order's vector, by the text it was read from, in a SQLite file of its own."""

    def __init__(self, path: Path, model: str = MODEL_NAME) -> None:
        self.path = Path(path)
        self.model = model
        self.usable = True
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self._connect() as conn:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS embedding (
                        mapped_record_id INTEGER NOT NULL,
                        model TEXT NOT NULL,
                        text_hash TEXT NOT NULL,
                        vector BLOB NOT NULL,
                        embedded_at TEXT NOT NULL DEFAULT (datetime('now')),
                        PRIMARY KEY (mapped_record_id, model)
                    )
                    """
                )
        except (OSError, sqlite3.Error):
            # A folder this machine cannot write to: vectors are kept in memory only.
            self.usable = False

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.execute("PRAGMA busy_timeout = 30000")
        return conn

    def each(self) -> Iterable[tuple[int, str, bytes]]:
        """Every kept vector for this model: (mapped_record_id, text hash, float16 bytes)."""

        if not self.usable:
            return
        try:
            with self._connect() as conn:
                for row in conn.execute(
                    "SELECT mapped_record_id, text_hash, vector FROM embedding WHERE model = ?", (self.model,)
                ):
                    yield int(row[0]), row[1], row[2]
        except sqlite3.Error:
            return

    def store(self, rows: Iterable[tuple[int, str, bytes]]) -> None:
        if not self.usable:
            return
        try:
            with self._connect() as conn:
                conn.executemany(
                    """
                    INSERT INTO embedding(mapped_record_id, model, text_hash, vector) VALUES (?, ?, ?, ?)
                    ON CONFLICT(mapped_record_id, model) DO UPDATE SET
                        text_hash = excluded.text_hash, vector = excluded.vector, embedded_at = datetime('now')
                    """,
                    [(mapped_record_id, self.model, digest, vector) for mapped_record_id, digest, vector in rows],
                )
        except sqlite3.Error:
            self.usable = False


class MeaningIndex:
    """Every work order's meaning vector, by its place in the helper's word index.

    Built beside a ``Corpus`` from the same rows. Vectors already in the cache
    for the same text are used as they are; the rest are read in the
    background and added as they are done, so the index is usable at once and
    complete a minute or two later on a plant's history.
    """

    def __init__(self, ids: Sequence[int], texts: Sequence[str], encoder: Any, cache: VectorCache,
                 *, on_complete: Callable[[], None] | None = None) -> None:
        import numpy as np

        self.np = np
        self.encoder = encoder
        self.cache = cache
        self.ids = list(ids)
        self.total = len(self.ids)
        self.matrix = np.zeros((self.total, DIMENSIONS), dtype=np.float32)
        self.have = np.zeros(self.total, dtype=bool)
        self.hashes = [text_hash(textual) for textual in texts]
        self._on_complete = on_complete
        self._queries: OrderedDict[str, Any] = OrderedDict()
        self._lock = threading.Lock()
        positions = {mapped_record_id: position for position, mapped_record_id in enumerate(self.ids)}
        for mapped_record_id, digest, vector in cache.each():
            position = positions.get(mapped_record_id)
            if position is not None and digest == self.hashes[position] and len(vector) == DIMENSIONS * 2:
                self.matrix[position] = np.frombuffer(vector, dtype=np.float16)
                self.have[position] = True
        missing = [position for position in range(self.total) if not self.have[position]]
        self.done = int(self.have.sum())
        self.error: str | None = None
        if not missing:
            return
        work = [(position, texts[position]) for position in missing]
        if BACKGROUND:
            threading.Thread(target=self._fill, args=(work,), daemon=True, name="gremlin-embeddings").start()
        else:
            self._fill(work)

    @property
    def complete(self) -> bool:
        return self.done >= self.total

    def _fill(self, work: list[tuple[int, str]]) -> None:
        np = self.np
        try:
            for start in range(0, len(work), BATCH_SIZE):
                chunk = work[start:start + BATCH_SIZE]
                vectors = self.encoder.encode([textual for _, textual in chunk])
                rows = []
                for (position, _), vector in zip(chunk, vectors):
                    self.matrix[position] = vector
                    self.have[position] = True
                    rows.append((self.ids[position], self.hashes[position], vector.astype(np.float16).tobytes()))
                self.done = int(self.have.sum())
                self.cache.store(rows)
        except Exception as exc:  # noqa: BLE001 - what was read is kept; the rest are matched by words
            self.error = str(exc)
            return
        if self._on_complete is not None:
            self._on_complete()

    def query(self, textual: str) -> Any:
        """A work order's vector from its text (the last few are kept, so reopening one costs nothing)."""

        with self._lock:
            kept = self._queries.get(textual)
            if kept is not None:
                self._queries.move_to_end(textual)
                return kept
        vector = self.encoder.encode([textual])[0]
        with self._lock:
            self._queries[textual] = vector
            while len(self._queries) > 256:
                self._queries.popitem(last=False)
        return vector

    def vector_for(self, position: int | None, textual: str) -> Any:
        """A work order's vector: the index's, when it has read the same text, else read now."""

        if position is not None and 0 <= position < self.total and self.have[position] and self.hashes[position] == text_hash(textual):
            return self.matrix[position]
        return self.query(textual)

    def similarities(self, vector: Any) -> Any:
        """Cosine similarity to every work order; NaN for those not read yet."""

        np = self.np
        scores = self.matrix @ vector
        scores[~self.have] = np.nan
        return scores

    def nearest(self, scores: Any, count: int, *, exclude: int | None = None) -> list[tuple[int, float]]:
        np = self.np
        ranked = np.where(np.isnan(scores), -np.inf, scores)
        if exclude is not None and 0 <= exclude < self.total:
            ranked[exclude] = -np.inf
        count = min(count, self.total)
        if count <= 0:
            return []
        top = np.argpartition(-ranked, count - 1)[:count]
        return [(int(position), float(scores[position])) for position in top if np.isfinite(ranked[position])]

    def status(self) -> dict[str, Any]:
        return {"done": self.done, "total": self.total, "complete": self.complete, "error": self.error}
