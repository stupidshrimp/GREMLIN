"""Find the maintenance team's structured failure write-up in a CMMS payload.

Limble work orders now ask the maintenance teams to fill out four individual
text boxes -- Area Affected, Condition, Cause, Action -- rather than dropping
everything into one free-text completion note. Those four answers are the most
defensible evidence a Weibull analysis has for *why* a record is a failure, so
GREMLIN reads them into named columns instead of leaving them buried in
``raw_json``.

Where they arrive is not fixed. Depending on how a work order was built in
Limble, the same four answers can land as any of:

* top-level task keys -- ``{"Area Affected": "North conveyor"}``
* entries in a custom-field collection -- ``customFields``, ``fields``,
  ``customTags``
* task instruction items, where the box's prompt and the tech's answer are
  *separate keys on one object* -- ``{"description": "Cause", "answer": "..."}``

Rather than pin the reader to one of those shapes, this module searches all of
them and matches on the **label the maintenance team sees**, normalised so that
"Area Affected", "area_affected" and "AREA AFFECTED:" are one field. First hit
wins, and top-level keys are searched before nested collections, so an explicit
task field always beats a same-named entry buried in a checklist.

A payload carrying none of these simply yields nothing, which is what a work
order completed before the template change should do -- those rows keep showing
blank narrative cells rather than inventing content.
"""

from __future__ import annotations

import html
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Iterator


@dataclass(frozen=True)
class NarrativeField:
    """One of the four text boxes, and every label that resolves to it."""

    key: str
    """The ``mapped_cmms_record`` column this field is stored in."""

    label: str
    """The heading the tables show for it."""

    aliases: tuple[str, ...]
    """Normalised labels (see :func:`_normalise`) that mean this field."""


# Ordered as the boxes appear on the work order, because that order is also how
# the narrative reads on every table: where it happened, what was seen, why, and
# what was done about it.
#
# The aliases are deliberately generous about spelling and punctuation -- these
# labels are typed by whoever builds a work-order template, not chosen from a
# list, so "Area Affected", "Area Affect" and "Area Effected" all appear in the
# wild. They stay specific enough not to collide with unrelated task fields: a
# bare "area" is excluded for exactly that reason, since a plant-area field would
# otherwise be read as the failure's affected area.
NARRATIVE_FIELDS: tuple[NarrativeField, ...] = (
    NarrativeField(
        "area_affected",
        "Area Affected",
        ("areaaffected", "areaaffect", "areaeffected", "areaeffect", "affectedarea", "areaimpacted", "impactedarea"),
    ),
    NarrativeField(
        "condition_found",
        "Condition",
        ("condition", "conditionfound", "foundcondition", "conditionobserved", "observedcondition"),
    ),
    NarrativeField(
        "cause",
        "Cause",
        ("cause", "causes", "causefound", "rootcause", "probablecause", "suspectedcause"),
    ),
    NarrativeField(
        "action_taken",
        "Action",
        ("action", "actions", "actiontaken", "actionstaken", "actiontoken", "correctiveaction", "actionperformed", "actioncompleted"),
    ),
)

NARRATIVE_KEYS: tuple[str, ...] = tuple(field.key for field in NARRATIVE_FIELDS)

# Collections that are worth searching before anything else, because this is
# where a CMMS conventionally hangs per-task fields and checklist answers.
# Anything else nested in the payload is still searched, just afterwards.
_PREFERRED_CONTAINER_KEYS = (
    "customFields", "custom_fields", "fields", "customTags", "custom_tags",
    "instructions", "taskInstructions", "task_instructions",
    "checklist", "checklistItems", "checklist_items", "items", "responses", "answers",
)

# On an object that carries the prompt and the answer as two separate keys, the
# prompt is read from the first of these present. The instruction spellings lead
# because Limble uses two of them for the same thing, and the difference is not
# academic -- a task export from the site and the API disagree:
#
#   site export:  {"instructionText": "Affected area", "response": "...",
#                  "type": "text box", "order": "1", "parentID": "47", ...}
#   GET /tasks/{id}/instructions:
#                 {"instruction": "Affected area", "response": "...",
#                  "type": "text box", "parentInstructionID": "47",
#                  "instructionFiles": [], "options": [], ...}
#
# Both are confirmed against this account, so both are read. ``response`` carries
# the answer either way.
_LABEL_KEYS = (
    "instruction", "instructionText", "instruction_text", "name", "label",
    "title", "fieldName", "field_name", "field", "question", "prompt",
    "description", "text",
)

# ... and the answer from the first of these that is not the key already spent on
# the label. The overlap ("text", "description") is intentional: Limble uses
# ``description`` for an instruction's prompt but other shapes use it for the
# answer, so which role a key plays depends on what else the object carries.
_VALUE_KEYS = (
    "value", "answer", "response", "userInput", "user_input", "answerText",
    "answer_text", "content", "input", "data", "text", "description",
)

# How far into nested collections to look. Deep enough for a checklist inside a
# task inside a wrapper, shallow enough that a pathological payload cannot turn
# one record's mapping into a long walk.
_MAX_DEPTH = 4


def _normalise(label: Any) -> str:
    """Reduce a label to its comparable form: lowercase, letters and digits only.

    Everything a human varies without meaning to -- case, spaces, underscores,
    a trailing colon -- drops out, so "Area Affected", "area_affected" and
    "AREA AFFECTED:" all normalise to ``areaaffected``.
    """

    return "".join(char for char in str(label).lower() if char.isalnum())


_ALIAS_TO_KEY: dict[str, str] = {
    alias: field.key for field in NARRATIVE_FIELDS for alias in field.aliases
}


def extract_narrative(raw: Any) -> dict[str, str]:
    """Return ``{column key: text}`` for each narrative box found in ``raw``.

    Only fields that are actually present and non-empty appear in the result, so
    a caller can ``dict.get`` each key and store ``None`` for the rest.
    """

    found: dict[str, str] = {}
    for label, value in _candidate_pairs(raw, depth=0):
        key = _ALIAS_TO_KEY.get(_normalise(label))
        # First hit wins. _candidate_pairs yields in priority order, so this is
        # what keeps a top-level task field ahead of a checklist entry that
        # happens to carry the same label.
        if key is None or key in found:
            continue
        text = _clean(value)
        if text:
            found[key] = text
        if len(found) == len(NARRATIVE_FIELDS):
            break
    return found


def _candidate_pairs(payload: Any, *, depth: int) -> Iterator[tuple[str, Any]]:
    """Yield every ``(label, value)`` the payload offers, best candidates first."""

    if depth > _MAX_DEPTH or not isinstance(payload, Mapping):
        return

    # 1. This object's own scalar keys -- the "Area Affected": "..." shape.
    for key, value in payload.items():
        if _is_text(value):
            yield str(key), value

    # 2. An object that names its own prompt and answer, rather than using the
    #    prompt as a key. Yielded after the plain keys above so a self-describing
    #    wrapper never outranks a direct field on the same object.
    labelled = _labelled_pair(payload)
    if labelled is not None:
        yield labelled

    # 3. Nested collections, conventional locations first.
    collections = [(key, value) for key, value in payload.items() if not _is_text(value)]
    collections.sort(key=lambda item: item[0] not in _PREFERRED_CONTAINER_KEYS)
    for _key, value in collections:
        yield from _pairs_from_container(value, depth=depth + 1)


def _pairs_from_container(value: Any, *, depth: int) -> Iterator[tuple[str, Any]]:
    """Yield candidate pairs from a nested list or mapping."""

    if depth > _MAX_DEPTH:
        return
    if isinstance(value, Mapping):
        yield from _candidate_pairs(value, depth=depth)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _candidate_pairs(item, depth=depth)


def _labelled_pair(item: Mapping[str, Any]) -> tuple[str, Any] | None:
    """Read ``(prompt, answer)`` off an object that names both as separate keys.

    Returns ``None`` unless the object supplies both halves -- a label with no
    answer is a box the tech left blank, and there is nothing to record.
    """

    label_key = _first_answered(item, _LABEL_KEYS)
    if label_key is None:
        return None
    value_key = _first_answered(item, _VALUE_KEYS, skip=label_key)
    if value_key is None:
        return None
    return str(item[label_key]), item[value_key]


def _first_answered(item: Mapping[str, Any], keys: tuple[str, ...], *, skip: str | None = None) -> str | None:
    """The first of ``keys`` this object answers with non-blank text.

    Blank candidates are stepped over rather than settled on. An object can carry
    several of the supported keys with only one of them filled in -- Limble sends
    ``{"description": "Cause", "value": "", "answer": "Bearing wear"}`` when the
    box is answered but the unused ``value`` slot is still serialised -- and
    stopping at the empty ``value`` would drop a narrative the tech did record.
    Only one pair is read per object, so there is no second chance to correct for
    a blank candidate chosen here.
    """

    for key in keys:
        if key == skip:
            continue
        value = item.get(key)
        if _is_text(value) and _clean(value):
            return key
    return None


def _is_text(value: Any) -> bool:
    """True for a scalar that can serve as a label or an answer.

    Booleans are excluded: a checkbox instruction is not one of these text boxes,
    and rendering its True as the Cause would be worse than leaving it blank.
    """

    if isinstance(value, bool) or value is None:
        return False
    return isinstance(value, (str, int, float))


# A tag, as distinct from a less-than a tech typed. Requiring a letter, slash or
# "!" right after the "<" leaves arithmetic ("temp < 50 > 40") alone.
_TAG = re.compile(r"<[a-zA-Z/!][^>]*>")


def _clean(value: Any) -> str:
    """Reduce one answer to the text the tech actually typed.

    Limble's rich-text boxes return the editor's markup rather than plain text,
    so a Condition can arrive as ``<a class='cursor ...'></a><div><a ...><div>It
    was leaking which spilled into confined space floor.</div></a></div>`` and a
    Cause as ``Unknown,&nbsp; dropped it box``. Rendered verbatim in a table cell
    that is a wall of markup, so strip the tags and decode the entities here --
    once, on the way in -- rather than in each of the tables that show it.

    Every tag becomes a space rather than nothing: two adjacent ``<div>`` lines
    would otherwise run their last and first words together, and an over-split
    phrase is far easier to read than an invented compound word.
    """

    text = str(value)
    if "<" in text and ">" in text:
        text = _TAG.sub(" ", text)
    # After the tags, so a genuinely escaped "&lt;b&gt;" survives as text rather
    # than being decoded into a tag and then stripped.
    text = html.unescape(text)
    # Collapses the runs the substitution above leaves, and the non-breaking
    # spaces &nbsp; decodes into.
    return " ".join(text.split())
