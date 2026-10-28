"""The disposition helper: reads a work order and proposes its disposition.

Two sources of evidence, read together for every field:

* **The work orders the team has already dispositioned.** Every work order is
  indexed by its words and the concepts read off them
  (``disposition_helper_text``), weighted TF-IDF so a word every work order uses
  counts for little and "S09" or "flame rod" for a lot. The most similar
  dispositioned work orders vote for their codes, each by how similar it is,
  and one on the same asset counts for more. That is how the plant's own way of
  writing is learnt: if "dry-off tripped" has been coded S09 / Burner / SIGN a
  dozen times, the thirteenth is too.
* **What the work order says outright.** A phrase from the code lists or the
  thesaurus ("clogged", "won't light", "replaced") is evidence for its code on its
  own, which is what carries a work order unlike any seen before, or a code
  nobody has used yet. A sub-asset is read off its number ("S09"), its name, or
  the asset the work order was logged against.

Where the two agree the helper is sure; where they disagree, or there is little
of either, it is not, and says so. Each field's confidence is then *calibrated*:
the helper disposition the work orders the team has already decided, as if they
were new (each one without itself), and learns how often a suggestion at each
score was right. A confidence of 90% means that nine in ten suggestions that
scored like this one matched what an engineer decided. The same run is the
accuracy report the Disposition page shows.

The helper never decides the standard's rules: the disposition it proposes goes
through ``normalize_disposition`` like any other, and what it saves on its own
is marked ``source = 'AUTO'`` with a review flag until an engineer confirms it.
"""

from __future__ import annotations

import json
import math
import random
import sqlite3
import threading
from array import array
from bisect import bisect_right
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import TYPE_CHECKING, Any, Iterable, Mapping, Sequence

from services import disposition_standard as std
from services import disposition_helper_embedding as embedding
from services import disposition_helper_text as text
from services.standard_disposition_schema import AssetTree, load_codebook

if TYPE_CHECKING:  # pragma: no cover - typing only
    from services.standard_disposition_service import StandardDispositionService

HELPER_VERSION = "1.0"

# How many of the most similar dispositioned work orders vote.
NEIGHBOURS = 25
# How many of them the page lists as "similar work orders".
SIMILAR_SHOWN = 5
# A feature in more than this share of work orders ("replaced", "pm") is left
# out of the search for candidates; it would touch most of the index and tell
# them apart from almost nothing. The candidates found are then scored on every
# feature.
COMMON_FEATURE_SHARE = 0.2
CANDIDATES_RESCORED = 300
# Similar enough, logged on the same asset within this many days: possibly the
# same job logged twice.
DUPLICATE_SIMILARITY = 0.6
DUPLICATE_DAYS = 3
# How the votes are weighed: the same asset counts for more, and a REL
# disposition carried over by the migration (a person's decision, but under the
# old categories and seldom coded) for less.
SAME_ASSET_WEIGHT = 1.5
MIGRATED_WEIGHT = 0.6
OTHER_CATEGORY_WEIGHT = 0.4
# How much vote weight makes the votes trusted as much as they can be.
VOTE_SCALE = 0.6
# With meaning matching on, how alike two work orders are is this much their
# wording (TF-IDF) and the rest their meaning (the embedding model). The model
# scores any two pieces of English a little alike, so its similarity counts
# only above MEANING_FLOOR.
WORDING_SHARE = 0.8
MEANING_FLOOR = 0.2

# The fields the helper proposes, in the order the form fills them.
CODED_FIELDS = {
    "component_code": std.COMPONENT,
    "failure_mechanism_code": std.FAILURE_MECHANISM,
    "failure_mode_code": std.FAILURE_MODE,
    "action_code": std.ACTION,
}
SCORED_FIELDS = ("disposition_category", "sub_asset", *CODED_FIELDS)
FIELD_NAMES = {
    "disposition_category": "Category",
    "sub_asset": "Sub-asset",
    "component_code": "Component",
    "failure_mechanism_code": "Failure mechanism",
    "failure_mode_code": "Failure mode",
    "action_code": "Action",
    "failure_cause_code": "Failure cause",
    "renewed_items": "Renewed items",
    "evidence": "Evidence",
}
# The categories the helper may propose. HELD needs a person to say what is
# missing and DUPLICATE_MISFILED the work order it repeats: the helper points
# out a likely duplicate but leaves the call to the engineer.
SUGGESTED_CATEGORIES = std.CODED_CATEGORIES

# The form fills a field in only when the helper is at least this sure of it;
# below that the suggestion is offered beside the field instead.
PREFILL_CONFIDENCE = 0.5
# A batch run's default bar, and the lowest one it accepts.
DEFAULT_BATCH_CONFIDENCE = 0.9
MIN_BATCH_CONFIDENCE = 0.6
# Most work orders one batch run dispositions; the next run takes the rest.
BATCH_LIMIT = 1000
# Calibration: how many decided work orders it re-reads, and how few make it
# unreliable. Below the floor the helper still suggests, but a batch run is
# refused: it would be acting on a confidence nobody has measured.
CALIBRATION_SAMPLE = 400
MIN_CALIBRATION_RECORDS = 40
# Uncalibrated scores are taken down by this much, to stay on the safe side.
UNCALIBRATED_DISCOUNT = 0.85
EVALUATION_THRESHOLDS = (0.5, 0.7, 0.8, 0.9, 0.95)


# ---------------------------------------------------------------------------
# The index
# ---------------------------------------------------------------------------
_TEXT_COLUMNS = (
    "task_name", "request_title", "requestor_description", "area_affected", "condition_found", "cause",
    "action_taken", "completion_notes", "asset_name",
)
_CORPUS_SQL = f"""
    SELECT m.mapped_record_id, TRIM(m.asset_number) AS asset_number, m.task_id,
           {', '.join('m.' + column for column in _TEXT_COLUMNS)},
           COALESCE(m.record_class_final, m.record_class_auto) AS record_class,
           COALESCE(NULLIF(TRIM(m.created_date_final), ''), m.completed_date_final) AS found
    FROM mapped_cmms_record m
    WHERE TRIM(COALESCE(m.asset_number, '')) <> ''
"""


def _day_number(value: Any) -> int | None:
    """A work order's date as a day number, from the leading YYYY-MM-DD; None when it has none."""

    textual = str(value or "").strip()
    if len(textual) >= 10 and textual[4] == "-" and textual[7] == "-":
        try:
            return date.fromisoformat(textual[:10]).toordinal()
        except ValueError:
            return None
    return None


def record_texts(row: Mapping[str, Any]) -> dict[str, Any]:
    """The text fields the helper reads from a work-order row (or a record dict)."""

    keys = set(row.keys())
    return {column: row[column] if column in keys else None for column in _TEXT_COLUMNS}


class Corpus:
    """Every work order's features, and an inverted index to find the similar ones."""

    def __init__(self, rows: Iterable[Mapping[str, Any]], lexicon: text.Lexicon) -> None:
        self.lexicon = lexicon
        self.ids: list[int] = []
        self.assets: list[str] = []
        self.days: list[int | None] = []
        self.task_ids: list[str] = []
        self.titles: list[str] = []
        self.feature_ids: dict[str, int] = {}
        doc_features: list[tuple[array, array]] = []
        df: list[int] = []
        for row in rows:
            reading = text.read_work_order(record_texts(row), lexicon, record_class=row["record_class"])
            ids = array("i")
            counts = array("f")
            for feature, count in reading.features.items():
                fid = self.feature_ids.get(feature)
                if fid is None:
                    fid = len(df)
                    self.feature_ids[feature] = fid
                    df.append(0)
                df[fid] += 1
                ids.append(fid)
                counts.append(count)
            self.ids.append(int(row["mapped_record_id"]))
            self.assets.append(row["asset_number"] or "")
            self.days.append(_day_number(row["found"]))
            self.task_ids.append("" if row["task_id"] is None else str(row["task_id"]))
            self.titles.append(str(row["task_name"] or row["request_title"] or ""))
            doc_features.append((ids, counts))
        self.size = len(self.ids)
        self.position = {mapped_record_id: index for index, mapped_record_id in enumerate(self.ids)}
        total = max(self.size, 1)
        self.idf = array("d", (math.log((total + 1) / (count + 1)) + 1.0 for count in df))
        self.df = df
        limit = max(50, int(total * COMMON_FEATURE_SHARE))
        self.common = bytearray(1 if count > limit else 0 for count in df)
        self.postings_docs: list[array] = [array("i") for _ in df]
        self.postings_weights: list[array] = [array("f") for _ in df]
        self.doc_vectors: list[tuple[array, array]] = []
        for position, (ids, counts) in enumerate(doc_features):
            weights = [(1.0 + math.log(count)) * self.idf[fid] if count > 0 else 0.0 for fid, count in zip(ids, counts)]
            norm = math.sqrt(sum(weight * weight for weight in weights)) or 1.0
            normalised = array("f", (weight / norm for weight in weights))
            for fid, weight in zip(ids, normalised):
                self.postings_docs[fid].append(position)
                self.postings_weights[fid].append(weight)
            self.doc_vectors.append((ids, normalised))
        # The words the plant uses, by how many work orders use them, and the
        # thesaurus's: what a misspelling is pulled onto.
        known: dict[str, int] = {}
        for feature, fid in self.feature_ids.items():
            if feature.startswith("w:"):
                known[feature[2:]] = df[fid]
        for word in lexicon.words():
            known.setdefault(word, 1)
        self.speller = text.Speller(known)
        # The work orders' meaning vectors, by the same positions, when meaning
        # matching is on (see DispositionHelper.knowledge).
        self.meaning: embedding.MeaningIndex | None = None
        self.meaning_status: dict[str, Any] = {"enabled": False, "model": embedding.MODEL_NAME, "reason": None}

    def similarity(self, query: Mapping[int, float], position: int) -> float:
        """Cosine similarity of ``query`` to one work order, on every feature."""

        ids, weights = self.doc_vectors[position]
        return sum(query.get(fid, 0.0) * weight for fid, weight in zip(ids, weights))

    def meaning_signature(self) -> tuple:
        """What the calibration depends on of the meaning index: whether there is one, and whether it is complete."""

        return (self.meaning is not None, self.meaning is not None and self.meaning.complete)

    def word_idf(self, word: str) -> float:
        fid = self.feature_ids.get(f"w:{word}")
        return self.idf[fid] if fid is not None else math.log(self.size + 1) + 1.0

    def vector(self, reading: text.Reading) -> dict[int, float]:
        """A reading as a unit vector over the index's features (features it has never seen are dropped)."""

        weights: dict[int, float] = {}
        for feature, count in reading.features.items():
            fid = self.feature_ids.get(feature)
            if fid is None or count <= 0:
                continue
            weights[fid] = (1.0 + math.log(count)) * self.idf[fid]
        norm = math.sqrt(sum(weight * weight for weight in weights.values())) or 1.0
        return {fid: weight / norm for fid, weight in weights.items()}

    def search(self, query: Mapping[int, float], *, exclude: int | None = None) -> list[tuple[int, float]]:
        """Work orders by cosine similarity to ``query``, most similar first: (position, similarity)."""

        scores: dict[int, float] = {}
        for fid, weight in query.items():
            if self.common[fid]:
                continue
            for position, doc_weight in zip(self.postings_docs[fid], self.postings_weights[fid]):
                scores[position] = scores.get(position, 0.0) + weight * doc_weight
        if exclude is not None:
            scores.pop(exclude, None)
        best = sorted(scores.items(), key=lambda item: item[1], reverse=True)[:CANDIDATES_RESCORED]
        rescored = [(position, self.similarity(query, position)) for position, _ in best]
        rescored.sort(key=lambda item: item[1], reverse=True)
        return rescored


@dataclass(frozen=True)
class Label:
    """One work order's current disposition, as a vote."""

    disposition_category: str
    sub_asset: str | None
    component_code: str | None
    failure_mechanism_code: str | None
    failure_mode_code: str | None
    action_code: str | None
    failure_cause_code: str | None
    renewed_items: tuple[str, ...]
    source: str


_LABELS_SQL = """
    SELECT mapped_record_id, disposition_category, sub_asset, component_code, failure_mechanism_code,
           failure_mode_code, action_code, failure_cause_code, renewed_items_json, source
    FROM wo_disposition
    WHERE is_current = 1 AND disposition_category <> 'NOT_REVIEWED' AND source <> 'AUTO'
"""


def load_labels(conn: sqlite3.Connection) -> dict[int, Label]:
    """Every decision a person made (or the migration carried over), by work order.

    The helper's own unconfirmed dispositions are left out, so it never learns
    from itself.
    """

    labels: dict[int, Label] = {}
    for row in conn.execute(_LABELS_SQL):
        try:
            renewed = tuple(json.loads(row[8] or "[]"))
        except (TypeError, ValueError):
            renewed = ()
        labels[int(row[0])] = Label(
            disposition_category=row[1],
            sub_asset=row[2],
            component_code=row[3],
            failure_mechanism_code=row[4],
            failure_mode_code=row[5],
            action_code=row[6],
            failure_cause_code=row[7],
            renewed_items=tuple(str(item) for item in renewed if item),
            source=row[9] or "ENGINEER",
        )
    return labels


# ---------------------------------------------------------------------------
# Calibration
# ---------------------------------------------------------------------------
@dataclass
class Isotonic:
    """A monotone step from a raw score to how often a suggestion that scored so was right."""

    edges: list[float] = field(default_factory=list)
    values: list[float] = field(default_factory=list)
    samples: int = 0

    @classmethod
    def fit(cls, pairs: Sequence[tuple[float, bool]]) -> "Isotonic":
        """Pool-adjacent-violators over (score, right), with each pool's rate shrunk towards a coin toss.

        The shrinking -- (right + 1) / (n + 2) -- keeps a handful of lucky
        suggestions from reading as certain.
        """

        # Suggestions that scored the same are one block from the start, so a
        # score never reads from a block above its own.
        tied: dict[float, list[float]] = {}
        for score, right in pairs:
            block = tied.setdefault(score, [0.0, 0.0])
            block[0] += float(right)
            block[1] += 1.0
        blocks: list[list[float]] = []  # [score low, right, n]
        for score in sorted(tied):
            blocks.append([score, *tied[score]])
            while len(blocks) > 1 and blocks[-2][1] / blocks[-2][2] >= blocks[-1][1] / blocks[-1][2]:
                low, hits, count = blocks.pop()
                blocks[-1][1] += hits
                blocks[-1][2] += count
        return cls(
            edges=[block[0] for block in blocks],
            values=[(block[1] + 1.0) / (block[2] + 2.0) for block in blocks],
            samples=len(pairs),
        )

    def __call__(self, score: float) -> float:
        if not self.values:
            return score * UNCALIBRATED_DISCOUNT
        if score < self.edges[0]:
            # Below every score it was fitted on (an alternative behind a sure
            # suggestion, usually): nothing says how often those are right, so
            # it falls away from the lowest block towards nothing.
            return self.values[0] * max(0.0, score) / self.edges[0] if self.edges[0] > 0 else self.values[0]
        return self.values[bisect_right(self.edges, score) - 1]


@dataclass
class Calibration:
    fields: dict[str, Isotonic]
    overall: Isotonic
    samples: int
    basis: tuple
    built_at: str

    def confidence(self, field_name: str, score: float) -> float:
        curve = self.fields.get(field_name)
        return curve(score) if curve is not None and curve.samples >= MIN_CALIBRATION_RECORDS else score * UNCALIBRATED_DISCOUNT


# ---------------------------------------------------------------------------
# Deciding one field
# ---------------------------------------------------------------------------
@dataclass
class Candidate:
    value: str
    score: float
    votes: int = 0
    vote_weight: float = 0.0
    reasons: list[str] = field(default_factory=list)


def _decide(
    votes: Mapping[str, float],
    vote_counts: Mapping[str, int],
    vote_total: float,
    lexicon: Mapping[str, float],
    *,
    allowed: Iterable[str] | None = None,
    said_weight: float = 1.0,
) -> tuple[list[Candidate], float]:
    """Candidates for one field, best first, and the raw score of the best.

    The votes give each value a share of the similar work orders; what the work
    order says gives it a share of the evidence read off it. The two are blended
    by how much of each there is, and the best value's blend is then scaled by
    how much evidence there is in all, so a lone weak vote never reads as sure.
    ``said_weight`` is how much more what the work order says counts than the
    votes, for a field where saying it outright is the stronger evidence.
    """

    allowed_set = set(allowed) if allowed is not None else None
    trust = 1.0 - math.exp(-vote_total / VOTE_SCALE) if vote_total > 0 else 0.0
    positive = {value: 1.0 - math.exp(-score) for value, score in lexicon.items() if score > 0}
    if allowed_set is not None:
        positive = {value: score for value, score in positive.items() if value in allowed_set}
    said_total = sum(positive.values())
    strongest = max(positive.values(), default=0.0)
    said = strongest * said_weight
    values = set(votes) | set(positive)
    if allowed_set is not None:
        values &= allowed_set
    candidates = []
    for value in values:
        share = votes.get(value, 0.0) / vote_total if vote_total > 0 else 0.0
        stated = positive.get(value, 0.0) / said_total if said_total > 0 else 0.0
        blend = (trust * share + said * stated) / (trust + said) if trust + said > 0 else 0.0
        if lexicon.get(value, 0.0) < 0:
            # The work order says it was not so: "no leaks".
            blend *= 0.4
        candidates.append(Candidate(value=value, score=blend, votes=vote_counts.get(value, 0), vote_weight=votes.get(value, 0.0)))
    candidates.sort(key=lambda candidate: (candidate.score, candidate.vote_weight, candidate.value), reverse=True)
    if not candidates:
        return [], 0.0
    strength = 1.0 - (1.0 - trust) * (1.0 - strongest)
    for candidate in candidates:
        candidate.score = candidate.score * (0.4 + 0.6 * strength)
    return candidates, candidates[0].score


def _quote(mention: text.Mention) -> str:
    where = text.FIELD_LABELS.get(mention.field, mention.field)
    return f"“{mention.phrase}” in the {where}"


# ---------------------------------------------------------------------------
# Context for one asset
# ---------------------------------------------------------------------------
@dataclass
class SubAssetOption:
    number: str
    name: str
    label: str
    words: frozenset[str]


@dataclass
class AssetContext:
    asset_number: str
    asset_name: str
    options: list[SubAssetOption]

    @classmethod
    def build(cls, tree: AssetTree, asset_number: str) -> "AssetContext":
        own_name = tree.name(asset_number)
        parent_words = {
            token
            for value in (own_name, asset_number)
            for clause in text.clauses(value)
            for token in text.tokens(clause)
        }
        options = []
        for option in tree.descendants(asset_number):
            number = option["asset_number"]
            number_words = {token for clause in text.clauses(number) for token in text.tokens(clause)}
            words = frozenset(
                token
                for clause in text.clauses(option.get("asset_name") or "")
                for token in text.tokens(clause)
                if text.is_content(token) and token not in parent_words and token not in number_words
            )
            options.append(SubAssetOption(number, option.get("asset_name") or "", option.get("label") or number, words))
        return cls(asset_number=asset_number, asset_name=own_name, options=options)


# ---------------------------------------------------------------------------
# The helper's knowledge, kept per database until it changes
# ---------------------------------------------------------------------------
@dataclass
class _Knowledge:
    corpus_key: tuple | None = None
    corpus: Corpus | None = None
    codebook: std.CodeBook | None = None
    labels_token: Any = None
    labels: dict[int, Label] | None = None
    calibration: Calibration | None = None
    calibrating: bool = False


# Whether a suggestion may start the calibration in the background (tests turn it off).
BACKGROUND_CALIBRATION = True

_KNOWLEDGE: dict[str, _Knowledge] = {}
_KNOWLEDGE_LOCK = threading.Lock()
_BUILD_LOCK = threading.Lock()


def _codebook_key(codebook: std.CodeBook) -> tuple:
    return tuple(
        (entry.code_list, entry.code, entry.name, entry.description, entry.semantic, entry.is_active)
        for code_list in std.CODE_LISTS
        for entry in codebook.entries(code_list, include_inactive=True)
    )


def reset_cache() -> None:
    """Forget every index and calibration (for tests)."""

    with _KNOWLEDGE_LOCK:
        _KNOWLEDGE.clear()


# Code texts' meaning vectors, for the similar-code check: a few thousand codes'
# worth, by their text, so renaming a code reads it afresh.
CODE_VECTORS_KEPT = 5000
_CODE_VECTORS: dict[str, Any] = {}
_CODE_VECTORS_LOCK = threading.Lock()


class DispositionHelper:
    """Suggestions, batch runs and the accuracy report, over the disposition standard's service."""

    def __init__(self, records: "StandardDispositionService") -> None:
        self.records = records
        self.life = records.life

    # ------------------------------------------------------------------ #
    # Knowledge
    # ------------------------------------------------------------------ #
    def _state(self) -> _Knowledge:
        key = str(getattr(self.life, "db_path", id(self.life)))
        with _KNOWLEDGE_LOCK:
            state = _KNOWLEDGE.get(key)
            if state is None:
                state = _KNOWLEDGE[key] = _Knowledge()
            return state

    def knowledge(self, *, token: Any = None) -> tuple[Corpus, dict[int, Label], std.CodeBook]:
        """The index, the decisions and the code lists as the database reads now.

        ``token`` is GREMLIN.db's change token, read before this: while it is
        unchanged, so is everything here. The index is rebuilt only when the
        work orders' text or the code lists change (a sync, a new code); the
        decisions are re-read whenever anything was written, which is one narrow
        query. The rows are read first and the index built after, so no read
        lock is held while it is.
        """

        state = self._state()
        if token is not None and state.labels_token == token and state.corpus is not None and state.labels is not None:
            return state.corpus, state.labels, state.codebook
        with _BUILD_LOCK:
            if token is not None and state.labels_token == token and state.corpus is not None:
                return state.corpus, state.labels, state.codebook
            with self.life.read_transaction() as conn:
                codebook = load_codebook(conn)
                fingerprint = tuple(conn.execute(
                    "SELECT COUNT(*), MAX(mapped_record_id), MAX(mapped_at) FROM mapped_cmms_record"
                ).fetchone())
                corpus_key = (fingerprint, _codebook_key(codebook))
                rows = conn.execute(_CORPUS_SQL).fetchall() if state.corpus is None or state.corpus_key != corpus_key else None
                labels = load_labels(conn)
            if rows is not None:
                corpus = Corpus(rows, text.build_lexicon(codebook))
                self._attach_meaning(corpus, rows)
                state.corpus, state.corpus_key = corpus, corpus_key
                state.calibration = None
            state.codebook = codebook
            state.labels = labels
            state.labels_token = token
            return state.corpus, labels, codebook

    def _attach_meaning(self, corpus: Corpus, rows: Sequence[Mapping[str, Any]]) -> None:
        """Give the index its meaning vectors, when the model is installed and meaning matching is on."""

        db_path = getattr(self.life, "db_path", None)
        if db_path is None:
            corpus.meaning_status = {"enabled": False, "model": embedding.MODEL_NAME, "reason": "No database path."}
            return
        encoder, status = embedding.availability(db_path, load=embedding.LOADER)
        corpus.meaning_status = status
        if encoder is None:
            return
        config = embedding.settings(db_path)
        corpus.meaning = embedding.MeaningIndex(
            corpus.ids, [embedding.embedding_text(row) for row in rows], encoder,
            embedding.VectorCache(config["cache_path"]),
        )

    def meaning_status(self, corpus: Corpus) -> dict[str, Any]:
        """Whether meaning matching is on for this index, why not, and how far it has read."""

        status = dict(corpus.meaning_status)
        if corpus.meaning is not None:
            status.update(corpus.meaning.status())
        return status

    def _calibration(self, corpus: Corpus, labels: Mapping[int, Label], codebook: std.CodeBook, tree: AssetTree,
                     *, wait: bool) -> Calibration | None:
        """The calibration for this index, refitted when the decisions have grown by a tenth.

        Fitting re-reads a few hundred work orders, which takes seconds on a
        plant's history, so a suggestion does not wait for it (``wait=False``):
        it starts in the background and the suggestion is scored uncalibrated,
        and discounted, meanwhile. A batch run and the accuracy report wait.
        """

        state = self._state()
        engineered = sum(1 for label in labels.values() if label.source != "MIGRATION")
        current = state.calibration
        # Refitted, too, once every work order's meaning has been read: the
        # scores read differently with it than with part of it.
        if (current is not None and current.basis[0] is corpus and current.basis[2] == corpus.meaning_signature()
                and engineered < current.basis[1] * 1.1 + 5):
            return current
        if not wait:
            if not BACKGROUND_CALIBRATION:
                return current if current is not None and current.basis[0] is corpus else None
            with _KNOWLEDGE_LOCK:
                if state.calibrating:
                    return current if current is not None and current.basis[0] is corpus else None
                state.calibrating = True
            thread = threading.Thread(
                target=self._calibrate_in_background, args=(corpus, dict(labels), codebook, tree), daemon=True
            )
            thread.start()
            return current if current is not None and current.basis[0] is corpus else None
        calibration = self._fit_calibration(corpus, labels, codebook, tree)
        state.calibration = calibration
        return calibration

    def _calibrate_in_background(self, corpus: Corpus, labels: dict[int, Label], codebook: std.CodeBook, tree: AssetTree) -> None:
        state = self._state()
        try:
            calibration = self._fit_calibration(corpus, labels, codebook, tree)
            if state.corpus is corpus:
                state.calibration = calibration
        except Exception:  # noqa: BLE001 - a failed calibration leaves suggestions uncalibrated
            pass
        finally:
            with _KNOWLEDGE_LOCK:
                state.calibrating = False

    def _fit_calibration(self, corpus: Corpus, labels: Mapping[int, Label], codebook: std.CodeBook,
                         tree: AssetTree) -> Calibration:
        engineered = sum(1 for label in labels.values() if label.source != "MIGRATION")
        signature = corpus.meaning_signature()
        trials = self._replay(corpus, labels, codebook, tree, sample=CALIBRATION_SAMPLE)
        return self._calibration_from(trials, basis=(corpus, engineered, signature))

    def _calibration_from(self, trials: Sequence[Mapping[str, Any]], *, basis: tuple) -> Calibration:
        """Each field's curve from replayed work orders, then the overall one: from the least sure field to all right."""

        fields = {
            field_name: Isotonic.fit([(trial["scores"][field_name], trial["right"][field_name])
                                      for trial in trials if field_name in trial["right"]])
            for field_name in SCORED_FIELDS
        }
        draft = Calibration(fields=fields, overall=Isotonic(), samples=len(trials),
                            basis=basis, built_at=datetime.now().isoformat(timespec="seconds"))
        draft.overall = Isotonic.fit([
            (self._overall(trial["scores"], trial["category"], draft), trial["right"]["all"]) for trial in trials
        ])
        return draft

    @classmethod
    def _overall_confidence(cls, scores: Mapping[str, float], category: str | None, calibration: Calibration | None) -> float:
        """How likely every field the disposition needs is right.

        Read off the overall calibration (from the least sure field to how often
        all of them were right), but never above the least sure field itself: a
        disposition is never surer than its weakest part, whatever the record of
        past ones says.
        """

        weakest = cls._overall(scores, category, calibration)
        if calibration is not None and calibration.overall.samples >= MIN_CALIBRATION_RECORDS:
            return min(weakest, calibration.overall(weakest))
        return weakest

    @staticmethod
    def _overall(scores: Mapping[str, float], category: str | None, calibration: Calibration | None) -> float:
        """The least sure of the fields a disposition in ``category`` needs, calibrated."""

        needed = ["disposition_category"]
        if category in std.CODED_CATEGORIES:
            needed += ["sub_asset", *CODED_FIELDS]
        values = []
        for field_name in needed:
            score = scores.get(field_name)
            if score is None:
                return 0.0
            values.append(calibration.confidence(field_name, score) if calibration else score * UNCALIBRATED_DISCOUNT)
        return min(values) if values else 0.0

    # ------------------------------------------------------------------ #
    # Prediction
    # ------------------------------------------------------------------ #
    def _predict(
        self,
        record: Mapping[str, Any],
        corpus: Corpus,
        labels: Mapping[int, Label],
        codebook: std.CodeBook,
        context: AssetContext,
        *,
        legacy: str | None = None,
        use_meaning: bool = True,
    ) -> dict[str, Any]:
        """What the evidence says about one work order, field by field, uncalibrated.

        ``record`` carries the work order's text, ``mapped_record_id``,
        ``asset_number``, ``record_class`` and the date it was found
        (``found``). Its own disposition, if it has one, is never a vote: it is
        read as though it had never been dispositioned. ``use_meaning=False``
        finds the similar work orders by their wording alone (for the accuracy
        report's comparison).
        """

        mapped_record_id = int(record["mapped_record_id"])
        texts = record_texts(record)
        if legacy:
            texts["legacy"] = legacy
        record_class = (record.get("record_class") or "").upper() or None
        reading = text.read_work_order(texts, corpus.lexicon, record_class=record_class, speller=corpus.speller)
        query = corpus.vector(reading)
        own_position = corpus.position.get(mapped_record_id)
        worded = corpus.search(query, exclude=own_position)
        hits, parts = self._similar(corpus, query, texts, own_position, worded, use_meaning=use_meaning)
        day = _day_number(record.get("found"))
        neighbours, duplicates = self._neighbours(hits, corpus, labels, context, day)
        # The category and the sub-asset are voted on by wording alone. Meaning
        # finds work orders about the same thing, which is what the codes need;
        # but a check of the oven's intake screen and a clogged intake screen
        # mean nearly the same and are different kinds of event, and the words
        # ("check", "PM", the record class) are what tell them apart. Where is
        # in names and numbers ("S09", "dry-off oven"), which are words too.
        worded_neighbours = neighbours if hits is worded else self._neighbours(worded, corpus, labels, context, None)[0]

        evidence = self._lexicon_evidence(reading, corpus, codebook)
        fields: dict[str, Any] = {}

        # Category first: it decides which fields the rest are read for.
        votes: dict[str, float] = {}
        counts: dict[str, int] = {}
        total = 0.0
        for _, _, label, weight in worded_neighbours:
            total += weight
            votes[label.disposition_category] = votes.get(label.disposition_category, 0.0) + weight
            counts[label.disposition_category] = counts.get(label.disposition_category, 0) + 1
        said = dict(evidence.get(text.CATEGORY_KEY, {}))
        if record_class in std.PM_RECORD_CLASSES:
            said[std.PM_RESET] = said.get(std.PM_RESET, 0.0) + 0.8
        elif record_class in ("PARTS_ORDER", "ADMINISTRATIVE", "PROJECT_WORK"):
            said[std.NON_FAILURE] = said.get(std.NON_FAILURE, 0.0) + 0.8
        elif record_class == "CORRECTIVE_WO":
            said[std.FAILURE] = said.get(std.FAILURE, 0.0) + 0.2
        candidates, raw = _decide(votes, counts, total, said, allowed=SUGGESTED_CATEGORIES)
        voted = sum(counts.values())
        for candidate in candidates:
            if candidate.votes:
                candidate.reasons.append(f"{candidate.votes} of {voted} similar work orders")
            candidate.reasons.extend(_quote(m) for m in evidence["mentions"].get(f"{text.CATEGORY_KEY}:{candidate.value}", [])[:2])
            if candidate.value == std.PM_RESET and record_class in std.PM_RECORD_CLASSES:
                candidate.reasons.append(f"record class {record_class}")
        fields["disposition_category"] = (candidates, raw)
        category = candidates[0].value if candidates else None

        # Sub-asset.
        fields["sub_asset"] = self._sub_asset(reading, record, context, worded_neighbours, corpus)

        # The coded fields, read for the category the work order looks like.
        for field_name, code_list in CODED_FIELDS.items():
            votes, counts, total = {}, {}, 0.0
            for _, _, label, weight in neighbours:
                value = getattr(label, field_name)
                if value is None or codebook.entry(code_list, value) is None:
                    continue
                if label.disposition_category not in std.CODED_CATEGORIES and label.disposition_category != std.HELD:
                    continue
                if category is not None and label.disposition_category != category:
                    weight *= OTHER_CATEGORY_WEIGHT
                total += weight
                votes[value] = votes.get(value, 0.0) + weight
                counts[value] = counts.get(value, 0) + 1
            allowed = [entry.code for entry in codebook.entries(code_list)]
            if field_name == "failure_mode_code" and category == std.FAILURE:
                # No fault found is never a failure (normalize_disposition refuses it).
                allowed = [code for code in allowed if not codebook.has_semantic(code_list, code, std.SEMANTIC_NO_FAULT_FOUND)]
            candidates, raw = _decide(votes, counts, total, evidence.get(code_list, {}), allowed=allowed)
            voted = sum(counts.values())
            for candidate in candidates:
                if candidate.votes:
                    candidate.reasons.append(f"{candidate.votes} of {voted} similar work orders")
                candidate.reasons.extend(_quote(m) for m in evidence["mentions"].get(f"{code_list}:{candidate.value}", [])[:2])
                if f"{code_list}:{candidate.value}" in evidence["described"]:
                    candidate.reasons.append("words from its description")
            fields[field_name] = (candidates, raw)

        return {
            "reading": reading,
            "fields": fields,
            "category": category,
            "neighbours": neighbours,
            "duplicates": duplicates,
            "parts": parts,
            "day": day,
            "evidence": evidence,
            "corpus": corpus,
        }

    @staticmethod
    def _neighbours(hits: Sequence[tuple[int, float]], corpus: Corpus, labels: Mapping[int, Label], context: AssetContext,
                    day: int | None) -> tuple[list[tuple[int, float, Label, float]], list[tuple[int, float]]]:
        """The dispositioned work orders that vote, with their weights, and any likely duplicates.

        Each vote weighs its similarity squared, more on the same asset and less
        for a migrated REL decision. A likely duplicate is one on the same asset,
        found within DUPLICATE_DAYS, that reads much the same (``day`` None
        looks for none).
        """

        neighbours: list[tuple[int, float, Label, float]] = []
        duplicates: list[tuple[int, float]] = []
        for position, similarity in hits:
            if similarity <= 0.02:
                break
            same_asset = corpus.assets[position] == context.asset_number
            if (same_asset and similarity >= DUPLICATE_SIMILARITY and day is not None
                    and corpus.days[position] is not None and abs(corpus.days[position] - day) <= DUPLICATE_DAYS
                    and len(duplicates) < 3):
                duplicates.append((position, similarity))
            if len(neighbours) >= NEIGHBOURS:
                if day is None or len(duplicates) >= 3 or similarity < DUPLICATE_SIMILARITY:
                    break
                continue
            label = labels.get(corpus.ids[position])
            if label is None:
                continue
            weight = similarity * similarity
            weight *= SAME_ASSET_WEIGHT if same_asset else 1.0
            weight *= MIGRATED_WEIGHT if label.source == "MIGRATION" else 1.0
            neighbours.append((position, similarity, label, weight))
        return neighbours, duplicates

    @staticmethod
    def _similar(corpus: Corpus, query: Mapping[int, float], texts: Mapping[str, Any], own_position: int | None,
                 worded: list[tuple[int, float]], *, use_meaning: bool,
                 ) -> tuple[list[tuple[int, float]], dict[int, tuple[float, float | None]]]:
        """Work orders by how alike they are to this one, most alike first, and each one's wording and meaning parts.

        ``worded`` is the TF-IDF search's best. With meaning matching, the best
        by wording and the best by meaning are pooled and each is scored on
        both, so a work order that says the same thing in other words is found
        even when it shares none of them. Wording keeps most of the say
        (WORDING_SHARE): on the synthetic plant, more meaning than that found
        work orders about the same part but a different fault.
        """

        meaning = corpus.meaning if use_meaning else None
        if meaning is None:
            return worded, {position: (similarity, None) for position, similarity in worded}
        vector = meaning.vector_for(own_position, embedding.embedding_text(texts))
        scores = meaning.similarities(vector)
        wording = dict(worded)
        pool = set(wording)
        pool.update(position for position, _ in meaning.nearest(scores, CANDIDATES_RESCORED, exclude=own_position))
        parts: dict[int, tuple[float, float | None]] = {}
        blended = []
        for position in pool:
            words = wording[position] if position in wording else corpus.similarity(query, position)
            alike = float(scores[position])
            alike_part = None if math.isnan(alike) else alike
            parts[position] = (words, alike_part)
            if alike_part is None:
                blended.append((position, words))
                continue
            meant = min(1.0, max(0.0, (alike_part - MEANING_FLOOR) / (1.0 - MEANING_FLOOR)))
            blended.append((position, WORDING_SHARE * words + (1.0 - WORDING_SHARE) * meant))
        blended.sort(key=lambda item: item[1], reverse=True)
        return blended, parts

    @staticmethod
    def _lexicon_evidence(reading: text.Reading, corpus: Corpus, codebook: std.CodeBook) -> dict[str, Any]:
        """What the work order says outright, as a score per code and per category.

        Keyed by code list (and ``CATEGORY``), each a score per value; beside
        them, ``mentions`` (the phrases read for each "LIST:CODE", to quote) and
        ``described`` (the codes whose descriptions the work order's words match).
        """

        scores: dict[str, dict[str, float]] = {}
        mentions: dict[str, list[text.Mention]] = {}
        renewing_action = any(
            not mention.negated and mention.key.startswith(f"{std.ACTION}:")
            and codebook.renews(mention.key.split(":", 1)[1])
            for mention in reading.mentions
        )
        for mention in reading.mentions:
            code_list, value = mention.key.split(":", 1)
            weight = mention.weight * text.role_weight(code_list, mention.field)
            if renewing_action and (
                mention.key == f"{text.CATEGORY_KEY}:{std.NON_FAILURE}"
                or (code_list == std.FAILURE_MODE and codebook.has_semantic(code_list, value, std.SEMANTIC_NO_FAULT_FOUND))
            ):
                # "Tested OK" after "replaced the belt" is the check after a fix,
                # not a fault that could not be found.
                weight *= 0.3
            if mention.negated:
                weight = -0.6 * weight
            bucket = scores.setdefault(code_list, {})
            bucket[value] = bucket.get(value, 0.0) + weight
            if not mention.negated:
                mentions.setdefault(mention.key, []).append(mention)
        # A code's description is weaker evidence: each of its rarer words the
        # work order uses adds a little.
        described: set[str] = set()
        words = set().union(*reading.content.values()) if reading.content else set()
        if words:
            typical = math.log(corpus.size + 1) * 0.5 + 1.0
            for key, description in corpus.lexicon.descriptions.items():
                shared = description & words
                if not shared:
                    continue
                strength = sum(min(1.0, corpus.word_idf(word) / typical) for word in shared)
                if strength < 0.5:
                    continue
                code_list, value = key.split(":", 1)
                bucket = scores.setdefault(code_list, {})
                bucket[value] = bucket.get(value, 0.0) + min(0.6, 0.25 * strength)
                described.add(key)
        scores["mentions"] = mentions  # type: ignore[assignment]
        scores["described"] = described  # type: ignore[assignment]
        return scores

    @staticmethod
    def _sub_asset(reading: text.Reading, record: Mapping[str, Any], context: AssetContext,
                   neighbours: Sequence[tuple[int, float, Label, float]], corpus: Corpus) -> tuple[list[Candidate], float]:
        """Where on the asset: its number or name in the work order, the asset it was logged on, and the votes.

        The whole asset is the value "" (no sub-asset).
        """

        if not context.options:
            return [Candidate(value="", score=1.0, reasons=[f"{context.asset_number} has no sub-assets"])], 1.0
        said: dict[str, float] = {}
        reasons: dict[str, list[str]] = {}
        known = {option.number for option in context.options}
        for word in dict.fromkeys(reading.words):
            if not (1 <= sum(ch.isalpha() for ch in word) <= 3 and word[-1].isdigit() and word[0].isalpha() and len(word) <= 6):
                continue
            matches = [option for option in context.options if std._sub_asset_token_matches(option.number, word, option.name)]
            for option in matches:
                said[option.number] = said.get(option.number, 0.0) + 1.6 / len(matches)
                reasons.setdefault(option.number, []).append(f"“{word.upper()}” in the work order")
        words = set().union(*reading.content.values()) if reading.content else set()
        for option in context.options:
            if not option.words:
                continue
            shared = option.words & words
            if not shared:
                continue
            share = len(shared) / len(option.words)
            if share >= 1.0:
                said[option.number] = said.get(option.number, 0.0) + 1.4
                reasons.setdefault(option.number, []).append(f"its name, “{option.name}”, in the work order")
            elif share >= 0.5:
                said[option.number] = said.get(option.number, 0.0) + 0.7 * share
                reasons.setdefault(option.number, []).append(f"part of its name, “{option.name}”, in the work order")
        logged = (record.get("asset_name") or "").strip().casefold()
        if logged and logged not in (context.asset_name.casefold(), context.asset_number.casefold()):
            for option in context.options:
                if logged in (option.name.casefold(), option.label.casefold()):
                    said[option.number] = said.get(option.number, 0.0) + 2.5
                    reasons.setdefault(option.number, []).append("the work order is logged against it")
        votes: dict[str, float] = {}
        counts: dict[str, int] = {}
        total = 0.0
        for position, _, label, weight in neighbours:
            if corpus.assets[position] != context.asset_number:
                continue
            value = label.sub_asset or ""
            if value and value not in known:
                continue
            if label.disposition_category not in std.CODED_CATEGORIES and label.disposition_category != std.HELD:
                continue
            total += weight
            votes[value] = votes.get(value, 0.0) + weight
            counts[value] = counts.get(value, 0) + 1
        # A sub-asset named outright ("S09", "dry-off oven", the asset it was
        # logged on) outweighs similar work orders, which are similar for what
        # failed more than for where.
        candidates, raw = _decide(votes, counts, total, said, allowed=[*known, ""], said_weight=2.5)
        voted = sum(counts.values())
        for candidate in candidates:
            if candidate.votes:
                candidate.reasons.append(f"{candidate.votes} of {voted} similar work orders on this asset")
            candidate.reasons.extend(reasons.get(candidate.value, [])[:2])
        if not candidates:
            return [Candidate(value="", score=0.2, reasons=["nothing places it on a sub-asset"])], 0.2
        return candidates, raw

    # ------------------------------------------------------------------ #
    # Suggestions
    # ------------------------------------------------------------------ #
    def _context(self, tree: AssetTree, asset_number: str, cache: dict[str, AssetContext]) -> AssetContext:
        context = cache.get(asset_number)
        if context is None:
            context = cache[asset_number] = AssetContext.build(tree, asset_number)
        return context

    def suggest(self, mapped_record_id: int) -> dict[str, Any]:
        """The helper's disposition for one work order, field by field, with its reasons."""

        token = self.life.change_token()
        corpus, labels, codebook = self.knowledge(token=token)
        with self.life.read_transaction() as conn:
            tree = self.records.asset_tree(conn, token=token)
            row = conn.execute(
                f"SELECT {self.records.RECORD_COLUMNS} {self.records.RECORD_FROM} WHERE m.mapped_record_id = ?",
                (int(mapped_record_id),),
            ).fetchone()
            if row is None:
                raise LookupError("That record was not found.")
            record = self.records.record_dict(row, codebook)
        calibration = self._calibration(corpus, labels, codebook, tree, wait=False)
        return self._suggestion(record, corpus, labels, codebook, tree, calibration, {})

    def _suggestion(
        self,
        record: Mapping[str, Any],
        corpus: Corpus,
        labels: Mapping[int, Label],
        codebook: std.CodeBook,
        tree: AssetTree,
        calibration: Calibration | None,
        contexts: dict[str, AssetContext],
    ) -> dict[str, Any]:
        asset_number = record["asset_number"] or ""
        context = self._context(tree, asset_number, contexts)
        disposition = record.get("disposition") or {}
        legacy = " ".join(
            str(value) for value in (disposition.get("legacy_failure_mode"), disposition.get("legacy_failure_mechanism")) if value
        )
        found = record.get("created_date") or record.get("completed_date")
        prediction = self._predict({**record, "found": found}, corpus, labels, codebook, context, legacy=legacy or None)
        return self._present(record, prediction, codebook, tree, calibration)

    def _confidence(self, calibration: Calibration | None, field_name: str, score: float) -> float:
        if calibration is None:
            return round(score * UNCALIBRATED_DISCOUNT, 4)
        return round(calibration.confidence(field_name, score), 4)

    def _present(
        self,
        record: Mapping[str, Any],
        prediction: Mapping[str, Any],
        codebook: std.CodeBook,
        tree: AssetTree,
        calibration: Calibration | None,
    ) -> dict[str, Any]:
        """A prediction as the page and a batch run read it: values, confidences, reasons and a ready disposition."""

        corpus: Corpus = prediction["corpus"]
        fields_out: dict[str, Any] = {}
        scores: dict[str, float] = {}

        def named(field_name: str, value: str) -> str:
            if field_name == "disposition_category":
                return std.CATEGORY_GUIDE[value]["label"] if value in std.CATEGORY_GUIDE else value
            if field_name == "sub_asset":
                return tree.label(value) if value else f"{record['asset_number']} (whole asset)"
            return codebook.name(CODED_FIELDS[field_name], value) or value

        for field_name in SCORED_FIELDS:
            candidates, raw = prediction["fields"][field_name]
            if not candidates:
                fields_out[field_name] = {"value": None, "name": None, "confidence": 0.0, "alternatives": [], "reasons": []}
                continue
            scores[field_name] = raw
            best = candidates[0]
            fields_out[field_name] = {
                "value": best.value or None,
                "name": named(field_name, best.value),
                "confidence": self._confidence(calibration, field_name, raw),
                "reasons": best.reasons[:4],
                "alternatives": [
                    {
                        "value": candidate.value or None,
                        "name": named(field_name, candidate.value),
                        "confidence": self._confidence(calibration, field_name, candidate.score),
                        "reasons": candidate.reasons[:3],
                    }
                    for candidate in candidates[1:4]
                    if candidate.score > 0.02
                ],
            }
        category = prediction["category"]
        overall = self._overall_confidence(scores, category, calibration)
        neighbours = prediction["neighbours"]

        # What follows from the fields: the cause, the renewed items, the evidence.
        mechanism = fields_out["failure_mechanism_code"]["value"]
        component = fields_out["component_code"]["value"]
        cause = self._cause(prediction, codebook, mechanism, neighbours)
        renewed = self._renewed(category, component, neighbours)
        evidence_value = self._evidence(prediction, component, mechanism, fields_out["failure_mode_code"]["value"])
        fields_out["failure_cause_code"] = cause
        fields_out["renewed_items"] = renewed
        fields_out["evidence"] = evidence_value

        payload: dict[str, Any] = {"mapped_record_id": record["mapped_record_id"], "disposition_category": category}
        if category in std.CODED_CATEGORIES:
            payload.update({
                "sub_asset": fields_out["sub_asset"]["value"],
                **{field_name: fields_out[field_name]["value"] for field_name in CODED_FIELDS},
                "evidence": evidence_value["value"],
                "failure_cause_code": cause["value"],
                "renewed_items": renewed["value"] if category in std.RENEWING_CATEGORIES else [],
                "disposition_note": cause.get("note"),
            })

        similar = [
            {
                "mapped_record_id": corpus.ids[position],
                "task_id": corpus.task_ids[position],
                "title": corpus.titles[position],
                "similarity": round(similarity, 3),
                "wording": round(prediction["parts"].get(position, (similarity, None))[0], 3),
                "meaning": (None if prediction["parts"].get(position, (0, None))[1] is None
                            else round(prediction["parts"][position][1], 3)),
                "same_asset": corpus.assets[position] == record["asset_number"],
                "asset_number": corpus.assets[position],
                "disposition_category": label.disposition_category,
                "sub_asset": label.sub_asset,
                "sub_asset_label": tree.label(label.sub_asset) if label.sub_asset else None,
                "component_code": label.component_code,
                "failure_mechanism_code": label.failure_mechanism_code,
                "failure_mode_code": label.failure_mode_code,
                "action_code": label.action_code,
                "source": label.source,
            }
            for position, similarity, label, _ in neighbours[:SIMILAR_SHOWN]
        ]
        duplicates = [
            {
                "mapped_record_id": corpus.ids[position],
                "task_id": corpus.task_ids[position],
                "title": corpus.titles[position],
                "similarity": round(similarity, 3),
                "days_apart": (abs(corpus.days[position] - prediction["day"])
                               if corpus.days[position] is not None and prediction["day"] is not None else None),
            }
            for position, similarity in prediction["duplicates"]
        ]
        notes = []
        if len(neighbours) < 3:
            notes.append(
                "Few dispositioned work orders are like this one, so these suggestions lean on what it says outright."
            )
        if calibration is None or calibration.samples < MIN_CALIBRATION_RECORDS:
            notes.append(
                "The helper has not yet measured itself against enough dispositioned work orders; its confidences "
                "are estimates, taken down to be safe."
            )
        if duplicates:
            notes.append(
                f"Task {duplicates[0]['task_id']} on this asset reads much the same and was logged within "
                f"{DUPLICATE_DAYS} days: check whether this one duplicates it."
            )
        return {
            "mapped_record_id": record["mapped_record_id"],
            "task_id": record.get("task_id"),
            "helper_version": HELPER_VERSION,
            "calibrated": calibration is not None and calibration.samples >= MIN_CALIBRATION_RECORDS,
            "overall_confidence": round(overall, 4),
            "fields": fields_out,
            "disposition": payload,
            "similar": similar,
            "possible_duplicates": duplicates,
            "notes": notes,
            "prefill_confidence": PREFILL_CONFIDENCE,
            "meaning": self.meaning_status(corpus),
        }

    @staticmethod
    def _cause(prediction: Mapping[str, Any], codebook: std.CodeBook, mechanism: str | None,
               neighbours: Sequence[tuple[int, float, Label, float]]) -> dict[str, Any]:
        """The mechanism's default cause, unless the work order and the similar ones agree on another.

        An override needs a note (spec section 3.2), so one is written saying
        where it came from; the engineer can reword it.
        """

        default = codebook.default_cause(mechanism)
        result: dict[str, Any] = {
            "value": None,
            "default": default,
            "default_name": codebook.name(std.FAILURE_CAUSE, default) if default else None,
            "confidence": None,
            "reasons": [],
        }
        if mechanism is None:
            return result
        said = prediction["evidence"].get(std.FAILURE_CAUSE, {})
        votes: dict[str, float] = {}
        total = 0.0
        for _, _, label, weight in neighbours:
            if label.failure_mechanism_code != mechanism:
                continue
            effective = label.failure_cause_code or default
            if effective is None:
                continue
            total += weight
            votes[effective] = votes.get(effective, 0.0) + weight
        candidates, raw = _decide(votes, {}, total, said, allowed=[entry.code for entry in codebook.entries(std.FAILURE_CAUSE)])
        if not candidates or candidates[0].value == default or raw < 0.45:
            return result
        best = candidates[0]
        mentions = prediction["evidence"]["mentions"].get(f"{std.FAILURE_CAUSE}:{best.value}", [])
        if not mentions:
            # Votes alone never override the default: the record has to say so.
            return result
        quoted = "; ".join(_quote(mention) for mention in mentions[:2])
        result.update({
            "value": best.value,
            "name": codebook.name(std.FAILURE_CAUSE, best.value),
            "confidence": round(raw * UNCALIBRATED_DISCOUNT, 4),
            "reasons": [quoted],
            "note": f"Cause from the work order: {quoted}.",
        })
        return result

    @staticmethod
    def _renewed(category: str | None, component: str | None,
                 neighbours: Sequence[tuple[int, float, Label, float]]) -> dict[str, Any]:
        """For a PM reset, the items the similar PM resets renewed, or the component."""

        if category != std.PM_RESET:
            return {"value": [], "reasons": []}
        votes: dict[str, float] = {}
        total = 0.0
        for _, _, label, weight in neighbours:
            if label.disposition_category != std.PM_RESET:
                continue
            total += weight
            for item in label.renewed_items:
                votes[item] = votes.get(item, 0.0) + weight
        items = [item for item, weight in sorted(votes.items(), key=lambda pair: -pair[1]) if total and weight / total >= 0.5]
        if items:
            return {"value": items, "reasons": ["renewed in most of the similar PM resets"]}
        if component:
            return {"value": [component], "reasons": ["the component"]}
        return {"value": [], "reasons": []}

    @staticmethod
    def _evidence(prediction: Mapping[str, Any], component: str | None, mechanism: str | None,
                  mode: str | None) -> dict[str, Any]:
        """EXPLICIT when the work order names the component and its condition outright, INFERRED otherwise."""

        mentions = prediction["evidence"]["mentions"]
        names_item = component is not None and bool(mentions.get(f"{std.COMPONENT}:{component}"))
        names_condition = (mechanism is not None and bool(mentions.get(f"{std.FAILURE_MECHANISM}:{mechanism}"))) or (
            mode is not None and bool(mentions.get(f"{std.FAILURE_MODE}:{mode}"))
        )
        if names_item and names_condition:
            return {"value": "EXPLICIT", "reasons": ["the work order names the item and its condition"]}
        return {"value": "INFERRED", "reasons": ["read from similar work orders and the work order's wording"]}

    # ------------------------------------------------------------------ #
    # Replaying decided work orders: calibration and the accuracy report
    # ------------------------------------------------------------------ #
    def _replay(
        self,
        corpus: Corpus,
        labels: Mapping[int, Label],
        codebook: std.CodeBook,
        tree: AssetTree,
        *,
        sample: int,
        asset_number: str | None = None,
        seed: int = 14224,
        use_meaning: bool = True,
    ) -> list[dict[str, Any]]:
        """Disposition decided work orders as if they were new, each without its own decision.

        Only engineers' decisions are replayed: a migrated record's codes are the
        old screens' and the helper's own are what is being measured.
        """

        eligible = [
            mapped_record_id
            for mapped_record_id, label in labels.items()
            if label.source == "ENGINEER"
            and label.disposition_category in (*std.CODED_CATEGORIES, std.HELD, std.DUPLICATE_MISFILED)
            and mapped_record_id in corpus.position
            and (asset_number is None or corpus.assets[corpus.position[mapped_record_id]] == asset_number)
        ]
        eligible.sort()
        chosen = random.Random(seed).sample(eligible, sample) if len(eligible) > sample else eligible
        if not chosen:
            return []
        with self.life.connect() as conn:
            rows = []
            for start in range(0, len(chosen), 500):
                chunk = chosen[start:start + 500]
                rows.extend(conn.execute(
                    f"{_CORPUS_SQL} AND m.mapped_record_id IN ({', '.join('?' * len(chunk))})", chunk
                ).fetchall())
        contexts: dict[str, AssetContext] = {}
        trials = []
        for row in rows:
            mapped_record_id = int(row["mapped_record_id"])
            truth = labels[mapped_record_id]
            record = {key: row[key] for key in row.keys()}
            context = self._context(tree, record["asset_number"] or "", contexts)
            prediction = self._predict(record, corpus, labels, codebook, context, use_meaning=use_meaning)
            scores: dict[str, float] = {}
            right: dict[str, bool] = {}
            predicted: dict[str, Any] = {}
            for field_name in SCORED_FIELDS:
                candidates, raw = prediction["fields"][field_name]
                value = candidates[0].value if candidates else None
                predicted[field_name] = value
                scores[field_name] = raw if candidates else 0.0
            right["disposition_category"] = predicted["disposition_category"] == truth.disposition_category
            if truth.disposition_category in std.CODED_CATEGORIES:
                right["sub_asset"] = (predicted["sub_asset"] or "") == (truth.sub_asset or "")
                for field_name in CODED_FIELDS:
                    right[field_name] = predicted[field_name] == getattr(truth, field_name)
                right["all"] = all(right[field_name] for field_name in SCORED_FIELDS)
            else:
                right["all"] = False
            trials.append({
                "mapped_record_id": mapped_record_id,
                "asset_number": record["asset_number"],
                "task_id": record["task_id"],
                "title": record["task_name"] or record["request_title"],
                "truth": truth,
                "predicted": predicted,
                "scores": scores,
                "right": right,
                "category": predicted["disposition_category"],
            })
        return trials

    def evaluate(self, asset_number: str | None = None, *, sample: int = CALIBRATION_SAMPLE) -> dict[str, Any]:
        """How often the helper agrees with the team, field by field and at each confidence.

        The decided work orders are replayed as if new. Their confidences come
        from a calibration fitted on the *other* half of them, so the report
        never grades the helper on the work orders it learnt its confidences
        from. With ``asset_number`` only that asset's work orders are replayed
        (the helper still learns from every asset).
        """

        token = self.life.change_token()
        corpus, labels, codebook = self.knowledge(token=token)
        with self.life.read_transaction() as conn:
            tree = self.records.asset_tree(conn, token=token)
        trials = self._replay(corpus, labels, codebook, tree, sample=sample, asset_number=asset_number)
        in_use = self.in_use(asset_number)
        engineered = sum(1 for label in labels.values() if label.source == "ENGINEER")
        meaning = self.meaning_status(corpus)
        report: dict[str, Any] = {
            "asset_number": asset_number,
            "replayed": len(trials),
            "learnt_from": engineered,
            "enough": False,
            "fields": [],
            "thresholds": [],
            "mistakes": [],
            "in_use": in_use,
            "meaning": meaning,
            "without_meaning": None,
            "helper_version": HELPER_VERSION,
        }
        if len(trials) < 2:
            return report
        graded = self._graded(trials)
        report.update({
            "replayed": len(graded["rows"]),
            "enough": len(graded["rows"]) >= MIN_CALIBRATION_RECORDS,
            "fields": graded["fields"],
            "thresholds": graded["thresholds"],
            "mistakes": graded["mistakes"],
        })
        if corpus.meaning is not None:
            # The same work orders matched by their wording alone: what meaning
            # matching adds, on this plant's own records.
            plain = self._graded(self._replay(
                corpus, labels, codebook, tree, sample=sample, asset_number=asset_number, use_meaning=False,
            ))
            report["without_meaning"] = {
                "fields": [{key: row[key] for key in ("field", "label", "accuracy")} for row in plain["fields"]],
                "thresholds": plain["thresholds"],
            }
        return report

    def _graded(self, trials: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        """Replayed work orders graded: each with calibrated confidences, and the report's tables from them.

        Each half's confidences come from a calibration fitted on the other
        half, exactly as the live calibration is fitted on all of them.
        """

        halves = [list(trials[0::2]), list(trials[1::2])]
        rows = []
        for half, other in ((halves[0], halves[1]), (halves[1], halves[0])):
            calibration = self._calibration_from(other, basis=(None, 0, None))
            for trial in half:
                confidences = {
                    field_name: calibration.confidence(field_name, trial["scores"][field_name])
                    for field_name in SCORED_FIELDS
                }
                overall = self._overall_confidence(trial["scores"], trial["category"], calibration)
                rows.append({**trial, "confidence": confidences, "overall": overall})
        fields_report = []
        for field_name in SCORED_FIELDS:
            judged = [row for row in rows if field_name in row["right"]]
            if not judged:
                continue
            fields_report.append({
                "field": field_name,
                "label": FIELD_NAMES[field_name],
                "judged": len(judged),
                "accuracy": round(sum(row["right"][field_name] for row in judged) / len(judged), 4),
                "at": [
                    self._at(judged, lambda row, name=field_name: row["confidence"][name],
                             lambda row, name=field_name: row["right"][name], threshold)
                    for threshold in EVALUATION_THRESHOLDS
                ],
            })
        thresholds = [
            {
                **self._at(rows, lambda row: row["overall"], lambda row: row["right"]["all"], threshold),
                "category_right": self._rate(
                    [row for row in rows if row["overall"] >= threshold], lambda row: row["right"]["disposition_category"]
                ),
            }
            for threshold in EVALUATION_THRESHOLDS
        ]
        mistakes = []
        for field_name in ("disposition_category", "component_code", "failure_mechanism_code", "failure_mode_code"):
            pairs: dict[tuple[str, str], int] = {}
            for row in rows:
                if field_name in row["right"] and not row["right"][field_name]:
                    truth = getattr(row["truth"], field_name) or "—"
                    guess = row["predicted"][field_name] or "—"
                    pairs[(guess, truth)] = pairs.get((guess, truth), 0) + 1
            for (guess, truth), count in sorted(pairs.items(), key=lambda item: -item[1])[:3]:
                mistakes.append({"field": field_name, "label": FIELD_NAMES[field_name], "suggested": guess,
                                 "decided": truth, "count": count})
        return {"rows": rows, "fields": fields_report, "thresholds": thresholds, "mistakes": mistakes}

    @staticmethod
    def _rate(rows: Sequence[Any], right) -> float | None:
        return round(sum(1 for row in rows if right(row)) / len(rows), 4) if rows else None

    def _at(self, rows: Sequence[Any], confidence, right, threshold: float) -> dict[str, Any]:
        kept = [row for row in rows if confidence(row) >= threshold]
        return {
            "threshold": threshold,
            "coverage": round(len(kept) / len(rows), 4) if rows else 0.0,
            "count": len(kept),
            "accuracy": self._rate(kept, right),
        }

    def in_use(self, asset_number: str | None = None) -> dict[str, Any]:
        """How the helper's suggestions have fared since it was turned on.

        Two measures. A work order an engineer saved with a suggestion open: did
        they keep each suggested value? And a work order the helper dispositioned
        on its own: when an engineer later saved it, did they change it?
        """

        asset_clause = " AND TRIM(m.asset_number) = ?" if asset_number else ""
        params = [asset_number] if asset_number else []
        assisted = {field_name: [0, 0] for field_name in SCORED_FIELDS}
        assisted_saves = 0
        confirmed = changed = 0
        changed_fields: dict[str, int] = {}
        with self.life.connect() as conn:
            for row in conn.execute(
                f"""
                SELECT w.suggestion_json, w.disposition_category, w.sub_asset, w.component_code,
                       w.failure_mechanism_code, w.failure_mode_code, w.action_code
                FROM wo_disposition w JOIN mapped_cmms_record m ON m.mapped_record_id = w.mapped_record_id
                WHERE w.source = 'ENGINEER' AND w.suggestion_json IS NOT NULL{asset_clause}
                """,
                params,
            ):
                try:
                    suggested = json.loads(row["suggestion_json"]).get("fields") or {}
                except (TypeError, ValueError, AttributeError):
                    continue
                assisted_saves += 1
                for field_name in SCORED_FIELDS:
                    entry = suggested.get(field_name)
                    if not isinstance(entry, dict) or "value" not in entry:
                        continue
                    if field_name != "disposition_category" and row["disposition_category"] not in std.CODED_CATEGORIES:
                        continue
                    assisted[field_name][1] += 1
                    if (entry.get("value") or None) == (row[field_name] or None):
                        assisted[field_name][0] += 1
            for row in conn.execute(
                f"""
                SELECT auto.disposition_category AS a_category, auto.sub_asset AS a_sub_asset,
                       auto.component_code AS a_component_code, auto.failure_mechanism_code AS a_failure_mechanism_code,
                       auto.failure_mode_code AS a_failure_mode_code, auto.action_code AS a_action_code,
                       nxt.disposition_category, nxt.sub_asset, nxt.component_code, nxt.failure_mechanism_code,
                       nxt.failure_mode_code, nxt.action_code
                FROM wo_disposition auto
                JOIN mapped_cmms_record m ON m.mapped_record_id = auto.mapped_record_id
                JOIN wo_disposition nxt ON nxt.wo_disposition_id = (
                    SELECT MIN(later.wo_disposition_id) FROM wo_disposition later
                    WHERE later.mapped_record_id = auto.mapped_record_id AND later.wo_disposition_id > auto.wo_disposition_id
                )
                WHERE auto.source = 'AUTO' AND nxt.source = 'ENGINEER'{asset_clause}
                """,
                params,
            ):
                differing = [
                    field_name for field_name in SCORED_FIELDS
                    if (row[f"a_{'category' if field_name == 'disposition_category' else field_name}"] or None)
                    != (row[field_name] or None)
                ]
                if differing:
                    changed += 1
                    for field_name in differing:
                        changed_fields[field_name] = changed_fields.get(field_name, 0) + 1
                else:
                    confirmed += 1
            pending = conn.execute(
                f"""
                SELECT COUNT(*) FROM wo_disposition w JOIN mapped_cmms_record m ON m.mapped_record_id = w.mapped_record_id
                WHERE w.is_current = 1 AND w.source = 'AUTO'{asset_clause}
                """,
                params,
            ).fetchone()[0]
        return {
            "assisted_saves": assisted_saves,
            "kept": [
                {"field": field_name, "label": FIELD_NAMES[field_name], "kept": kept, "of": of,
                 "rate": round(kept / of, 4) if of else None}
                for field_name, (kept, of) in assisted.items()
            ],
            "auto_reviewed": confirmed + changed,
            "auto_confirmed_unchanged": confirmed,
            "auto_changed": changed,
            "auto_changed_fields": [
                {"field": field_name, "label": FIELD_NAMES[field_name], "count": count}
                for field_name, count in sorted(changed_fields.items(), key=lambda item: -item[1])
            ],
            "auto_pending": int(pending or 0),
        }

    # ------------------------------------------------------------------ #
    # Batch runs
    # ------------------------------------------------------------------ #
    def _backlog(self, conn: sqlite3.Connection, asset_number: str, search: str | None,
                 filters: Mapping[str, Any] | None, codebook: std.CodeBook) -> tuple[list[dict[str, Any]], int]:
        """The not-reviewed work orders the list shows (its search and column filters), newest first."""

        cleaned = self.records.clean_list_filters(filters)
        where, params = self.records._list_where(asset_number, "not_reviewed", search, cleaned)
        total = int(conn.execute(f"SELECT COUNT(*) {self.records.RECORD_FROM} {where}", params).fetchone()[0])
        rows = conn.execute(
            f"""
            SELECT {self.records.RECORD_COLUMNS} {self.records.RECORD_FROM} {where}
            ORDER BY {self.records._list_order(None, None)}
            LIMIT ?
            """,
            [*params, BATCH_LIMIT],
        ).fetchall()
        return [self.records.record_dict(row, codebook) for row in rows], total

    def _batch(self, asset_number: str, *, search: str | None, filters: Mapping[str, Any] | None,
               min_confidence: float) -> dict[str, Any]:
        if not asset_number:
            raise ValueError("Select an Asset Number first.")
        try:
            threshold = float(min_confidence)
        except (TypeError, ValueError):
            raise ValueError("The minimum confidence must be a number between 0.6 and 1.") from None
        if threshold > 1:
            threshold /= 100.0
        if not MIN_BATCH_CONFIDENCE <= threshold <= 1:
            raise ValueError(
                f"The minimum confidence must be between {MIN_BATCH_CONFIDENCE:.0%} and 100%: below that, more of what "
                "the helper saves would need correcting than it saves time."
            )
        token = self.life.change_token()
        corpus, labels, codebook = self.knowledge(token=token)
        with self.life.read_transaction() as conn:
            tree = self.records.asset_tree(conn, token=token)
            self.records._register_labels(conn, tree)
            backlog, total = self._backlog(conn, asset_number, search, filters, codebook)
        calibration = self._calibration(corpus, labels, codebook, tree, wait=True)
        calibrated = calibration is not None and calibration.samples >= MIN_CALIBRATION_RECORDS
        contexts: dict[str, AssetContext] = {}
        suggestions = [self._suggestion(record, corpus, labels, codebook, tree, calibration, contexts) for record in backlog]
        return {
            "threshold": threshold,
            "calibrated": calibrated,
            "backlog_total": total,
            "considered": len(backlog),
            "limited": total > len(backlog),
            "records": backlog,
            "suggestions": suggestions,
        }

    @staticmethod
    def _batch_row(record: Mapping[str, Any], suggestion: Mapping[str, Any], threshold: float) -> dict[str, Any]:
        fields = suggestion["fields"]
        category = suggestion["disposition"].get("disposition_category")
        ready = category in SUGGESTED_CATEGORIES and suggestion["overall_confidence"] >= threshold
        return {
            "mapped_record_id": record["mapped_record_id"],
            "task_id": record.get("task_id"),
            "title": record.get("task_name") or record.get("request_title") or "",
            "found": record.get("created_date") or record.get("completed_date"),
            "confidence": suggestion["overall_confidence"],
            "will_apply": ready,
            "disposition_category": category,
            "sub_asset": fields["sub_asset"]["value"],
            "sub_asset_name": fields["sub_asset"]["name"],
            **{field_name: fields[field_name]["value"] for field_name in CODED_FIELDS},
            "weakest": min(
                (
                    {"field": name, "label": FIELD_NAMES[name], "confidence": fields[name]["confidence"]}
                    for name in (["disposition_category", "sub_asset", *CODED_FIELDS] if category in std.CODED_CATEGORIES
                                 else ["disposition_category"])
                ),
                key=lambda item: item["confidence"],
            ),
            "possible_duplicate": bool(suggestion["possible_duplicates"]),
        }

    def batch_preview(self, asset_number: str, *, search: str | None = None, filters: Mapping[str, Any] | None = None,
                      min_confidence: float = DEFAULT_BATCH_CONFIDENCE) -> dict[str, Any]:
        """What a batch run would do to the list's not-reviewed work orders, without saving anything."""

        batch = self._batch(asset_number, search=search, filters=filters, min_confidence=min_confidence)
        rows = [self._batch_row(record, suggestion, batch["threshold"])
                for record, suggestion in zip(batch["records"], batch["suggestions"])]
        by_category: dict[str, int] = {}
        for row in rows:
            if row["will_apply"]:
                by_category[row["disposition_category"]] = by_category.get(row["disposition_category"], 0) + 1
        rows.sort(key=lambda row: (-row["confidence"], str(row["task_id"])))
        return {
            "asset_number": asset_number,
            "threshold": batch["threshold"],
            "calibrated": batch["calibrated"],
            "backlog_total": batch["backlog_total"],
            "considered": batch["considered"],
            "limited": batch["limited"],
            "would_apply": sum(1 for row in rows if row["will_apply"]),
            "by_category": by_category,
            "rows": rows,
            "histogram": self._histogram([row["confidence"] for row in rows]),
        }

    @staticmethod
    def _histogram(values: Sequence[float]) -> list[dict[str, Any]]:
        bands = [(0.0, 0.5), (0.5, 0.7), (0.7, 0.8), (0.8, 0.9), (0.9, 0.95), (0.95, 1.01)]
        return [
            {"from": low, "to": min(high, 1.0), "count": sum(1 for value in values if low <= value < high)}
            for low, high in bands
        ]

    def batch_apply(self, asset_number: str, *, search: str | None = None, filters: Mapping[str, Any] | None = None,
                    min_confidence: float = DEFAULT_BATCH_CONFIDENCE, run_by: str | None = None) -> dict[str, Any]:
        """Disposition the list's not-reviewed work orders the helper is sure enough of, as one undoable run.

        Each is saved through the standard's own checks, marked AUTO with a
        review flag naming its confidence, and left for an engineer to confirm
        (saving it again confirms it). A work order an engineer dispositioned
        meanwhile is left alone. The suggestions are worked out again here
        rather than taken from the page, so what is saved is what the helper
        says now.
        """

        batch = self._batch(asset_number, search=search, filters=filters, min_confidence=min_confidence)
        if not batch["calibrated"]:
            raise ValueError(
                f"The helper needs at least {MIN_CALIBRATION_RECORDS} work orders dispositioned by engineers to measure "
                "its own accuracy before it may disposition on its own. Use it in suggest mode until then."
            )
        threshold = batch["threshold"]
        items = []
        for record, suggestion in zip(batch["records"], batch["suggestions"]):
            row = self._batch_row(record, suggestion, threshold)
            if not row["will_apply"]:
                continue
            confidence = suggestion["overall_confidence"]
            items.append({
                **suggestion["disposition"],
                "review_flag": (
                    f"Auto-dispositioned by the disposition helper at {confidence:.0%} confidence; "
                    "confirm or correct it, then save."
                ),
                "suggestion_json": self.suggestion_record(suggestion),
            })
        outcome = self.records.save_auto_dispositions(
            items,
            run={
                "asset_number": asset_number,
                "min_confidence": threshold,
                "scope_json": json.dumps({"search": search or "", "filters": self.records.clean_list_filters(filters)}),
                "considered": batch["considered"],
                "helper_version": HELPER_VERSION,
            },
            decided_by=f"Disposition helper ({run_by})" if run_by else "Disposition helper",
            run_by=run_by,
        )
        return {
            "asset_number": asset_number,
            "threshold": threshold,
            "considered": batch["considered"],
            "backlog_total": batch["backlog_total"],
            "limited": batch["limited"],
            **outcome,
        }

    @staticmethod
    def suggestion_record(suggestion: Mapping[str, Any]) -> str:
        """What is kept with a disposition saved from a suggestion: each field's value and confidence."""

        fields = suggestion.get("fields") or {}
        kept = {
            field_name: {"value": (fields.get(field_name) or {}).get("value"),
                         "confidence": (fields.get(field_name) or {}).get("confidence")}
            for field_name in SCORED_FIELDS
            if isinstance(fields.get(field_name), Mapping)
        }
        return json.dumps({
            "v": HELPER_VERSION,
            "overall": suggestion.get("overall_confidence"),
            "fields": kept,
        }, separators=(",", ":"))

    def similar_codes(self, code_list: str, name: str, description: str | None = None) -> dict[str, Any]:
        """The codes already on a list that a new one may simply be, before it is added.

        Read as the helper reads a work order (``text.similar_codes``), and, with
        the local model installed and meaning matching on, by meaning too: a new
        "combustion air blower" finds "Fan / blower" in other words. The model is
        the one the helper's index uses; the codes' vectors are kept by their text.
        """

        code_list = (std.clean_text(code_list) or "").upper()
        if code_list not in std.CODE_LISTS:
            raise ValueError(f"Choose one of the code lists: {', '.join(std.CODE_LISTS)}.")
        codebook = self.records.codebook()
        meaning, status = self._code_meaning()
        matches = text.similar_codes(codebook, code_list, name, description, meaning=meaning)
        return {"code_list": code_list, "matches": matches, "meaning": status}

    def _code_meaning(self) -> tuple[Any, dict[str, Any]]:
        """How alike in meaning a text is to each of some others, by the local model, or None without it."""

        db_path = getattr(self.life, "db_path", None)
        if db_path is None:
            return None, {"enabled": False, "model": embedding.MODEL_NAME, "reason": "No database path."}
        encoder, status = embedding.availability(db_path, load=embedding.LOADER)
        if encoder is None:
            return None, status

        def meaning(asked: str, texts: Sequence[str]) -> list[float]:
            with _CODE_VECTORS_LOCK:
                missing = [textual for textual in dict.fromkeys(texts) if textual not in _CODE_VECTORS]
            if missing:
                for textual, vector in zip(missing, encoder.encode(missing)):
                    with _CODE_VECTORS_LOCK:
                        _CODE_VECTORS[textual] = vector
                        if len(_CODE_VECTORS) > CODE_VECTORS_KEPT:
                            _CODE_VECTORS.pop(next(iter(_CODE_VECTORS)))
            query = encoder.encode([asked])[0]
            with _CODE_VECTORS_LOCK:
                vectors = [_CODE_VECTORS.get(textual) for textual in texts]
            return [float(vector @ query) if vector is not None else 0.0 for vector in vectors]

        return meaning, status

    def status(self) -> dict[str, Any]:
        """Whether meaning matching is on (and how far it has read), and whether the helper has measured itself."""

        corpus, labels, _ = self.knowledge(token=self.life.change_token())
        calibration = self._state().calibration
        return {
            "meaning": self.meaning_status(corpus),
            "learnt_from": sum(1 for label in labels.values() if label.source == "ENGINEER"),
            "calibrated": calibration is not None and calibration.samples >= MIN_CALIBRATION_RECORDS,
            "helper_version": HELPER_VERSION,
        }

    def runs(self, asset_number: str, *, limit: int = 10) -> list[dict[str, Any]]:
        """The asset's batch runs, newest first, with how many of each are still the helper's."""

        with self.life.connect() as conn:
            rows = conn.execute(
                """
                SELECT r.*,
                       (SELECT COUNT(*) FROM wo_disposition w
                        WHERE w.helper_run_id = r.helper_run_id AND w.is_current = 1 AND w.source = 'AUTO') AS still_auto
                FROM disposition_helper_run r
                WHERE r.asset_number = ?
                ORDER BY r.helper_run_id DESC
                LIMIT ?
                """,
                (asset_number, int(limit)),
            ).fetchall()
        return [
            {
                "helper_run_id": row["helper_run_id"],
                "asset_number": row["asset_number"],
                "min_confidence": row["min_confidence"],
                "considered": row["considered"],
                "applied": row["applied"],
                "skipped": row["skipped"],
                "run_by": row["run_by"],
                "run_at": row["run_at"],
                "undone_at": row["undone_at"],
                "undone_by": row["undone_by"],
                "undone_count": row["undone_count"],
                "still_auto": int(row["still_auto"] or 0),
            }
            for row in rows
        ]

    def undo_run(self, helper_run_id: int, *, undone_by: str | None = None) -> dict[str, Any]:
        return self.records.undo_helper_run(int(helper_run_id), undone_by=undone_by)
