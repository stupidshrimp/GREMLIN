"""The work-order disposition standard, and the renewal-stream life engine built on it.

One set of dispositioned fields has to serve two very different readers without
contradicting either: the pattern views, which group work orders by what failed
and how, and Weibull life analysis, which needs every life measured on one
physical item. The ISO 14224 trio the team adopted -- failure mode, mechanism and
cause -- says *how* something failed but not *what* or *where*: the PFS dry-off
oven's clogged air filter (4002-S09) codes as FTS / CONT / MNT-INT, exactly like a
contaminated flame rod on the prime bake oven. So the standard adds the location
(sub-asset) and the component, and the life engine keys every clock on them.

Everything here is plain data and functions, with no database: the rules can be
read, tested and argued about on their own. ``StandardDispositionService`` stores
what they decide and feeds them records.

The hard rule (spec section 5): life intervals are always built per renewal
stream -- one location's one component -- and only then pooled. A population
selection chooses which streams are pooled and which events count as failures;
it never chooses what an interval is measured between. Mixing streams first
measures the gap between two different items' events, which is no life at all.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from typing import Any, Callable, Iterable, Mapping, Sequence

# ---------------------------------------------------------------------------
# Disposition categories (spec section 2.1)
# ---------------------------------------------------------------------------
# Fixed rather than a lookup table: the life engine's rules are written against
# them, so renaming one is a change to the method, not to configuration.
FAILURE = "FAILURE"
PM_RESET = "PM_RESET"
EXTERNAL = "EXTERNAL"
NON_FAILURE = "NON_FAILURE"
DUPLICATE_MISFILED = "DUPLICATE_MISFILED"
HELD = "HELD"
NOT_REVIEWED = "NOT_REVIEWED"

DISPOSITION_CATEGORIES = (FAILURE, PM_RESET, EXTERNAL, NON_FAILURE, DUPLICATE_MISFILED, HELD, NOT_REVIEWED)

CATEGORY_GUIDE: dict[str, dict[str, Any]] = {
    FAILURE: {
        "label": "Failure",
        "use_when": "Function lost or degraded and corrective work done, including a condition found on "
        "inspection (mode DEG).",
        "pattern_views": True,
        "weibull_role": "A failure when it matches the population; otherwise a suspension that resets the clock.",
    },
    PM_RESET: {
        "label": "PM reset",
        "use_when": "Planned work renewed specific items.",
        "pattern_views": True,
        "weibull_role": "A suspension and a fresh clock for each renewed item.",
    },
    EXTERNAL: {
        "label": "External",
        "use_when": "A real stop caused outside the equipment: utility off, operator error, forklift strike, "
        "upstream system.",
        "pattern_views": True,
        "weibull_role": "Left out.",
    },
    NON_FAILURE: {
        "label": "Non-failure",
        "use_when": "Check or verify, no fault found, parts delivery, rebuild for stock, send-out repair, "
        "improvement or project, setpoint adjustment, admin.",
        "pattern_views": True,
        "weibull_role": "No event.",
    },
    DUPLICATE_MISFILED: {
        "label": "Duplicate / misfiled",
        "use_when": "A continuation or duplicate of another work order, or one logged against the wrong asset.",
        "pattern_views": False,
        "weibull_role": "Left out, but its completion is the linked work order's renewal date.",
    },
    HELD: {
        "label": "Held",
        "use_when": "Reviewed, and the record cannot support a decision.",
        "pattern_views": True,
        "weibull_role": "Left out until it is resolved.",
    },
    NOT_REVIEWED: {
        "label": "Not reviewed",
        "use_when": "Nobody has dispositioned it yet.",
        "pattern_views": False,
        "weibull_role": "Left out.",
    },
}

PATTERN_VIEW_CATEGORIES = tuple(code for code in DISPOSITION_CATEGORIES if CATEGORY_GUIDE[code]["pattern_views"])
# The categories that are a decision about the equipment, so the coded fields
# (component, mechanism, mode, action, evidence) are required. HELD is reviewed
# but undecided, and NOT_REVIEWED is not reviewed at all; both may carry partial
# codes. DUPLICATE_MISFILED carries none.
CODED_CATEGORIES = (FAILURE, PM_RESET, EXTERNAL, NON_FAILURE)
# The only categories that renew anything, so the only ones renewed_items means
# something on.
RENEWING_CATEGORIES = (FAILURE, PM_RESET)

EVIDENCE_VALUES = ("EXPLICIT", "INFERRED")
EVIDENCE_LABELS = {
    "EXPLICIT": "Explicit: the work order states the item and its condition",
    "INFERRED": "Inferred from the work order",
}

# ISO 14224 detection methods, derived rather than entered (spec section 4).
DETECTION_INSPECTION = "INSPECTION"
DETECTION_PERIODIC_MAINTENANCE = "PERIODIC_MAINTENANCE"
DETECTION_PRODUCTION_INTERFERENCE = "PRODUCTION_INTERFERENCE"
DETECTION_LABELS = {
    DETECTION_INSPECTION: "Inspection",
    DETECTION_PERIODIC_MAINTENANCE: "Periodic maintenance",
    DETECTION_PRODUCTION_INTERFERENCE: "Production interference",
}
PM_RECORD_CLASSES = ("PM", "PM_RESET_CANDIDATE")

# ---------------------------------------------------------------------------
# Code lists (spec section 3) -- configuration, stored in disposition_code
# ---------------------------------------------------------------------------
# A component is the maintainable item (ISO 14224's level 8: a compressor, a
# blower); a part is what it is made of (level 9: a filter, an O-ring), from one
# list shared by every component, so "every filter on S09" can be asked whatever
# each filter is in.
COMPONENT = "COMPONENT"
PART = "PART"
FAILURE_MECHANISM = "FAILURE_MECHANISM"
FAILURE_MODE = "FAILURE_MODE"
FAILURE_CAUSE = "FAILURE_CAUSE"
ACTION = "ACTION"
CODE_LISTS = (COMPONENT, PART, FAILURE_MECHANISM, FAILURE_MODE, FAILURE_CAUSE, ACTION)
# The lists whose codes are just a name and a description: no ISO reference,
# meaning or order (they list by name), and a new one's code is made from its name.
NAMED_CODE_LISTS = (COMPONENT, PART)
CODE_LIST_LABELS = {
    COMPONENT: "Component",
    PART: "Part",
    FAILURE_MECHANISM: "Failure mechanism",
    FAILURE_MODE: "Failure mode",
    FAILURE_CAUSE: "Failure cause",
    ACTION: "Action",
}

# What a code means to the engine, beyond its name. The team owns every code
# string and name; the engine only ever asks these questions of them, so a list
# can be renamed or extended without touching the method.
SEMANTIC_UNKNOWN = "UNKNOWN"
# Once a component's; no list takes it now, but a component saved with it still reads.
SEMANTIC_REVIEW = "REVIEW"
SEMANTIC_INSPECTION_FINDING = "INSPECTION_FINDING"
SEMANTIC_NO_FAULT_FOUND = "NO_FAULT_FOUND"
SEMANTIC_INDUCED = "INDUCED"
CODE_LIST_SEMANTICS = {
    COMPONENT: (),
    PART: (),
    FAILURE_MECHANISM: (SEMANTIC_UNKNOWN,),
    FAILURE_MODE: (SEMANTIC_INSPECTION_FINDING, SEMANTIC_NO_FAULT_FOUND),
    FAILURE_CAUSE: (SEMANTIC_UNKNOWN, SEMANTIC_INDUCED),
    ACTION: (),
}
SEMANTIC_LABELS = {
    SEMANTIC_UNKNOWN: "Means 'not known'",
    SEMANTIC_INSPECTION_FINDING: "Found on inspection, still running (detection: inspection)",
    SEMANTIC_NO_FAULT_FOUND: "No fault found (never a FAILURE)",
    SEMANTIC_INDUCED: "Induced failure (maintenance or installation)",
}
# The same, as a word or two for a chip in a list of codes.
SEMANTIC_SHORT_LABELS = {
    SEMANTIC_UNKNOWN: "Unknown",
    SEMANTIC_INSPECTION_FINDING: "Inspection finding",
    SEMANTIC_NO_FAULT_FOUND: "No fault found",
    SEMANTIC_INDUCED: "Induced",
}

# A code is a short stable key: it is what dispositions store and what a saved
# population definition names, so it never changes once used. Names can.
CODE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,31}$")


def code_from_name(name: str, taken: Iterable[str] = ()) -> str:
    """The code a component or part named ``name`` is kept under: its words run
    together ("Air compressor" -> AirCompressor), cut to fit, and numbered on
    when ``taken`` (in any capitals) already has it."""

    words = re.findall(r"[A-Za-z0-9]+", name or "")
    base = "".join(word[:1].upper() + word[1:] for word in words)[:32] or "Item"
    used = {code.casefold() for code in taken}
    code, number = base, 1
    while code.casefold() in used:
        number += 1
        suffix = str(number)
        code = base[: 32 - len(suffix)] + suffix
    return code


@dataclass(frozen=True)
class CodeEntry:
    """One code in one list, as the team has configured it."""

    code_list: str
    code: str
    name: str
    description: str | None = None
    iso_ref: str | None = None
    default_cause_code: str | None = None
    renews_item: bool = False
    semantic: str | None = None
    sort_order: int = 0
    is_active: bool = True


def _seed(code_list: str, rows: Sequence[tuple]) -> tuple[CodeEntry, ...]:
    entries = []
    for index, row in enumerate(rows, start=1):
        code, name, *rest = row
        options = rest[0] if rest else {}
        entries.append(CodeEntry(code_list=code_list, code=code, name=name, sort_order=index * 10, **options))
    return tuple(entries)


# Starting content, written into disposition_code the first time a list is empty.
# From then on the table is the list: the team edits it there.
SEED_CODES: dict[str, tuple[CodeEntry, ...]] = {
    COMPONENT: _seed(COMPONENT, (
        ("Filter", "Filter / strainer / sieve"),
        ("Belt", "Belt"),
        ("Bearing", "Bearing"),
        ("Motor", "Motor"),
        ("Fan", "Fan / blower"),
        ("Shaft", "Shaft / pulley / coupling"),
        ("Burner", "Burner / flame rod / igniter"),
        ("GasTrain", "Gas train / fuel valve"),
        ("Valve", "Valve"),
        ("Actuator", "Actuator"),
        ("Pump", "Pump"),
        ("Seal", "Seal / gasket / diaphragm / check valve"),
        ("Pipe", "Pipe / tubing / fitting"),
        ("Hose", "Hose / pneumatic fitting"),
        ("Tank", "Tank / hopper / heat exchanger"),
        ("Nozzle", "Nozzle / riser"),
        ("Gun", "Powder gun / applicator"),
        ("Chain", "Chain / sprocket / drive"),
        ("Conveyor", "Conveyor / carrier"),
        ("Sensor", "Sensor / switch / gauge"),
        ("Controller", "Controller / PLC / HMI"),
        ("Starter", "Starter / contactor / relay / VFD"),
        ("Electrical", "Electrical: fuse / breaker / wiring / disconnect"),
        ("Door", "Door / guard / cover / latch / hardware"),
        ("HVAC", "HVAC / compressor / chiller"),
        ("Other", "Other"),
        ("Unknown", "Unknown"),
    )),
    # Parts start empty: the team adds them as work orders name them.
    PART: (),
    FAILURE_MECHANISM: _seed(FAILURE_MECHANISM, (
        ("WEAR", "Wear", {"iso_ref": "2.4", "default_cause_code": "EXP-WEAR"}),
        ("PLUG", "Plugged / blocked", {"iso_ref": "5.1", "default_cause_code": "EXP-WEAR"}),
        ("CONT", "Contaminated / fouled", {"iso_ref": "5.2", "default_cause_code": "EXP-WEAR"}),
        ("LEAK", "Leak", {"iso_ref": "1.1", "default_cause_code": "UNK"}),
        ("DEFM", "Damaged / deformed", {"iso_ref": "1.4", "default_cause_code": "UNK"}),
        ("LOOS", "Loose / misaligned", {"iso_ref": "1.3 / 1.5", "default_cause_code": "MNT-ERR"}),
        ("BRKN", "Broken / fractured", {"iso_ref": "2.5", "default_cause_code": "UNK"}),
        ("OVHT", "Overheated", {"iso_ref": "2.7", "default_cause_code": "UNK"}),
        ("ELEC", "Electrical fault", {"iso_ref": "4.x", "default_cause_code": "UNK"}),
        ("SIGN", "Signal / control fault", {"iso_ref": "3.x", "default_cause_code": "UNK"}),
        ("UNK", "Unknown", {"iso_ref": "6.4", "default_cause_code": "UNK", "semantic": SEMANTIC_UNKNOWN}),
    )),
    FAILURE_MODE: _seed(FAILURE_MODE, (
        ("FTS", "Fails to start"),
        ("UST", "Trips / stops while running"),
        ("LOO", "Low output / can't reach setpoint"),
        ("ERO", "Erratic output / abnormal reading"),
        ("LKG", "Leak"),
        ("VIB", "Noise / vibration"),
        ("STD", "Physical damage"),
        ("DEG", "Degraded, still running (found on inspection)", {"semantic": SEMANTIC_INSPECTION_FINDING}),
        ("NFF", "No fault found", {"semantic": SEMANTIC_NO_FAULT_FOUND}),
    )),
    FAILURE_CAUSE: _seed(FAILURE_CAUSE, (
        ("DESIGN", "Design / capacity"),
        ("INSTALL", "Installation", {"semantic": SEMANTIC_INDUCED}),
        ("OPERATE", "Operating error"),
        ("MNT-ERR", "Maintenance error (incl. wrong part)", {"semantic": SEMANTIC_INDUCED}),
        ("MNT-INT", "Maintenance interval exceeded"),
        ("EXP-WEAR", "Expected wear and tear"),
        ("MGMT", "Management / procedure"),
        ("UNK", "Unknown", {"semantic": SEMANTIC_UNKNOWN}),
    )),
    ACTION: _seed(ACTION, (
        ("REPLACE", "Replace", {"renews_item": True}),
        ("REPAIR", "Repair", {"renews_item": True}),
        ("CLEAN_SERVICE", "Clean / service", {"renews_item": True}),
        ("ADJUST", "Adjust"),
        ("RESET_ONLY", "Reset only"),
        ("INSPECT_ONLY", "Inspect only"),
    )),
}


class CodeBook:
    """The code lists as configured, for validating and deriving dispositions."""

    def __init__(self, entries: Iterable[CodeEntry]) -> None:
        self._by_list: dict[str, dict[str, CodeEntry]] = {code_list: {} for code_list in CODE_LISTS}
        for entry in entries:
            self._by_list.setdefault(entry.code_list, {})[entry.code] = entry

    @classmethod
    def from_seed(cls) -> "CodeBook":
        return cls(entry for entries in SEED_CODES.values() for entry in entries)

    def entry(self, code_list: str, code: str | None) -> CodeEntry | None:
        if code is None:
            return None
        return self._by_list.get(code_list, {}).get(code)

    def entries(self, code_list: str, *, include_inactive: bool = False) -> list[CodeEntry]:
        rows = [entry for entry in self._by_list.get(code_list, {}).values() if include_inactive or entry.is_active]
        if code_list in NAMED_CODE_LISTS:
            # Components and parts have no order of their own: they list by name.
            return sorted(rows, key=lambda entry: (entry.name.lower(), entry.code))
        return sorted(rows, key=lambda entry: (entry.sort_order, entry.name.lower(), entry.code))

    def name(self, code_list: str, code: str | None) -> str | None:
        entry = self.entry(code_list, code)
        return entry.name if entry is not None else code

    def has_semantic(self, code_list: str, code: str | None, semantic: str) -> bool:
        entry = self.entry(code_list, code)
        return entry is not None and entry.semantic == semantic

    def default_cause(self, mechanism: str | None) -> str | None:
        entry = self.entry(FAILURE_MECHANISM, mechanism)
        return entry.default_cause_code if entry is not None else None

    def renews(self, action: str | None) -> bool:
        entry = self.entry(ACTION, action)
        return bool(entry and entry.renews_item)

    def unknown_code(self, code_list: str) -> str | None:
        """The active code in a list that means 'not known', if the list has one."""

        for entry in self.entries(code_list):
            if entry.semantic == SEMANTIC_UNKNOWN:
                return entry.code
        return None


# ---------------------------------------------------------------------------
# Validation (spec section 2) and derivation (spec section 4)
# ---------------------------------------------------------------------------
FIELD_LABELS = {
    "disposition_category": "Disposition category",
    "sub_asset": "Sub-asset",
    "component_code": "Component",
    "part_code": "Part",
    "failure_mechanism_code": "Failure mechanism",
    "failure_mode_code": "Failure mode",
    "action_code": "Action",
    "evidence": "Evidence",
    "failure_cause_code": "Failure cause",
    "renewed_items": "Renewed items",
    "qty": "Qty",
    "linked_mapped_record_id": "Linked WO",
    "disposition_note": "Disposition note",
}
CODED_FIELDS = (
    ("component_code", COMPONENT),
    ("part_code", PART),
    ("failure_mechanism_code", FAILURE_MECHANISM),
    ("failure_mode_code", FAILURE_MODE),
    ("action_code", ACTION),
)
NOTE_MAX = 2000


@dataclass(frozen=True)
class RecordContext:
    """What validation needs to know about the work order being dispositioned."""

    mapped_record_id: int
    asset_number: str
    # The asset numbers of the WO asset's children, from the asset hierarchy.
    sub_asset_choices: tuple[str, ...] = ()
    record_class: str | None = None


def clean_text(value: Any) -> str | None:
    if value is None:
        return None
    text = re.sub(r"\s+", " ", str(value)).strip()
    return text or None


def _code_values(value: Any) -> list[str]:
    """A multi-select's codes, whether sent as a list or a comma-separated string."""

    if value is None:
        return []
    if isinstance(value, str):
        parts = value.split(",")
    elif isinstance(value, (list, tuple, set, frozenset)):
        parts = list(value)
    else:
        raise ValueError("Renewed items must be a list of component codes.")
    seen: list[str] = []
    for part in parts:
        text = clean_text(part)
        if text and text not in seen:
            seen.append(text)
    return seen


def normalize_disposition(
    values: Mapping[str, Any],
    *,
    record: RecordContext,
    codebook: CodeBook,
    previous: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Check one disposition against the standard and return it as it is stored.

    Raises ValueError naming every problem at once, so the editor can show them
    together rather than one per save. ``previous`` is the record's current
    disposition: a code the team has since retired is still accepted when it is
    unchanged, so re-saving an old record is not refused over a field nobody touched.
    """

    problems: list[str] = []
    previous = previous or {}
    category = clean_text(values.get("disposition_category")) or NOT_REVIEWED
    category = category.upper()
    if category not in DISPOSITION_CATEGORIES:
        raise ValueError(f"Unknown disposition category {category!r}. Choose one of {', '.join(DISPOSITION_CATEGORIES)}.")

    result: dict[str, Any] = {
        "disposition_category": category,
        "sub_asset": None,
        "component_code": None,
        "part_code": None,
        "failure_mechanism_code": None,
        "failure_mode_code": None,
        "action_code": None,
        "evidence": None,
        "failure_cause_code": None,
        "renewed_items": (),
        "qty": None,
        "linked_mapped_record_id": None,
        "disposition_note": None,
    }

    note = clean_text(values.get("disposition_note"))
    if note is not None and len(note) > NOTE_MAX:
        problems.append(f"The disposition note is longer than {NOTE_MAX} characters; keep it to one line.")
    result["disposition_note"] = note

    def checked_code(field_name: str, code_list: str) -> str | None:
        code = clean_text(values.get(field_name))
        if code is None:
            return None
        entry = codebook.entry(code_list, code)
        if entry is None:
            problems.append(f"{FIELD_LABELS[field_name]} {code!r} is not in the {CODE_LIST_LABELS[code_list].lower()} list.")
            return None
        if not entry.is_active and previous.get(field_name) != code:
            problems.append(f"{FIELD_LABELS[field_name]} {code!r} has been retired from the list; choose another.")
            return None
        return code

    linked = values.get("linked_mapped_record_id")
    if category == DUPLICATE_MISFILED:
        # Nothing else describes a duplicate: it is an echo of another record, or a
        # record that belongs on another asset, and the analysis reads neither.
        if linked not in (None, ""):
            try:
                linked = int(linked)
            except (TypeError, ValueError):
                problems.append("Linked WO must name a work order.")
                linked = None
            if linked is not None and linked == record.mapped_record_id:
                problems.append("A work order cannot be a duplicate of itself; link the work order it continues.")
                linked = None
            result["linked_mapped_record_id"] = linked
        if problems:
            raise ValueError(" ".join(problems))
        return result
    if linked not in (None, ""):
        problems.append("Linked WO is only for DUPLICATE_MISFILED; clear it or change the category.")

    sub_asset = clean_text(values.get("sub_asset"))
    if sub_asset is not None and sub_asset != record.asset_number:
        if sub_asset not in record.sub_asset_choices:
            if record.sub_asset_choices:
                problems.append(
                    f"Sub-asset {sub_asset!r} is not one of asset {record.asset_number}'s sub-assets."
                )
            else:
                problems.append(f"Asset {record.asset_number} has no sub-assets on record; leave the sub-asset blank.")
        else:
            result["sub_asset"] = sub_asset

    for field_name, code_list in CODED_FIELDS:
        result[field_name] = checked_code(field_name, code_list)
    if result["part_code"] is not None and result["component_code"] is None and clean_text(values.get("component_code")) is None:
        problems.append("A part needs its component: choose the component (the maintainable item) the part is in.")
    evidence = clean_text(values.get("evidence"))
    if evidence is not None:
        evidence = evidence.upper()
        if evidence not in EVIDENCE_VALUES:
            problems.append("Evidence must be EXPLICIT or INFERRED.")
            evidence = None
    result["evidence"] = evidence

    if category in CODED_CATEGORIES:
        missing = [
            FIELD_LABELS[name]
            for name in ("component_code", "failure_mechanism_code", "failure_mode_code", "action_code", "evidence")
            if result[name] is None and clean_text(values.get(name)) is None
        ]
        if missing:
            problems.append(f"{category} needs {', '.join(missing)}.")

    if category == FAILURE and codebook.has_semantic(FAILURE_MODE, result["failure_mode_code"], SEMANTIC_NO_FAULT_FOUND):
        problems.append("No fault found is not a failure: disposition it as NON_FAILURE.")

    cause = checked_code("failure_cause_code", FAILURE_CAUSE)
    if cause is not None and cause == codebook.default_cause(result["failure_mechanism_code"]):
        # The default already gives this cause; storing it would read as an override.
        cause = None
    result["failure_cause_code"] = cause
    if cause is not None and note is None:
        problems.append("Overriding the failure cause needs a disposition note saying why.")
    if category == HELD and note is None:
        problems.append("HELD needs a disposition note saying what the record is missing.")

    renewed = _code_values(values.get("renewed_items"))
    for code in renewed:
        entry = codebook.entry(COMPONENT, code)
        if entry is None:
            problems.append(f"Renewed item {code!r} is not in the component list.")
        elif not entry.is_active and code not in tuple(previous.get("renewed_items") or ()):
            problems.append(f"Renewed item {code!r} has been retired from the component list; choose another.")
    if renewed and category not in RENEWING_CATEGORIES:
        problems.append("Renewed items are only for FAILURE and PM_RESET, the categories that renew anything.")
    if category == FAILURE and result["component_code"] in renewed:
        problems.append(
            "Renewed items repeats the component: list only the other items the work renewed. Whether the "
            "component itself was renewed follows from the action."
        )
    if category == PM_RESET and not renewed and result["part_code"] is None:
        problems.append(
            "PM_RESET needs the renewed items, or the part it renewed: each one gets a fresh life clock."
        )
    result["renewed_items"] = tuple(renewed)

    qty = values.get("qty")
    if qty not in (None, ""):
        try:
            qty_value = int(str(qty).strip())
            if str(qty_value) != str(qty).strip().lstrip("+"):
                raise ValueError
        except ValueError:
            problems.append("Qty must be a whole number.")
        else:
            if qty_value < 1:
                problems.append("Qty must be at least 1.")
            else:
                result["qty"] = qty_value

    if problems:
        raise ValueError(" ".join(problems))
    return result


def derive_fields(
    disposition: Mapping[str, Any],
    *,
    asset_number: str,
    record_class: str | None,
    codebook: CodeBook,
) -> dict[str, Any]:
    """The fields nobody enters (spec section 4), from a stored disposition."""

    category = disposition.get("disposition_category") or NOT_REVIEWED
    location = disposition.get("sub_asset") or asset_number
    mechanism = disposition.get("failure_mechanism_code")
    cause = disposition.get("failure_cause_code") or codebook.default_cause(mechanism)
    component = disposition.get("component_code")
    part = disposition.get("part_code")
    detection = None
    if category not in (DUPLICATE_MISFILED, NOT_REVIEWED):
        if codebook.has_semantic(FAILURE_MODE, disposition.get("failure_mode_code"), SEMANTIC_INSPECTION_FINDING):
            detection = DETECTION_INSPECTION
        elif (record_class or "").upper() in PM_RECORD_CLASSES:
            detection = DETECTION_PERIODIC_MAINTENANCE
        else:
            detection = DETECTION_PRODUCTION_INTERFERENCE
    return {
        "location": location,
        "failure_cause_effective": cause,
        "failure_cause_is_override": bool(disposition.get("failure_cause_code")),
        "detection_method": detection,
        "renews_item": codebook.renews(disposition.get("action_code")),
        "renewal_stream_key": (f"{location} | {component}" + (f" | {part}" if part else "")) if component else None,
    }


# ---------------------------------------------------------------------------
# Migration from the REL categories (spec section 2.2)
# ---------------------------------------------------------------------------
LEGACY_CATEGORY_MAP = {
    "INCLUDED_FAILURE": FAILURE,
    "INCLUDED_PM_RESET_EVENT": PM_RESET,
    "INCLUDED_CENSORED_ASSET_EVENT": PM_RESET,
    "EXCLUDED_NON_FAILURE": NON_FAILURE,
    "EXCLUDED_MIXED_CONTAMINATING": DUPLICATE_MISFILED,
    "HELD_AMBIGUOUS": HELD,
    "UNKNOWN": NOT_REVIEWED,
    # Not in the spec's table: two PM decisions that renew nothing. A PM kept for
    # context only, or one rejected as a reset, is planned work with no life
    # event, which is NON_FAILURE's "no event".
    "PM_CONTEXT_ONLY": NON_FAILURE,
    "REJECTED_PM_RESET": NON_FAILURE,
}

LEGACY_REVIEW_NOTES = {
    "INCLUDED_FAILURE": "Migrated from INCLUDED_FAILURE: confirm it is a failure and not no-fault-found or external.",
    "INCLUDED_PM_RESET_EVENT": "Migrated from INCLUDED_PM_RESET_EVENT: list the items the PM renewed.",
    "INCLUDED_CENSORED_ASSET_EVENT": "Migrated from INCLUDED_CENSORED_ASSET_EVENT: list the items the work renewed.",
    "EXCLUDED_NON_FAILURE": "Migrated from EXCLUDED_NON_FAILURE.",
    "EXCLUDED_MIXED_CONTAMINATING": "Migrated from EXCLUDED_MIXED_CONTAMINATING: link the work order it duplicates, if any.",
    "HELD_AMBIGUOUS": "Migrated from HELD_AMBIGUOUS.",
    "PM_CONTEXT_ONLY": "Migrated from PM_CONTEXT_ONLY: a PM kept for context, with no reset.",
    "REJECTED_PM_RESET": "Migrated from REJECTED_PM_RESET: a PM that was not accepted as a reset.",
}

# Legacy mechanism names that say, without any doubt, where and what failed and
# how. Only those are converted; every other legacy name is kept as text for an
# engineer to re-disposition (spec section 2.2). Each pattern names a sub-asset
# token, which must resolve to exactly one sub-asset of the work order's asset.
LEGACY_AUTO_CONVERSIONS = (
    (
        re.compile(r"^\s*(?P<sub>[A-Za-z]{1,3}\d{1,3})\s+(?:clogged|plugged|blocked|dirty)\s+(?:air\s+)?filters?\s*$", re.IGNORECASE),
        {"component_code": "Filter", "failure_mechanism_code": "CONT"},
    ),
)


def _sub_asset_token_matches(asset_number: str, token: str, name: str | None = None) -> bool:
    """Whether a sub-asset is the one a token such as "S09" names.

    By its number ("4002-S09") or, since on Limble an asset's number is often
    just its id, by a word of its name ("4002-S09 Dry-off oven").
    """

    token = token.strip().upper()
    for word in [asset_number, *(name or "").split()]:
        text = word.strip().upper()
        if text == token or any(text.endswith(separator + token) for separator in ("-", "_", ".")):
            return True
    return False


def legacy_auto_conversion(
    legacy_mechanism: str | None,
    *,
    asset_number: str,
    sub_asset_choices: Sequence[str],
    codebook: CodeBook,
    asset_names: Mapping[str, str] | None = None,
) -> dict[str, Any] | None:
    """Coded fields read off a legacy mechanism name, when it is unambiguous.

    None when no rule matches, when its sub-asset token does not resolve to
    exactly one place, or when the codes it would set are not in the lists.
    ``asset_names`` lets the token match a sub-asset by its name as well as its
    number.
    """

    names = asset_names or {}

    text = clean_text(legacy_mechanism)
    if text is None:
        return None
    for pattern, codes in LEGACY_AUTO_CONVERSIONS:
        match = pattern.match(text)
        if not match:
            continue
        if not all(
            (entry := codebook.entry(code_list, codes[name])) is not None and entry.is_active
            for name, code_list in CODED_FIELDS
            if name in codes
        ):
            return None
        token = match.group("sub")
        if _sub_asset_token_matches(asset_number, token, names.get(asset_number)):
            sub_asset = None
        else:
            matches = [choice for choice in sub_asset_choices if _sub_asset_token_matches(choice, token, names.get(choice))]
            if len(matches) != 1:
                return None
            sub_asset = matches[0]
        return {"sub_asset": sub_asset, **codes}
    return None


# ---------------------------------------------------------------------------
# Analysis population (spec section 7.1)
# ---------------------------------------------------------------------------
# The selector's dimensions, as a selection names them, with the record field each
# filters and the POP-string key it is written under (spec section 5.4).
POPULATION_DIMENSIONS = (
    ("assets", "asset", "asset"),
    ("locations", "location", "location"),
    ("components", "component_code", "component"),
    ("parts", "part_code", "part"),
    ("failure_modes", "failure_mode_code", "failure_mode"),
    ("failure_mechanisms", "failure_mechanism_code", "failure_mech"),
    ("failure_causes", "failure_cause_effective", "failure_cause"),
    ("actions", "action_code", "action"),
)
# The dimensions that narrow the pattern views but not the Weibull (see Population).
PATTERN_ONLY_DIMENSIONS = ("failure_modes", "failure_causes", "actions")
# The Perform an Analysis checkbox that sets ``exclude_induced``, word for word as
# the page labels it, so what names it here names what is on screen.
INDUCED_SWITCH_LABEL = "Count induced failures (installation and maintenance errors) as suspensions"


@dataclass(frozen=True)
class Population:
    """Which work orders an analysis reads.

    ``assets`` picks the records (an asset and its sub-assets); ``locations``,
    ``components`` and ``parts`` pick the renewal streams that are pooled (with no
    part picked, each component as a whole; with parts, those parts, in the
    components picked or in any); ``failure_mechanisms``
    picks which failures in those streams count as failures. Empty means no
    restriction. ``failure_modes``, ``failure_causes``, ``actions`` and
    ``categories`` narrow the pattern views only (``PATTERN_ONLY_DIMENSIONS``): a
    mode is a symptom one mechanism shows several ways, a cause beyond "induced"
    restates the mechanism, and an action is what was done after the failure, so
    none of them says which lives end in the failure being studied.
    """

    assets: frozenset[str] = frozenset()
    locations: frozenset[str] = frozenset()
    components: frozenset[str] = frozenset()
    parts: frozenset[str] = frozenset()
    failure_modes: frozenset[str] = frozenset()
    failure_mechanisms: frozenset[str] = frozenset()
    failure_causes: frozenset[str] = frozenset()
    actions: frozenset[str] = frozenset()
    categories: frozenset[str] = frozenset()
    exclude_induced: bool = False

    @classmethod
    def from_mapping(cls, values: Mapping[str, Any]) -> "Population":
        def values_of(key: str) -> frozenset[str]:
            raw = values.get(key)
            if raw in (None, ""):
                return frozenset()
            if isinstance(raw, str):
                raw = raw.split(",")
            if not isinstance(raw, (list, tuple, set, frozenset)):
                raise ValueError(f"The population's {key} must be a list.")
            return frozenset(text for item in raw if (text := clean_text(item)))

        categories = frozenset(code.upper() for code in values_of("categories"))
        unknown = sorted(categories - set(DISPOSITION_CATEGORIES))
        if unknown:
            raise ValueError(f"Unknown disposition categories: {', '.join(unknown)}.")
        exclude = values.get("exclude_induced")
        if isinstance(exclude, str):
            exclude = exclude.strip().lower() in {"1", "true", "yes", "on"}
        return cls(
            assets=values_of("assets"),
            locations=values_of("locations"),
            components=values_of("components"),
            parts=values_of("parts"),
            failure_modes=values_of("failure_modes"),
            failure_mechanisms=values_of("failure_mechanisms"),
            failure_causes=values_of("failure_causes"),
            actions=values_of("actions"),
            categories=categories,
            exclude_induced=bool(exclude),
        )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "assets": sorted(self.assets),
            "locations": sorted(self.locations),
            "components": sorted(self.components),
            "parts": sorted(self.parts),
            "failure_modes": sorted(self.failure_modes),
            "failure_mechanisms": sorted(self.failure_mechanisms),
            "failure_causes": sorted(self.failure_causes),
            "actions": sorted(self.actions),
            "categories": sorted(self.categories),
            "exclude_induced": self.exclude_induced,
        }

    def stream_selected(self, location: str, component: str, part: str = "") -> bool:
        """Whether a stream is pooled: a component's own stream when no part is
        picked, else the picked parts' streams."""

        if (self.locations and location not in self.locations) or (self.components and component not in self.components):
            return False
        return part in self.parts if self.parts else not part

    def for_life_analysis(self) -> "Population":
        """This population as the Weibull reads it: without the pattern-only narrowing."""

        return replace(self, **{attribute: frozenset() for attribute in PATTERN_ONLY_DIMENSIONS}, categories=frozenset())

    def narrows_patterns_only(self) -> bool:
        """Whether a mode, cause or action is ticked, which the Weibull does not read."""

        return any(getattr(self, attribute) for attribute in PATTERN_ONLY_DIMENSIONS)

    def induced_as_suspension(self, event: "StreamEvent") -> bool:
        """Whether this is an induced failure the checkbox counts as a suspension."""

        return self.exclude_induced and event.cause_induced

    def counts_as_failure(self, event: "StreamEvent", unknown_mechanism_as: str) -> bool:
        """Whether a FAILURE in a pooled stream is a failure of this population."""

        if self.induced_as_suspension(event):
            return False
        if not self.failure_mechanisms or event.mechanism in self.failure_mechanisms:
            return True
        return event.mechanism_unknown and unknown_mechanism_as == FAILURE_END

    def unknown_mechanism_decides(self, event: "StreamEvent") -> bool:
        """Whether this failure counts only if an unknown mechanism is taken to be this one."""

        return (
            bool(self.failure_mechanisms)
            and event.mechanism not in self.failure_mechanisms
            and event.mechanism_unknown
            and not self.induced_as_suspension(event)
        )


def population_definition(population: Population, window_start: date | None, window_end: date) -> str:
    """The population as one line a saved analysis keeps (spec section 5.4).

    ``POP: location=4002-S09; component=Filter; failure_mech=CONT;
    suspensions=other renewals; window=2022-04-21..2026-10-15``. Values are codes,
    sorted, so the same population always reads the same.
    """

    parts = []
    for attribute, _, key in POPULATION_DIMENSIONS:
        if attribute in PATTERN_ONLY_DIMENSIONS:
            continue
        selected = getattr(population, attribute)
        if selected:
            parts.append(f"{key}={','.join(sorted(selected))}")
    if population.exclude_induced:
        parts.append("induced=suspensions")
    parts.append("suspensions=other renewals")
    start = window_start.isoformat() if window_start is not None else "first-record"
    parts.append(f"window={start}..{window_end.isoformat()}")
    return "POP: " + "; ".join(parts)


# ---------------------------------------------------------------------------
# Renewal-stream life engine (spec section 5)
# ---------------------------------------------------------------------------
FAILURE_END = "FAILURE"
SUSPENSION_END = "SUSPENSION"
UNKNOWN_AS_SUSPENSION = SUSPENSION_END
UNKNOWN_AS_FAILURE = FAILURE_END

# What one event did in one stream, for the event table that traces a fit back to
# every record behind it.
ROLE_STARTS_LIFE = "STARTS_LIFE"
ROLE_FAILURE = "FAILURE"
ROLE_SUSPENSION = "SUSPENSION"
ROLE_NO_EFFECT = "NO_EFFECT"
ROLE_EXCLUDED = "EXCLUDED"

# The REL minimum for fitting is MIN_WEIBULL_FAILURE_LIVES (5); the spec warns
# below about 5-6, so a fit on exactly five is made and still flagged.
SMALL_N_FAILURES = 6

# (location, component, part): the part is "" for the component as a whole.
StreamKey = tuple[str, str, str]


@dataclass(frozen=True)
class StreamEvent:
    """One dispositioned work order, as the life engine reads it."""

    record_id: int
    category: str
    location: str
    component: str | None
    mechanism: str | None = None
    part: str | None = None
    mode: str | None = None
    cause: str | None = None
    action: str | None = None
    renews_item: bool = False
    renewed_items: tuple[str, ...] = ()
    # When the failure was found (the WO's created date) and when the item was
    # renewed (its completion, or its linked repair's).
    event_date: datetime | None = None
    renewal_date: datetime | None = None
    qty: int | None = None
    mechanism_unknown: bool = False
    cause_induced: bool = False
    task_id: str | None = None


@dataclass(frozen=True)
class LifeInterval:
    location: str
    component: str
    start: datetime
    end: datetime
    end_type: str
    note: str
    start_record_id: int | None
    end_record_id: int | None
    life_hours: float
    raw_hours: float
    part: str = ""

    @property
    def stream(self) -> StreamKey:
        return (self.location, self.component, self.part)

    @property
    def raw_days(self) -> float:
        return self.raw_hours / 24.0


@dataclass(frozen=True)
class StreamEventNote:
    location: str
    component: str
    record_id: int
    task_id: str | None
    when: datetime | None
    role: str
    note: str
    part: str = ""


@dataclass
class RenewalAnalysis:
    intervals: list[LifeInterval] = field(default_factory=list)
    notes: list[StreamEventNote] = field(default_factory=list)
    streams: list[StreamKey] = field(default_factory=list)
    recurrent_streams: list[StreamKey] = field(default_factory=list)
    # The events whose life ended in a failure, for the warnings that read them.
    failure_events: list[StreamEvent] = field(default_factory=list)
    # Failures in the window that count only if their unknown mechanism is taken
    # to be the one selected: when there are any, both fits are made.
    unknown_decided: list[StreamEvent] = field(default_factory=list)
    # Every event that touched a pooled stream inside the window.
    stream_events: list[StreamEvent] = field(default_factory=list)

    @property
    def failure_count(self) -> int:
        return sum(1 for interval in self.intervals if interval.end_type == FAILURE_END)

    @property
    def suspension_count(self) -> int:
        return sum(1 for interval in self.intervals if interval.end_type == SUSPENSION_END)

    def fit_data(self) -> list[tuple[float, int]]:
        return [
            (interval.life_hours, int(interval.end_type == FAILURE_END))
            for interval in self.intervals
            if interval.life_hours > 0
        ]


@dataclass(frozen=True)
class _Touch:
    """One event's effect on one stream: the item's exposure ends, and it may be renewed."""

    event: StreamEvent
    primary: bool
    exposure_end: datetime | None
    renewal: datetime | None
    # Renewed because the whole component it is part of was.
    with_component: bool = False


def stream_touches(event: StreamEvent) -> list[tuple[StreamKey, _Touch]]:
    """The streams an event touches, and how.

    A failure touches its item's stream -- its part's when it names one, else its
    component's -- ending the exposure when it was found and renewing when the
    repair finished if the action renews; and every other component it renewed
    (exposure ending with the stop, renewed with the repair). A PM reset touches
    each component it renewed, and the part it names: the item ran until the PM
    was done, which is both when its exposure ends and when its new life starts.

    A renewed component's parts are renewed with it; ``build_renewal_intervals``
    adds those touches, since one event can't know which parts a component has.
    """

    touches: list[tuple[StreamKey, _Touch]] = []
    part = event.part or ""
    if event.category == FAILURE:
        if event.component:
            touches.append((
                (event.location, event.component, part),
                _Touch(event, True, event.event_date, event.renewal_date if event.renews_item else None),
            ))
        for item in event.renewed_items:
            if item != event.component:
                touches.append(((event.location, item, ""), _Touch(event, False, event.event_date, event.renewal_date)))
    elif event.category == PM_RESET:
        for item in event.renewed_items:
            touches.append(((event.location, item, ""), _Touch(event, False, event.renewal_date, event.renewal_date)))
        if event.component and part:
            touches.append((
                (event.location, event.component, part), _Touch(event, False, event.renewal_date, event.renewal_date)
            ))
    return touches


def _with_parts(touches: list[tuple[StreamKey, _Touch]]) -> list[tuple[StreamKey, _Touch]]:
    """``touches`` and, for each one renewing a whole component, the same renewal of
    each of that component's parts at that location: a compressor replaced is a
    new filter and new O-rings too. Replacing a part renews nothing else."""

    parts: dict[tuple[str, str], set[str]] = {}
    for (location, component, part), _ in touches:
        if part:
            parts.setdefault((location, component), set()).add(part)
    # A stream an event already touches (a PM naming the part as well as its component) once is enough.
    touched = {(touch.event.record_id, key) for key, touch in touches}
    renewed = []
    for (location, component, part), touch in touches:
        if part or touch.renewal is None:
            continue
        for each in sorted(parts.get((location, component), ())):
            if (touch.event.record_id, (location, component, each)) in touched:
                continue
            renewed.append((
                (location, component, each),
                _Touch(touch.event, False, touch.exposure_end, touch.renewal, with_component=True),
            ))
    return touches + renewed


def _raw_hours(start: datetime, end: datetime) -> float:
    return (end - start).total_seconds() / 3600.0


def build_renewal_intervals(
    events: Iterable[StreamEvent],
    population: Population,
    *,
    cutoff: datetime,
    start: datetime | None = None,
    unknown_mechanism_as: str = UNKNOWN_AS_SUSPENSION,
    life_hours: Callable[[StreamKey, datetime, datetime], float] | None = None,
) -> RenewalAnalysis:
    """Every life in the population's pooled streams (spec section 5.2).

    Events are grouped into renewal streams (location + component + part) *first*, and
    each stream's intervals run from one renewal of that item to the next event in
    that same stream. Only then are streams pooled. ``life_hours`` measures an
    interval on a stream's schedule; raw elapsed hours without it.

    In a stream, ordered by when each event ended the item's exposure:

    * the first renewal starts the clock (the first event only starts it, as in
      REL-WBL); anything before it says nothing about a life with a known start;
    * a FAILURE counted for the population ends the life as a failure;
    * a FAILURE of another mechanism that renewed the item ends it as a suspension
      (a competing mode reset the clock); one that did not renew it changes nothing;
    * an unknown mechanism is a failure or a suspension as ``unknown_mechanism_as``
      says -- the caller runs both and reports both fits;
    * a counted FAILURE that did not renew the item (reset, adjust) still ends the
      life as a failure, but the clock does not restart: the item was not made as
      good as new, so no fresh life opens until it is next renewed, and the stream
      is flagged for recurrent-event (Crow-AMSAA) analysis;
    * a PM_RESET listing the item, or a FAILURE that renewed it alongside its own
      component, ends the life as a suspension and restarts the clock;
    * a life still running at ``cutoff`` is a suspension there (right-censored).

    Every other category never reaches a stream.
    """

    measure = life_hours or (lambda _stream, begin, end: _raw_hours(begin, end))
    by_stream: dict[StreamKey, list[_Touch]] = {}
    analysis = RenewalAnalysis()
    for key, touch in _with_parts([pair for event in events for pair in stream_touches(event)]):
        if population.stream_selected(*key):
            by_stream.setdefault(key, []).append(touch)

    seen_events: set[int] = set()
    for key in sorted(by_stream):
        analysis.streams.append(key)
        location, component, part = key
        recurrent = False
        touches = sorted(
            by_stream[key],
            key=lambda touch: (touch.exposure_end is None, touch.exposure_end or cutoff, touch.event.record_id),
        )
        clock: datetime | None = None
        clock_record: int | None = None

        def note(touch: _Touch, role: str, text: str) -> None:
            analysis.notes.append(StreamEventNote(
                location, component, touch.event.record_id, touch.event.task_id, touch.exposure_end, role, text, part
            ))

        for touch in touches:
            event = touch.event
            if touch.exposure_end is None:
                note(touch, ROLE_EXCLUDED, "Left out: no date to place it on the timeline"
                     + (" (the PM has no completion date)" if event.category == PM_RESET else "") + ".")
                continue
            if start is not None and touch.exposure_end < start:
                note(touch, ROLE_EXCLUDED, "Left out: before the analysis start.")
                continue
            if touch.exposure_end > cutoff:
                note(touch, ROLE_EXCLUDED, "Left out: after the analysis cutoff.")
                continue
            renewal = touch.renewal if touch.renewal is not None and touch.renewal <= cutoff else None
            if event.record_id not in seen_events:
                seen_events.add(event.record_id)
                analysis.stream_events.append(event)

            if touch.primary:
                if not event.renews_item:
                    recurrent = True
                counted = population.counts_as_failure(event, unknown_mechanism_as)
                if population.unknown_mechanism_decides(event):
                    analysis.unknown_decided.append(event)
                effect = FAILURE_END if counted else (SUSPENSION_END if event.renews_item else None)
            else:
                effect = SUSPENSION_END

            if clock is None:
                if renewal is not None:
                    clock, clock_record = renewal, event.record_id
                    note(touch, ROLE_STARTS_LIFE, "Starts the first life: no earlier renewal of this item is on record, "
                         "so it ends none.")
                else:
                    note(touch, ROLE_NO_EFFECT, "No effect: no life is running (the item has not been renewed since the "
                         "last failure that left it unrestored, or ever) and this work did not renew it.")
                continue

            if effect is None:
                note(touch, ROLE_NO_EFFECT,
                     "No effect: an induced failure, not counted as a failure here, that did not renew this item, so it ends no life."
                     if population.induced_as_suspension(event)
                     else "No effect: a failure of another kind that did not renew this item.")
                continue

            if touch.exposure_end <= clock:
                # The running life began with a renewal finished after this event, so
                # the item stays renewed from then: only a later renewal moves the
                # clock, and nothing here stops it.
                later = renewal is not None and renewal > clock
                note(touch, ROLE_STARTS_LIFE if later else ROLE_NO_EFFECT,
                     "No life: dated before the renewal that started the running life was finished. "
                     "Check for an overlapping or duplicate work order.")
                if later:
                    clock, clock_record = renewal, event.record_id
                continue

            hours = measure(key, clock, touch.exposure_end)
            raw = _raw_hours(clock, touch.exposure_end)
            if hours <= 0:
                note(touch, ROLE_NO_EFFECT, "No life: no scheduled hours since the life started.")
            else:
                if effect == FAILURE_END:
                    why = "Failure of this population."
                    if event.mechanism_unknown and population.failure_mechanisms and event.mechanism not in population.failure_mechanisms:
                        why = "Counted as a failure: its mechanism is unknown (sensitivity run)."
                    analysis.failure_events.append(event)
                elif touch.primary:
                    if population.unknown_mechanism_decides(event):
                        why = "Suspension: its mechanism is unknown (sensitivity run)."
                    elif population.induced_as_suspension(event):
                        why = ("Suspension: an induced failure (installation or maintenance error), counted as a "
                               "suspension; it renewed the item, resetting the clock.")
                    else:
                        why = "Suspension: a failure of another kind renewed the item, resetting the clock."
                elif touch.with_component:
                    why = "Suspension: renewed with the whole component it is part of."
                elif event.category == PM_RESET:
                    why = "Suspension: renewed by planned work."
                else:
                    why = "Suspension: renewed while repairing another item."
                analysis.intervals.append(LifeInterval(
                    location, component, clock, touch.exposure_end, effect, why,
                    clock_record, event.record_id, hours, raw, part,
                ))
                note(touch, ROLE_FAILURE if effect == FAILURE_END else ROLE_SUSPENSION, why)

            if renewal is not None:
                clock, clock_record = renewal, event.record_id
            else:
                # Ended a life without renewing the item: nothing restarts until the
                # item is renewed. Either the work did not restore it (reset, adjust),
                # or the repair had not finished by the cutoff.
                clock, clock_record = None, None

        if clock is not None and cutoff > clock:
            hours = measure(key, clock, cutoff)
            if hours > 0:
                analysis.intervals.append(LifeInterval(
                    location, component, clock, cutoff, SUSPENSION_END,
                    "Suspension: still in service at the analysis cutoff.", clock_record, None,
                    hours, _raw_hours(clock, cutoff), part,
                ))
        if recurrent:
            analysis.recurrent_streams.append(key)
    return analysis


def validity_warnings(
    analysis: RenewalAnalysis,
    population: Population,
    *,
    sensitivity: RenewalAnalysis | None = None,
    codebook: CodeBook | None = None,
    uncoded_count: int = 0,
    unreviewed_count: int = 0,
) -> list[dict[str, str]]:
    """The spec section 5.3 warnings for one run. They warn; they never block.

    ``analysis`` is the run with unknown mechanisms as suspensions, ``sensitivity``
    the one with them as failures, when the population makes the two differ.
    """

    def name(code_list: str, code: str | None) -> str:
        if code is None:
            return "uncoded"
        return codebook.name(code_list, code) if codebook is not None else code

    warnings: list[dict[str, str]] = []

    def warn(code: str, title: str, message: str, level: str = "warning") -> None:
        warnings.append({"code": code, "level": level, "title": title, "message": message})

    # A kind of item is its part, when the streams are parts' (every filter is
    # one kind, whatever it is in), else its component.
    kinds = sorted({name(PART, part) if part else name(COMPONENT, component) for _, component, part in analysis.streams})
    if len(kinds) > 1:
        warn(
            "MIXED_COMPONENTS",
            "Mixed components",
            "Pattern-only; intervals mix different items. Each life is still measured on one item, but "
            f"{len(kinds)} kinds of item are pooled into one fit ({', '.join(kinds)}). "
            "Narrow the population to one component, or one part, before acting on beta.",
        )
    if population.narrows_patterns_only():
        warn(
            "PATTERN_ONLY_FILTERS",
            "Mode, cause and action not used",
            "The failure mode, cause and action ticked on the population narrow the pattern views only. The Weibull "
            "counts failures by mechanism: one mechanism can present as several modes, a cause other than induced "
            "(its own checkbox) restates the mechanism, and the action is what was done after the failure.",
            level="info",
        )
    mechanisms = sorted({event.mechanism or "" for event in analysis.failure_events})
    if len(mechanisms) > 1:
        warn(
            "MIXED_MECHANISMS",
            "More than one mechanism counted as failure",
            f"{len(mechanisms)} mechanisms end lives as failures "
            f"({', '.join(name(FAILURE_MECHANISM, m or None) for m in mechanisms)}). Different mechanisms wear out "
            "differently; one fit over them blurs both.",
        )
    # Read from the run that counts unknown mechanisms as failures: with no
    # mechanism selected that is the main run, since every mechanism counts.
    counted = sensitivity if sensitivity is not None else analysis
    unknown = sum(1 for event in counted.failure_events if event.mechanism_unknown)
    total = len(counted.failure_events)
    if unknown:
        share = unknown / total if total else 1.0
        bounds = ""
        if sensitivity is not None:
            bounds = (
                f" Both fits are shown: {analysis.failure_count} failures counting them as suspensions, "
                f"{sensitivity.failure_count} counting them as failures."
            )
        warn(
            "UNKNOWN_SHARE",
            "Failures with an unknown mechanism",
            f"{unknown} of {total} failures ({share:.0%}) have an unknown mechanism.{bounds}",
        )
    failures = analysis.failure_count
    if failures < SMALL_N_FAILURES:
        warn(
            "SMALL_N",
            "Few failures",
            f"Only {failures} lives end in a failure. Below about {SMALL_N_FAILURES} the fit is unstable; read beta as a "
            "direction at most.",
        )
    multi = sorted({event.task_id or str(event.record_id) for event in analysis.stream_events if (event.qty or 1) > 1})
    if multi:
        warn(
            "MULTI_UNIT",
            "Multiple identical units",
            f"{len(multi)} work orders renewed several identical units at once (qty > 1: {', '.join(multi[:5])}"
            f"{'…' if len(multi) > 5 else ''}). Per-unit lives are not known, so analyse the location and component "
            "as a repairable system by rate (Crow-AMSAA) rather than by per-unit Weibull.",
        )
    induced = [event for event in analysis.failure_events if event.cause_induced]
    if induced and not population.exclude_induced:
        warn(
            "INDUCED_FAILURES",
            "Induced failures present",
            (f"1 failure was caused by maintenance or installation error. It was introduced by the work, not by "
             f"the item's wear; tick '{INDUCED_SWITCH_LABEL}' to leave it out of the failures."
             if len(induced) == 1 else
             f"{len(induced)} failures were caused by maintenance or installation error. Those are introduced by the "
             f"work, not by the item's wear; tick '{INDUCED_SWITCH_LABEL}' to leave them out of the failures."),
        )
    if analysis.recurrent_streams:
        warn(
            "RECURRENT_EVENTS",
            "Failures fixed without renewing the item",
            f"{len(analysis.recurrent_streams)} streams have failures cleared by a reset or adjustment. The item was "
            "not renewed, so no new life starts after them until it is; consider recurrent-event (Crow-AMSAA) "
            "analysis for those streams.",
        )
    if uncoded_count:
        warn(
            "UNCODED_RECORDS",
            "Records not yet coded",
            f"{uncoded_count} failure or PM reset records in scope have no component (usually migrated from the old "
            "categories and not yet re-dispositioned), so they belong to no stream and are left out.",
        )
    if unreviewed_count:
        warn(
            "NOT_REVIEWED",
            "Records not reviewed",
            f"{unreviewed_count} work orders in scope are not reviewed yet and are left out.",
            level="info",
        )
    return warnings
