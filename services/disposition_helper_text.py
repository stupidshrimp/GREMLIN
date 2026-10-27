"""How the disposition helper reads a work order: its words, and what they mean.

A work order says the same thing a dozen ways. "Clogged", "plugged", "blocked"
and "restricted" are one failure mechanism; "won't light", "no ignition" and "will
not fire" are one failure mode; "swapped", "changed out" and "installed new" are
one action. Counting words alone would see four different work orders there, so
each one is read on two levels:

* **Words**, stemmed so "leaking", "leaked" and "leaks" are one word, with
  common misspellings pulled onto a word the plant's own work orders use.
* **Concepts**, from a thesaurus of maintenance phrasing and from the code lists
  themselves (every name segment of a code is a phrase for it: "Filter /
  strainer / sieve" is three). A concept read after "no" or "not" in the same
  clause is read as negated: "no leaks found" is evidence against a leak, not
  for one.

Both become the features two work orders are compared on, and the concepts are
also direct evidence for a code, quoted back to the engineer as the reason for
it. Everything here is plain data and functions, with no database.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Iterable, Mapping, Sequence

from services import disposition_standard as std

# ---------------------------------------------------------------------------
# Words
# ---------------------------------------------------------------------------
_CLAUSE_BREAK = re.compile(r"[.;:!?\n\r\t|•]+|,\s|\s-\s|\s–\s")
_APOSTROPHE = re.compile(r"(?<=[a-z])['’](?=[a-z])")
_WORD = re.compile(r"[a-z0-9]+")

STOPWORDS = frozenset(
    """
    a an the and or to of in on at for from by with is are was were be been being it its this that these those
    has have had as per please pls also then than so if but into onto up down out off over under about after
    before again once there here when where while which who what all any each both other some such only own same
    too very can will just should would could may might must shall do does did doing i we you he she they them
    me my our your his her their am im ive weve s t re d ll m o v x
    wo work order task request req asset item unit line area machine equipment
    """.split()
)
# Words that make what follows them in the clause a "not": "no leaks", "not
# damaged". Those that usually mean a failure ("won't", "can't") are not here;
# the thesaurus reads them as part of their phrase ("won't start").
NEGATORS = frozenset(
    "no not non without never none nothing wasnt isnt didnt doesnt dont arent werent cannot nor".split()
)
NEGATION_REACH = 4
# Nouns that end like a verb form and must keep their ending: "bearing" is not "bear".
_KEEP_WHOLE = frozenset(
    """
    bearing coupling fitting housing tubing wiring piping spring string ring lighting ceiling casing bushing
    mounting packing railing grating building seating opening thing nothing something during morning evening
    sling bring shielding flashing siding decking awning bedding leading heading drawing setting lining
    bus status plus axis this gas glass brass press pass class process series
    """.split()
)


@lru_cache(maxsize=200_000)
def stem(word: str) -> str:
    """A light stemmer for maintenance English: plurals, -ed and -ing, and a final e.

    Light on purpose: "replace", "replaced" and "replacing" meet, "bearing" is
    left a bearing, and nothing is cut so far that two different parts meet.
    """

    if len(word) <= 3 or not word.isalpha() or word in _KEEP_WHOLE:
        return word
    if word.endswith("ies") and len(word) > 4:
        word = word[:-3] + "y"
    elif word.endswith("sses"):
        word = word[:-2]
    elif word.endswith("s") and not word.endswith(("ss", "us", "is")):
        word = word[:-1]
    if word in _KEEP_WHOLE:
        return word
    trimmed = None
    if word.endswith("ing") and len(word) > 5:
        trimmed = word[:-3]
    elif word.endswith("ed") and len(word) > 4 and not word.endswith("eed"):
        trimmed = word[:-2]
    if trimmed is not None and re.search(r"[aeiouy]", trimmed):
        word = trimmed
        if len(word) > 2 and word[-1] == word[-2] and word[-1] not in "lsz" and word[-1] not in "aeiou":
            word = word[:-1]
        return word
    if len(word) > 4 and word.endswith("e") and not word.endswith("ee"):
        word = word[:-1]
    return word


# The small words and negators as the stemmer leaves them ("these" -> "thes").
_STOP_STEMS = frozenset(STOPWORDS | {stem(word) for word in STOPWORDS})
_NEGATOR_STEMS = frozenset(NEGATORS | {stem(word) for word in NEGATORS})


def clauses(text: Any) -> list[str]:
    """Text in clauses, lowercased, so a negation stops where its sentence or list item does."""

    if text is None:
        return []
    value = str(text).lower()
    value = _APOSTROPHE.sub("", value)
    return [part for part in _CLAUSE_BREAK.split(value) if part.strip()]


def tokens(clause: str) -> list[str]:
    """One clause as stems, every word kept (phrases need their small words)."""

    return [stem(word) for word in _WORD.findall(clause)]


def is_content(token: str) -> bool:
    """Whether a stem says anything on its own: not a small word, and not a bare number."""

    return token not in _STOP_STEMS and token not in _NEGATOR_STEMS and not token.isdigit() and len(token) > 1


def split_identifier(code: str) -> str:
    """A code as words: "GasTrain" -> "gas train", "CLEAN_SERVICE" -> "clean service"."""

    text = re.sub(r"([a-z])([A-Z])", r"\1 \2", code)
    return re.sub(r"[_\-.]+", " ", text).lower()


# ---------------------------------------------------------------------------
# The thesaurus
# ---------------------------------------------------------------------------
# Built-in phrasing for the seed codes (and, for the failure modes no fault found
# and degraded, for whichever code carries that meaning). A list the team has
# changed keeps what still applies: a phrase only counts for a code that is in
# the list and active. Every code's own name segments are phrases for it too, so
# a code the team adds is recognised by its name from the start.
COMPONENT_PHRASES: dict[str, tuple[str, ...]] = {
    "Filter": ("filter", "strainer", "sieve", "filter bag", "air filter", "intake filter", "prefilter", "pre filter"),
    "Belt": ("belt", "v belt", "vbelt", "drive belt", "timing belt", "fan belt"),
    "Bearing": ("bearing", "pillow block", "bushing", "flange bearing", "bearing housing"),
    "Motor": ("motor", "mtr", "gearmotor", "gear motor", "blower motor", "fan motor", "pump motor"),
    "Fan": ("fan", "blower", "exhaust fan", "impeller", "squirrel cage", "fan blade", "supply fan"),
    "Shaft": ("shaft", "pulley", "coupling", "sheave", "keyway", "key stock", "lovejoy", "spider"),
    "Burner": (
        "burner", "flame rod", "flame sensor", "igniter", "ignitor", "ignition", "pilot", "spark rod", "uv scanner",
        "flame scanner", "flame", "spark plug",
    ),
    "GasTrain": ("gas train", "gas valve", "fuel valve", "gas regulator", "main gas valve", "safety shutoff valve"),
    "Valve": ("valve", "solenoid", "solenoid valve", "ball valve", "butterfly valve", "gate valve", "needle valve"),
    "Actuator": ("actuator", "cylinder", "air cylinder", "damper actuator", "damper motor", "positioner", "damper"),
    "Pump": ("pump", "transfer pump", "metering pump", "diaphragm pump"),
    "Seal": ("seal", "gasket", "diaphragm", "o ring", "oring", "check valve", "packing", "mechanical seal", "lip seal"),
    "Pipe": ("pipe", "piping", "tubing", "tube", "fitting", "union", "elbow", "plumbing", "water line", "drain line"),
    "Hose": ("hose", "air line", "airline", "quick connect", "quick disconnect", "push lock", "pneumatic fitting"),
    "Tank": ("tank", "hopper", "heat exchanger", "reservoir", "sump", "vat", "stage tank"),
    "Nozzle": ("nozzle", "riser", "spray tip", "spray nozzle", "jet", "spray header"),
    "Gun": ("gun", "powder gun", "applicator", "spray gun", "gun tip"),
    "Chain": ("chain", "sprocket", "drive chain", "chain tensioner", "idler", "roller chain"),
    "Conveyor": ("conveyor", "carrier", "hanger", "trolley", "rail", "track", "conveyor chain", "rack"),
    "Sensor": (
        "sensor", "switch", "limit switch", "prox", "proximity", "proximity switch", "photo eye", "photoeye", "gauge",
        "thermocouple", "rtd", "transmitter", "probe", "pressure switch", "flow switch", "float", "level switch",
        "encoder", "thermostat", "airflow switch",
    ),
    "Controller": (
        "plc", "hmi", "controller", "panelview", "touch screen", "touchscreen", "screen", "program",
        "temperature controller", "temp controller", "io card", "io module", "computer",
    ),
    "Starter": (
        "starter", "contactor", "relay", "vfd", "variable frequency drive", "drive", "overload", "inverter",
        "soft start", "motor starter", "overload relay",
    ),
    "Electrical": (
        "electrical", "fuse", "breaker", "circuit breaker", "wiring", "wire", "disconnect", "outlet", "receptacle",
        "cord", "cable", "terminal", "transformer", "power supply", "junction box", "conduit", "light", "bulb",
        "lamp", "ballast",
    ),
    "Door": ("door", "guard", "cover", "latch", "hinge", "handle", "lock", "access door", "panel door"),
    "HVAC": (
        "hvac", "compressor", "chiller", "air conditioner", "ac unit", "condenser", "evaporator", "refrigerant",
        "freon", "rtu", "rooftop unit", "makeup air", "make up air",
    ),
}
MECHANISM_PHRASES: dict[str, tuple[str, ...]] = {
    "WEAR": (
        "wear", "worn", "worn out", "frayed", "stretched", "glazed", "bald", "wore out", "eroded", "erosion",
        "end of life", "deteriorated", "deteriorating", "worn down", "wearing",
    ),
    "PLUG": (
        "clog", "clogged", "plugged", "plugged up", "blocked", "blockage", "restricted", "restriction", "choked",
        "obstructed", "obstruction", "jammed", "jam", "stopped up",
    ),
    "CONT": (
        "dirty", "contaminated", "contamination", "fouled", "buildup", "build up", "built up", "residue", "sludge",
        "debris", "gunk", "scale", "scaled", "covered in", "full of", "loaded up", "caked",
    ),
    "LEAK": ("leak", "leaking", "dripping", "drip", "seeping", "seep", "weeping", "spraying", "puddle"),
    "DEFM": (
        "damaged", "damage", "bent", "dented", "dent", "crushed", "deformed", "warped", "smashed", "mangled",
        "collapsed", "kinked",
    ),
    "LOOS": (
        "loose", "loosened", "came loose", "fell off", "misaligned", "misalignment", "out of alignment", "slipped",
        "slipping", "out of adjustment", "off track", "derailed", "came off", "backed out", "walked off",
    ),
    "BRKN": (
        "broken", "broke", "break", "snapped", "snap", "cracked", "crack", "fractured", "sheared", "split", "torn",
        "tore", "ripped", "rip", "busted", "shattered",
    ),
    "OVHT": (
        "overheat", "overheated", "overheating", "hot", "burnt", "burned", "burn", "smoking", "smoke", "melted",
        "scorched", "too hot", "running hot",
    ),
    "ELEC": (
        "short", "shorted", "short circuit", "grounded", "ground fault", "blown fuse", "blew fuse", "blown",
        "tripped breaker", "breaker tripped", "open circuit", "phase loss", "single phasing", "bad wire",
        "electrical fault", "overload tripped", "amp draw", "high amps", "no voltage", "no power",
    ),
    "SIGN": (
        "signal", "fault code", "error code", "error", "alarm", "communication", "comm", "comms",
        "lost communication", "programming", "out of calibration", "calibration", "reading wrong", "false reading",
        "sensor fault", "bad sensor", "faulty sensor", "plc fault", "glitch", "not reading", "bad reading",
    ),
}
MODE_PHRASES: dict[str, tuple[str, ...]] = {
    "FTS": (
        "wont start", "will not start", "would not start", "does not start", "doesnt start", "didnt start",
        "fail to start", "failed to start", "not starting", "no start", "wont light", "will not light",
        "would not light", "wont fire", "will not fire", "no ignition", "not igniting", "wont ignite",
        "wont turn on", "will not turn on", "wont run", "will not run", "not running", "wont come on",
        "does not come on", "inoperable", "dead", "not lighting", "no flame",
    ),
    "UST": (
        "trip", "tripped", "tripping", "shut down", "shutdown", "shuts down", "shutting down", "stopped", "stops",
        "keeps stopping", "kicked out", "kicks out", "faulted", "faulting", "fault out", "faulted out", "went down",
        "goes down", "cut out", "cuts out", "dropping out", "drops out", "keeps tripping",
    ),
    "LOO": (
        "low", "low temp", "low temperature", "low pressure", "low flow", "low output", "weak", "slow",
        "not heating", "not cooling", "wont heat", "cant reach", "cannot reach", "not reaching", "under temp",
        "not hot enough", "insufficient", "low airflow", "not enough", "not getting to temp", "cant get to temp",
        "wont get up to temp", "not making temp",
    ),
    "ERO": (
        "erratic", "intermittent", "intermittently", "fluctuating", "fluctuate", "abnormal", "unstable", "hunting",
        "bouncing", "wrong reading", "false", "incorrect", "inaccurate", "spiking", "surging", "cycling",
        "high temp", "high temperature", "high pressure", "overshoot", "erratically",
    ),
    "LKG": ("leak", "leaking", "dripping", "drip", "seeping", "spraying", "puddle"),
    "VIB": (
        "noise", "noisy", "loud", "squeal", "squealing", "squeak", "squeaking", "grinding", "grind", "vibration",
        "vibrating", "vibrate", "rattle", "rattling", "banging", "bang", "knocking", "knock", "chatter",
        "chattering", "screeching", "whine", "whining",
    ),
    "STD": ("damaged", "damage", "broken", "bent", "cracked", "crushed", "dented", "torn", "smashed", "hit"),
}
# Modes found by what the code means rather than its spelling.
MODE_SEMANTIC_PHRASES: dict[str, tuple[str, ...]] = {
    std.SEMANTIC_NO_FAULT_FOUND: (
        "no fault found", "nff", "no problem found", "no issue found", "no issues found", "no problems found",
        "could not find", "couldnt find", "unable to find", "unable to duplicate", "could not duplicate",
        "could not reproduce", "working fine", "working properly", "working normally", "operating normally",
        "operating properly", "running fine", "running normally", "running good", "tested ok", "tested good",
        "checked ok", "checked good", "everything ok", "all ok", "no issues", "nothing wrong", "found nothing",
        "no problem", "no problems", "ok at this time", "working as designed",
    ),
    std.SEMANTIC_INSPECTION_FINDING: (
        "found during pm", "found on pm", "during pm", "during inspection", "on inspection", "noticed",
        "found worn", "inspection found", "showing wear", "starting to", "beginning to", "needs replaced soon",
        "getting worn", "found during", "while doing pm", "found while",
    ),
}
ACTION_PHRASES: dict[str, tuple[str, ...]] = {
    "REPLACE": (
        "replace", "replaced", "replacement", "replacing", "swap", "swapped", "swapped out", "change out",
        "changed out", "changed", "change", "install new", "installed new", "new", "put new", "put in new",
        "installed a new", "installed replacement",
    ),
    "REPAIR": (
        "repair", "repaired", "fix", "fixed", "rebuild", "rebuilt", "weld", "welded", "patch", "patched", "splice",
        "spliced", "rewire", "rewired", "mend", "reterminate", "re terminate", "braze", "solder", "reattach",
        "re attach", "re attached", "reattached",
    ),
    "CLEAN_SERVICE": (
        "clean", "cleaned", "cleaning", "blow out", "blew out", "blown out", "vacuum", "vacuumed", "flush",
        "flushed", "lubricate", "lubricated", "lube", "grease", "greased", "oil", "oiled", "service", "serviced",
        "wipe", "wiped", "wash", "washed", "scrape", "scraped", "descale", "degrease",
    ),
    "ADJUST": (
        "adjust", "adjusted", "tighten", "tightened", "align", "aligned", "realign", "realigned", "calibrate",
        "calibrated", "tension", "tensioned", "retension", "retighten", "shim", "shimmed", "level", "leveled",
    ),
    "RESET_ONLY": (
        "reset", "restart", "restarted", "reboot", "rebooted", "power cycle", "cycled power", "cycle power",
        "reset breaker", "reset fault", "cleared fault", "clear fault", "acknowledged alarm", "turned back on",
        "relit", "re lit", "re light", "relight",
    ),
    "INSPECT_ONLY": (
        "inspect", "inspected", "check", "checked", "verify", "verified", "look", "looked", "test", "tested",
        "monitor", "monitored", "troubleshoot", "troubleshot", "observe", "observed", "investigate",
    ),
}
CAUSE_PHRASES: dict[str, tuple[str, ...]] = {
    "INSTALL": (
        "installed wrong", "installed incorrectly", "improper install", "improperly installed", "installed backwards",
        "wrong install", "installation error",
    ),
    "OPERATE": ("operator error", "misuse", "wrong setting", "operator changed", "operator turned"),
    "MNT-ERR": (
        "wrong part", "incorrect part", "maintenance error", "left loose", "not tightened", "reassembled wrong",
        "improperly repaired", "installed wrong part",
    ),
    "MNT-INT": ("overdue", "past due", "missed pm", "pm missed", "never changed", "not changed", "should have been changed"),
    "EXP-WEAR": ("normal wear", "wear and tear", "expected wear", "old age", "end of life", "life expectancy"),
    "DESIGN": ("undersized", "under sized", "not rated", "design flaw", "poor design", "design issue"),
    "MGMT": ("no procedure", "procedure not followed", "not trained", "training issue"),
}
# Evidence for a category, beside what the similar work orders were dispositioned as.
CATEGORY_PHRASES: dict[str, tuple[str, ...]] = {
    std.EXTERNAL: (
        "power outage", "power out", "power failure", "power bump", "power blip", "utility", "utilities",
        "plant power", "gas off", "gas was off", "gas shut off", "gas supply", "no gas", "main gas off",
        "air supply", "no air", "plant air", "low plant air", "water off", "no water", "operator error",
        "forklift", "hit by", "struck by", "ran into", "upstream", "downstream", "storm", "lightning",
        "no product", "material issue",
    ),
    std.NON_FAILURE: (
        "parts delivery", "deliver parts", "pick up parts", "order parts", "ordered parts", "quote", "project",
        "improvement", "upgrade", "modify", "modification", "relocate", "move", "setpoint change",
        "change setpoint", "adjust setpoint", "admin", "training", "meeting", "clean up", "housekeeping", "paint",
        "label", "rebuild for stock", "send out", "sent out for repair", "trial", "mockup", "inventory",
    ),
    std.PM_RESET: (
        "pm", "preventive", "preventative", "monthly", "weekly", "quarterly", "annual", "annually", "semi annual",
        "biweekly", "scheduled", "routine", "lube route", "pm reset",
    ),
    std.FAILURE: (
        "broken", "failed", "failure", "down", "not working", "inoperable", "leak", "tripped", "wont start",
        "stuck", "jammed", "damaged", "wont light", "no heat",
    ),
}

# What evidence a field is read from: the action is what the technician says
# they did, the mechanism what they found, the mode what was noticed first.
FIELD_LABELS = {
    "task_name": "title",
    "request_title": "request title",
    "requestor_description": "request description",
    "area_affected": "area affected",
    "condition_found": "condition found",
    "cause": "cause",
    "action_taken": "action taken",
    "completion_notes": "completion notes",
    "asset_name": "asset named on the work order",
    "legacy": "REL disposition",
}
# How much each part of the work order counts towards similarity.
FIELD_WEIGHTS = {
    "task_name": 2.0,
    "request_title": 1.5,
    "requestor_description": 1.0,
    "area_affected": 1.0,
    "condition_found": 1.5,
    "cause": 1.2,
    "action_taken": 1.5,
    "completion_notes": 1.0,
    "asset_name": 0.8,
    "legacy": 0.8,
}
TEXT_FIELDS = tuple(FIELD_WEIGHTS)
_ROLE_WEIGHTS: dict[str, dict[str, float]] = {
    std.ACTION: {"action_taken": 1.6, "completion_notes": 1.3, "task_name": 1.0, "request_title": 0.5,
                 "requestor_description": 0.4, "cause": 0.5, "condition_found": 0.6},
    std.FAILURE_MECHANISM: {"condition_found": 1.6, "cause": 1.5, "completion_notes": 1.1, "action_taken": 0.8},
    std.FAILURE_MODE: {"task_name": 1.3, "request_title": 1.3, "requestor_description": 1.3, "condition_found": 1.0,
                       "action_taken": 0.5},
    std.FAILURE_CAUSE: {"cause": 1.8, "condition_found": 1.0},
    std.COMPONENT: {"area_affected": 1.4, "condition_found": 1.2, "action_taken": 1.2, "task_name": 1.2},
    "CATEGORY": {"task_name": 1.3, "request_title": 1.0, "action_taken": 1.0, "completion_notes": 1.0},
}
CATEGORY_KEY = "CATEGORY"


def role_weight(code_list: str, field_name: str) -> float:
    return _ROLE_WEIGHTS.get(code_list, {}).get(field_name, 1.0)


@dataclass(frozen=True)
class LexiconEntry:
    key: str  # "COMPONENT:Filter", "CATEGORY:EXTERNAL"
    weight: float
    source: str  # "name" (the code's own name) or "thesaurus"


@dataclass
class Lexicon:
    """Phrases to the codes and categories they are evidence for, as stems."""

    phrases: dict[tuple[str, ...], list[LexiconEntry]] = field(default_factory=dict)
    by_first: dict[str, list[tuple[str, ...]]] = field(default_factory=dict)
    # Each code's description as content stems: weaker evidence than a phrase,
    # but it lets the team teach the helper a word by writing it into a description.
    descriptions: dict[str, frozenset[str]] = field(default_factory=dict)

    def add(self, phrase: str, key: str, weight: float, source: str) -> None:
        stems = tuple(token for clause in clauses(phrase) for token in tokens(clause))
        if not stems or not any(is_content(token) for token in stems):
            return
        entries = self.phrases.setdefault(stems, [])
        for index, entry in enumerate(entries):
            if entry.key == key:
                if weight > entry.weight:
                    entries[index] = LexiconEntry(key, weight, source)
                return
        entries.append(LexiconEntry(key, weight, source))

    def finish(self) -> "Lexicon":
        self.by_first = {}
        for stems in self.phrases:
            self.by_first.setdefault(stems[0], []).append(stems)
        for options in self.by_first.values():
            options.sort(key=len, reverse=True)
        return self

    def words(self) -> set[str]:
        return {token for stems in self.phrases for token in stems if is_content(token)}


def _name_segments(name: str) -> list[str]:
    parts = re.split(r"[/,;:()]|\bor\b|\bincl\.?", name or "")
    return [part.strip() for part in parts if part and part.strip()]


def build_lexicon(codebook: std.CodeBook) -> Lexicon:
    """The thesaurus for the code lists as the team has them now."""

    lexicon = Lexicon()
    lists = (std.COMPONENT, std.FAILURE_MECHANISM, std.FAILURE_MODE, std.ACTION, std.FAILURE_CAUSE)
    for code_list in lists:
        for entry in codebook.entries(code_list):
            if entry.semantic in (std.SEMANTIC_UNKNOWN, std.SEMANTIC_REVIEW):
                # "Unknown" and "Other" are what is left when nothing is read,
                # never something to read off a work order.
                continue
            key = f"{code_list}:{entry.code}"
            for segment in _name_segments(entry.name):
                lexicon.add(segment, key, 1.0, "name")
            if code_list == std.COMPONENT:
                lexicon.add(split_identifier(entry.code), key, 1.0, "name")
            description = {
                token for clause in clauses(entry.description) for token in tokens(clause) if is_content(token)
            }
            if description:
                lexicon.descriptions[key] = frozenset(description)
    builtin: list[tuple[str, Mapping[str, Sequence[str]]]] = [
        (std.COMPONENT, COMPONENT_PHRASES),
        (std.FAILURE_MECHANISM, MECHANISM_PHRASES),
        (std.FAILURE_MODE, MODE_PHRASES),
        (std.ACTION, ACTION_PHRASES),
        (std.FAILURE_CAUSE, CAUSE_PHRASES),
    ]
    for code_list, table in builtin:
        for code, phrases in table.items():
            entry = codebook.entry(code_list, code)
            if entry is None or not entry.is_active:
                continue
            for phrase in phrases:
                lexicon.add(phrase, f"{code_list}:{code}", 0.9, "thesaurus")
    for semantic, phrases in MODE_SEMANTIC_PHRASES.items():
        for entry in codebook.entries(std.FAILURE_MODE):
            if entry.semantic == semantic:
                for phrase in phrases:
                    lexicon.add(phrase, f"{std.FAILURE_MODE}:{entry.code}", 1.0, "thesaurus")
    for category, phrases in CATEGORY_PHRASES.items():
        for phrase in phrases:
            lexicon.add(phrase, f"{CATEGORY_KEY}:{category}", 0.8, "thesaurus")
    # No fault found is a category signal as much as a mode.
    for phrase in MODE_SEMANTIC_PHRASES[std.SEMANTIC_NO_FAULT_FOUND]:
        lexicon.add(phrase, f"{CATEGORY_KEY}:{std.NON_FAILURE}", 1.0, "thesaurus")
    return lexicon.finish()


# ---------------------------------------------------------------------------
# Reading one work order
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Mention:
    """One phrase read off the work order as evidence for a code or category."""

    key: str
    field: str
    phrase: str
    weight: float
    negated: bool
    source: str


@dataclass
class Reading:
    """A work order as the helper compares and explains it."""

    features: dict[str, float]
    mentions: list[Mention]
    content: dict[str, set[str]]  # each field's content stems, for the descriptions and sub-asset names
    words: list[str]  # the work order's raw words, for sub-asset tokens such as "S09"


class Speller:
    """Pulls a misspelt word onto one the plant's work orders or the thesaurus use.

    Only a word nobody else uses is touched, and only onto a word one edit away
    (a letter dropped, added, changed or two swapped), so "bearng" reads as
    "bearing" and a part number is left alone.
    """

    def __init__(self, known: Mapping[str, int]) -> None:
        self.known = known
        self._deletes: dict[str, list[str]] = {}
        for word in known:
            if len(word) < 4 or word.isdigit():
                continue
            for variant in self._variants(word):
                self._deletes.setdefault(variant, []).append(word)

    @staticmethod
    def _variants(word: str) -> set[str]:
        return {word[:index] + word[index + 1:] for index in range(len(word))}

    @lru_cache(maxsize=50_000)
    def correct(self, word: str) -> str:
        if len(word) < 5 or word in self.known or any(ch.isdigit() for ch in word):
            return word
        candidates: set[str] = set(self._deletes.get(word, ()))
        for variant in self._variants(word):
            if variant in self.known:
                candidates.add(variant)
            candidates.update(self._deletes.get(variant, ()))
        candidates = {candidate for candidate in candidates if candidate[0] == word[0] and abs(len(candidate) - len(word)) <= 1}
        if not candidates:
            return word
        return max(candidates, key=lambda candidate: (self.known.get(candidate, 0), candidate))


def read_work_order(
    texts: Mapping[str, Any],
    lexicon: Lexicon,
    *,
    record_class: str | None = None,
    speller: Speller | None = None,
) -> Reading:
    """Read one work order's text fields into features and evidence.

    ``texts`` maps TEXT_FIELDS to the work order's text. A request title that
    repeats the task title is read once.
    """

    features: dict[str, float] = {}
    mentions: list[Mention] = []
    content: dict[str, set[str]] = {}
    raw_words: list[str] = []
    title = str(texts.get("task_name") or "").strip().casefold()
    for field_name in TEXT_FIELDS:
        value = texts.get(field_name)
        if value is None or not str(value).strip():
            continue
        if field_name == "request_title" and str(value).strip().casefold() == title:
            continue
        weight = FIELD_WEIGHTS[field_name]
        field_content: set[str] = set()
        for clause in clauses(value):
            words = _WORD.findall(clause)
            raw_words.extend(words)
            stems = [stem(word) for word in words]
            if speller is not None:
                stems = [speller.correct(token) for token in stems]
            previous: str | None = None
            for token in stems:
                if not is_content(token):
                    previous = None
                    continue
                field_content.add(token)
                features[f"w:{token}"] = features.get(f"w:{token}", 0.0) + weight
                if previous is not None:
                    pair = f"b:{previous} {token}"
                    features[pair] = features.get(pair, 0.0) + 0.5 * weight
                previous = token
            for mention in _phrases_in(stems, words, lexicon, field_name):
                mentions.append(mention)
                feature = f"{'n' if mention.negated else 'c'}:{mention.key}"
                features[feature] = features.get(feature, 0.0) + weight * mention.weight
        if field_content:
            content[field_name] = field_content
    if record_class:
        features[f"rc:{record_class.upper()}"] = 1.5
    return Reading(features=features, mentions=mentions, content=content, words=raw_words)


def _phrases_in(stems: list[str], words: list[str], lexicon: Lexicon, field_name: str) -> Iterable[Mention]:
    """Each thesaurus phrase in one clause, longest first, with whether a "no" or "not" reaches it."""

    negated_until = -1
    index = 0
    while index < len(stems):
        token = stems[index]
        matched = None
        for option in lexicon.by_first.get(token, ()):
            if tuple(stems[index:index + len(option)]) == option:
                matched = option
                break
        if matched is not None:
            phrase = " ".join(words[index:index + len(matched)])
            negated = index <= negated_until
            for entry in lexicon.phrases[matched]:
                yield Mention(entry.key, field_name, phrase, entry.weight, negated, entry.source)
            index += len(matched)
            continue
        if token in _NEGATOR_STEMS:
            negated_until = index + NEGATION_REACH
        index += 1
