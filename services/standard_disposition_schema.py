"""The tables the work-order disposition standard keeps in GREMLIN.db, and the
migration that fills them from the REL dispositions already there.

Kept apart from both ``life_data_service`` (which calls into it while building
its own schema, and after every save on the old disposition screen) and
``standard_disposition_service`` (which reads and writes through it), so neither
has to import the other to get here.

Three tables:

* ``disposition_code`` -- the code lists (component, mechanism, mode, cause,
  action). Configuration the team edits, seeded once from
  ``disposition_standard.SEED_CODES``. A code is the stable key dispositions and
  saved populations store, so it never changes; its name, ISO reference, default
  cause and the rest can.
* ``wo_disposition`` -- one work order's disposition under the standard, with its
  history: saving retires the current row and adds a new one, as
  ``event_disposition`` does. Validation lives in
  ``disposition_standard.normalize_disposition`` rather than in CHECK
  constraints, so the rules can change without rebuilding the table.
* ``population_weibull_analysis`` -- every saved renewal-stream Weibull run, with
  the population definition string it was made from.
"""

from __future__ import annotations

import itertools
import json
import re
import sqlite3
import threading
from typing import Any, Iterable, Mapping

from services import disposition_standard as std

_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS disposition_code (
        disposition_code_id INTEGER PRIMARY KEY,
        code_list TEXT NOT NULL,
        code TEXT NOT NULL,
        name TEXT NOT NULL,
        description TEXT,
        iso_ref TEXT,
        default_cause_code TEXT,
        renews_item INTEGER NOT NULL DEFAULT 0,
        semantic TEXT,
        sort_order INTEGER NOT NULL DEFAULT 0,
        is_active INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        updated_at TEXT NOT NULL DEFAULT (datetime('now')),
        updated_by TEXT,
        UNIQUE (code_list, code)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_disposition_code_list ON disposition_code(code_list, is_active, sort_order)",
    """
    CREATE TABLE IF NOT EXISTS wo_disposition (
        wo_disposition_id INTEGER PRIMARY KEY,
        mapped_record_id INTEGER NOT NULL,
        disposition_category TEXT NOT NULL DEFAULT 'NOT_REVIEWED',
        sub_asset TEXT,
        component_code TEXT,
        part_code TEXT,
        failure_mechanism_code TEXT,
        failure_mode_code TEXT,
        action_code TEXT,
        evidence TEXT,
        failure_cause_code TEXT,
        renewed_items_json TEXT NOT NULL DEFAULT '[]',
        qty INTEGER,
        linked_mapped_record_id INTEGER,
        disposition_note TEXT,
        record_class TEXT,
        legacy_disposition_category TEXT,
        legacy_failure_mode TEXT,
        legacy_failure_mechanism TEXT,
        source TEXT NOT NULL DEFAULT 'ENGINEER',
        migrated_from_event_disposition_id INTEGER,
        review_flag TEXT,
        decided_by TEXT,
        decided_at TEXT NOT NULL DEFAULT (datetime('now')),
        is_current INTEGER NOT NULL DEFAULT 1,
        helper_run_id INTEGER,
        suggestion_json TEXT,
        replaced_wo_disposition_id INTEGER,
        FOREIGN KEY (mapped_record_id) REFERENCES mapped_cmms_record(mapped_record_id) ON UPDATE CASCADE ON DELETE RESTRICT,
        FOREIGN KEY (linked_mapped_record_id) REFERENCES mapped_cmms_record(mapped_record_id)
    )
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_wo_disposition_one_current ON wo_disposition(mapped_record_id) WHERE is_current = 1",
    "CREATE INDEX IF NOT EXISTS idx_wo_disposition_current ON wo_disposition(is_current, mapped_record_id)",
    "CREATE INDEX IF NOT EXISTS idx_wo_disposition_linked ON wo_disposition(linked_mapped_record_id) WHERE linked_mapped_record_id IS NOT NULL",
    # A population's records placed on one of its sub-assets.
    "CREATE INDEX IF NOT EXISTS idx_wo_disposition_current_sub_asset ON wo_disposition(sub_asset) WHERE is_current = 1",
    # The disposition helper's batch runs: each one is undone as a whole, and
    # every disposition it saved names it (wo_disposition.helper_run_id).
    """
    CREATE TABLE IF NOT EXISTS disposition_helper_run (
        helper_run_id INTEGER PRIMARY KEY,
        asset_number TEXT NOT NULL,
        min_confidence REAL NOT NULL,
        scope_json TEXT,
        considered INTEGER NOT NULL DEFAULT 0,
        applied INTEGER NOT NULL DEFAULT 0,
        skipped INTEGER NOT NULL DEFAULT 0,
        helper_version TEXT,
        run_by TEXT,
        run_at TEXT NOT NULL DEFAULT (datetime('now')),
        undone_at TEXT,
        undone_by TEXT,
        undone_count INTEGER
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_disposition_helper_run_asset ON disposition_helper_run(asset_number, helper_run_id)",
    # Whether any work order's text changed since the helper indexed it, without
    # reading every work order (the newest re-mapping, from this index).
    "CREATE INDEX IF NOT EXISTS idx_mapped_cmms_mapped_at ON mapped_cmms_record(mapped_at)",
    """
    CREATE TABLE IF NOT EXISTS population_weibull_analysis (
        population_weibull_analysis_id INTEGER PRIMARY KEY,
        population_definition TEXT NOT NULL,
        selection_json TEXT NOT NULL,
        window_start TEXT,
        window_end TEXT,
        cutoff_source TEXT,
        method_version TEXT NOT NULL,
        software_version TEXT,
        failure_count INTEGER,
        suspension_count INTEGER,
        beta REAL,
        eta REAL,
        result_json TEXT NOT NULL,
        run_by TEXT,
        run_at TEXT NOT NULL DEFAULT (datetime('now'))
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_population_weibull_definition ON population_weibull_analysis(population_definition, run_at)",
    # An editor's adjusted beta and eta for a saved run, kept beside the MLE rather
    # than over it, with who and why; the newest is the current one.
    """
    CREATE TABLE IF NOT EXISTS population_weibull_adjustment (
        population_weibull_adjustment_id INTEGER PRIMARY KEY,
        population_weibull_analysis_id INTEGER NOT NULL,
        adjusted_beta REAL NOT NULL,
        adjusted_eta REAL NOT NULL,
        adjustment_reason TEXT,
        adjusted_by TEXT,
        adjusted_at TEXT NOT NULL DEFAULT (datetime('now')),
        is_current INTEGER NOT NULL DEFAULT 1,
        FOREIGN KEY (population_weibull_analysis_id) REFERENCES population_weibull_analysis(population_weibull_analysis_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_population_weibull_adjustment ON population_weibull_adjustment(population_weibull_analysis_id, is_current)",
    # Sub-assets are found by their parent's asset id, which nothing indexed before.
    "CREATE INDEX IF NOT EXISTS idx_mapped_cmms_parent_asset ON mapped_cmms_record(immediate_parent_asset_id)",
    "CREATE INDEX IF NOT EXISTS idx_mapped_cmms_asset_id ON mapped_cmms_record(asset_id_raw)",
)


def ensure_standard_disposition_schema(conn: sqlite3.Connection) -> None:
    """Create the standard's tables, seed empty code lists, and bring migrated rows up to date."""

    for statement in _STATEMENTS:
        conn.execute(statement)
    _add_missing_columns(conn)
    for statement in _AFTER_ADDED_COLUMNS:
        conn.execute(statement)
    seed_code_lists(conn)
    sync_migrated_dispositions(conn)


# Columns added after a table first shipped. CREATE TABLE IF NOT EXISTS leaves an
# existing table as it is, so anything missing is ALTERed in.
_ADDED_COLUMNS = {
    "wo_disposition": {
        "record_class": "TEXT",
        # The part of the component (maintainable item) the work was on, if any.
        "part_code": "TEXT",
        # The disposition helper's batch run that saved this disposition, and
        # what the helper suggested when one was on screen for the save.
        "helper_run_id": "INTEGER",
        "suggestion_json": "TEXT",
        # The disposition a helper run's row took over from (none when the work
        # order had none): what undoing the run puts back.
        "replaced_wo_disposition_id": "INTEGER",
    },
    # A numbered report on a population analysis says which run it reports.
    "weibull_report_log": {"population_weibull_analysis_id": "INTEGER"},
}


# Indexes on columns an older database only has once _add_missing_columns has run.
_AFTER_ADDED_COLUMNS = (
    "CREATE INDEX IF NOT EXISTS idx_wo_disposition_helper_run ON wo_disposition(helper_run_id) WHERE helper_run_id IS NOT NULL",
)


def _add_missing_columns(conn: sqlite3.Connection) -> None:
    for table, columns in _ADDED_COLUMNS.items():
        present = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if not present:
            continue
        for column, ddl in columns.items():
            if column not in present:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def seed_code_lists(conn: sqlite3.Connection) -> int:
    """Write the starting codes into each list that has none.

    Only an empty list is seeded. Once a list holds anything it is the team's: a
    code they retired stays retired, and one they deleted outright is not put back
    on the next start.
    """

    seeded = 0
    for code_list, entries in std.SEED_CODES.items():
        if conn.execute("SELECT 1 FROM disposition_code WHERE code_list = ? LIMIT 1", (code_list,)).fetchone():
            continue
        conn.executemany(
            """
            INSERT INTO disposition_code(code_list, code, name, description, iso_ref, default_cause_code,
                renews_item, semantic, sort_order, is_active, updated_by)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 'GREMLIN')
            """,
            [
                (
                    entry.code_list, entry.code, entry.name, entry.description, entry.iso_ref,
                    entry.default_cause_code, int(entry.renews_item), entry.semantic, entry.sort_order,
                )
                for entry in entries
            ],
        )
        seeded += len(entries)
    return seeded


def load_codebook(conn: sqlite3.Connection) -> std.CodeBook:
    rows = conn.execute(
        """
        SELECT code_list, code, name, description, iso_ref, default_cause_code, renews_item, semantic,
               sort_order, is_active
        FROM disposition_code
        """
    ).fetchall()
    return std.CodeBook(
        std.CodeEntry(
            code_list=row[0],
            code=row[1],
            name=row[2],
            description=row[3],
            iso_ref=row[4],
            default_cause_code=row[5],
            renews_item=bool(row[6]),
            semantic=row[7],
            sort_order=int(row[8] or 0),
            is_active=bool(row[9]),
        )
        for row in rows
    )


def _natural_key(value: str) -> list[Any]:
    return [int(part) if part.isdigit() else part.lower() for part in re.split(r"(\d+)", value)]


# How deep the tree is walked before it is taken to be a loop in bad data.
_MAX_TREE_DEPTH = 50


def _id_text(value: Any) -> str | None:
    """An asset id as plain text: "4002", never "4002.0" or " 4002 "."""

    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    text = str(value).strip()
    if not text or text == "0":
        return None
    try:
        number = float(text)
    except ValueError:
        return text
    return str(int(number)) if number.is_integer() else text


# An asset's code: the number it is known by, at the start of its Asset Number
# or its name -- "4002" in "4002 Panel Finishing System", "4002-S09" in
# "4002-S09 Dry-off oven".
_ASSET_CODE = re.compile(r"^\s*(\d+(?:-[A-Za-z0-9]+)*)(?![\w-])")
# Where Limble's asset list is kept in GREMLIN.db (written by the Limble sync).
LIMBLE_ASSET_TABLE = "limble_asset"


def asset_code(text: Any) -> str | None:
    """The code at the start of an Asset Number or name, upper-cased, or None."""

    match = _ASSET_CODE.match(str(text or ""))
    return match.group(1).upper() if match else None


def _names_number(name: str, number: str) -> bool:
    """Whether a name is this Asset Number's own: led by its code, or naming it whole.

    "4002 Panel Finishing System" and "Asset 4002" are 4002's; "4002-S09 Dry-off
    oven" is not, though it starts with the same digits.
    """

    code = asset_code(name)
    if code is not None:
        return code == number.upper()
    return re.search(rf"(?<![\w-]){re.escape(number)}(?![\w-])", name, re.IGNORECASE) is not None


def parent_code(code: str | None) -> str | None:
    """The code one level up: "4002-S09" -> "4002", "4002-S09-01" -> "4002-S09".

    The plant numbers a sub-asset by its parent's number and a suffix. Two
    plain numbers of the same length are a range of machines ("3101-3107
    Salvagnini"), not a machine and its sub-asset, so they have no parent code.
    """

    if not code or "-" not in code:
        return None
    segments = code.split("-")
    if len(segments) == 2 and all(part.isdigit() for part in segments) and len(segments[0]) == len(segments[1]):
        return None
    return "-".join(segments[:-1])


class AssetTree:
    """The plant's assets, by GREMLIN asset number: which are sub-assets of which.

    Built the way the PM calendar builds its asset picker
    (``PmCalendarService._asset_hierarchy``, ``_parent_links`` and
    ``_with_descendants``): from Limble's asset list, each asset's parent is
    its ``parentAssetID`` -- never ``parentID`` -- when that parent is on the
    list; an asset's children are every asset that names it, walked down
    level by level, each level in name order. The list is the one the Limble
    sync keeps in GREMLIN.db (every asset), else the PM calendar's copy
    (only assets that lead to a PM).

    The one thing the PM calendar does not need is to say which of those
    Limble assets a GREMLIN Asset Number is: the calendar files everything by
    Limble id, while work orders here are filed under the plant's own numbers
    ("4002", "4002-S09"), which a Limble asset carries at the start of its
    name ("4002 Panel Finishing System"). So an Asset Number is the Limble
    asset its work orders carry as their own id, else the one whose name it
    starts, else the one its work orders' ids name. And a Limble asset no work
    order is filed under yet is offered by the number its name starts with.

    An Asset Number Limble's list does not have at all takes its parent from
    the plant's numbering (4002-S09 under 4002), else from the parent its work
    orders were imported with.
    """

    def __init__(
        self,
        parent: Mapping[str, str],
        names: Mapping[str, str],
        aliases: Mapping[str, str] | None = None,
        *,
        on_limble_list: Iterable[str] = (),
        source: str | None = None,
    ) -> None:
        self.parent = dict(parent)
        self.names = dict(names)
        # The Asset Numbers Limble's list has, and which list that was:
        # "gremlin" (the sync's copy), "pm_calendar", or None for neither.
        self.on_limble_list = set(on_limble_list)
        self.source = source
        # Another Asset Number some work orders are filed under for the same
        # asset (its bare Limble id beside the plant's number for it).
        self.aliases = dict(aliases or {})
        self.children: dict[str, list[str]] = {}
        for child, parent_number in self.parent.items():
            self.children.setdefault(parent_number, []).append(child)
        # Which tree this is, for what is kept on top of it (a population's
        # records): a number no other tree in this process has had.
        self.serial = next(_TREE_SERIALS)
        for kids in self.children.values():
            kids.sort(key=lambda number: ((self.names.get(number) or number).casefold(), number))

    @classmethod
    def load(
        cls,
        conn: sqlite3.Connection,
        hierarchy: Iterable[Mapping[str, Any]] | None = None,
        *,
        token: Any = None,
    ) -> "AssetTree":
        """The tree from Limble's asset list (or the PM calendar's), and the work orders.

        ``hierarchy`` is the PM calendar's asset rows: ``asset_id``,
        ``asset_name`` and ``parent_asset_id`` (None at the top).

        Building it reads every asset any work order is filed under, which on a
        plant's history is seconds of work, so a tree is kept for as long as what
        it was built from reads the same: the same work-order rows, the same
        asset lists. Any change to either builds it again.

        Even telling whether they read the same is a pass over every work order,
        so ``token`` -- GREMLIN.db's ``LifeDataService.change_token``, read before
        ``conn``'s transaction first queried -- answers it without one: while the
        database and the PM calendar's list are unchanged, the tree is too.
        """

        calendar = tuple(_row_key(row) for row in (hierarchy or ()))
        if token is not None:
            with _TREE_CACHE_LOCK:
                tree = _TREE_BY_TOKEN.get((token, calendar))
            if tree is not None:
                return tree
        stored = tuple(_row_key(row) for row in _stored_limble_assets(conn))
        wo_rows = tuple(
            tuple(row)
            for row in conn.execute(
                """
                SELECT TRIM(asset_number) AS asset_number, asset_id_raw, asset_name, immediate_parent_asset_id,
                       COUNT(*) AS record_count
                FROM mapped_cmms_record
                WHERE TRIM(asset_number) <> ''
                GROUP BY TRIM(asset_number), asset_id_raw, asset_name, immediate_parent_asset_id
                """
            )
        )
        key = (stored, calendar, wo_rows)
        with _TREE_CACHE_LOCK:
            tree = _TREE_CACHE.get(key)
        if tree is None:
            tree = cls._build(stored, calendar, wo_rows)
            with _TREE_CACHE_LOCK:
                _TREE_CACHE[key] = tree
                while len(_TREE_CACHE) > _TREE_CACHE_SIZE:
                    _TREE_CACHE.pop(next(iter(_TREE_CACHE)))
        if token is not None:
            with _TREE_CACHE_LOCK:
                _TREE_BY_TOKEN[(token, calendar)] = tree
                while len(_TREE_BY_TOKEN) > _TREE_CACHE_SIZE:
                    _TREE_BY_TOKEN.pop(next(iter(_TREE_BY_TOKEN)))
        return tree

    @classmethod
    def _build(
        cls,
        stored: tuple[tuple[Any, Any, Any], ...],
        calendar: tuple[tuple[Any, Any, Any], ...],
        wo_rows: tuple[tuple[Any, ...], ...],
    ) -> "AssetTree":
        # ---- Limble's hierarchy, as the PM calendar builds it: one lookup by
        # id, and each asset's parent only when the parent is on the list.
        by_id: dict[str, Mapping[str, Any]] = {}
        sources = []
        for source, rows in (
            ("gremlin", [{"asset_id": a, "asset_name": n, "parent_asset_id": p} for a, n, p in stored]),
            ("pm_calendar", [{"asset_id": a, "asset_name": n, "parent_asset_id": p} for a, n, p in calendar]),
        ):
            if rows:
                sources.append(source)
            for row in rows:
                asset_id = _id_text(row.get("asset_id"))
                if asset_id is not None and asset_id not in by_id:
                    by_id[asset_id] = row
        limble_parent: dict[str, str] = {}
        for asset_id, row in by_id.items():
            parent_id = _id_text(row.get("parent_asset_id"))
            if parent_id is not None and parent_id != asset_id and parent_id in by_id:
                limble_parent[asset_id] = parent_id

        def limble_name(asset_id: str) -> str:
            row = by_id.get(asset_id)
            return ((row.get("asset_name") if row else None) or "").strip()

        # The Limble asset each number names, for "4002" -> 4002 Panel Finishing
        # System: of several named with one number ("4002-S09 Dry-off oven",
        # "4002-S09 Burner"), the one not under another of them.
        named: dict[str, list[str]] = {}
        for asset_id in by_id:
            code = asset_code(limble_name(asset_id))
            if code:
                named.setdefault(code, []).append(asset_id)
        asset_named = {
            code: min(ids, key=lambda asset_id: (
                asset_code(limble_name(limble_parent.get(asset_id, ""))) == code, _natural_key(asset_id),
            ))
            for code, ids in named.items()
        }

        # ---- The work orders: the Asset Numbers they are filed under, the
        # names and Limble ids they carry, and the parent they were imported with.
        wo_numbers: set[str] = set()
        wo_names: dict[str, dict[str, int]] = {}
        ids_of: dict[str, dict[str, int]] = {}
        parent_votes: dict[str, dict[str, int]] = {}
        for number, asset_id_raw, asset_name, parent_raw, record_count in wo_rows:
            count = int(record_count or 0)
            wo_numbers.add(number)
            for tally, key in (
                (wo_names.setdefault(number, {}), (asset_name or "").strip()),
                (ids_of.setdefault(number, {}), _id_text(asset_id_raw)),
                (parent_votes.setdefault(number, {}), _id_text(parent_raw)),
            ):
                if key:
                    tally[key] = tally.get(key, 0) + count

        # ---- Which Limble asset each Asset Number is.
        def limble_asset_of(number: str) -> str | None:
            ids = ids_of.get(number, {})
            code = asset_code(number)
            if number in ids and number in by_id:
                return number  # its work orders carry it as their own Limble id
            if code == number.upper() and code in asset_named:
                return asset_named[code]  # the asset whose name it starts
            if number in by_id and not ids and asset_code(limble_name(number)) in (None, code):
                return number
            for asset_id in sorted(ids, key=lambda asset_id: -ids[asset_id]):
                if asset_id in by_id and asset_code(limble_name(asset_id)) in (None, code):
                    return asset_id
            return None

        number_of_id: dict[str, str] = {}
        aliases: dict[str, str] = {}
        # The plant's own numbers claim their Limble asset before a bare id does,
        # so 4002, not 13042, is the number the asset is offered by.
        for number in sorted(wo_numbers, key=lambda number: (number in ids_of.get(number, {}), _natural_key(number))):
            asset_id = limble_asset_of(number)
            if asset_id is None:
                continue
            if asset_id in number_of_id:
                aliases[number] = number_of_id[asset_id]
            else:
                number_of_id[asset_id] = number
        # A Limble asset no work order is filed under: by the number its name
        # starts with, as the plant files it, unless work orders here go by
        # Limble ids, or that number is taken.
        taken = {number.upper() for number in wo_numbers}
        by_ids = sum(1 for number in wo_numbers if number in ids_of.get(number, {}))
        for asset_id in sorted(by_id, key=_natural_key):
            if asset_id in number_of_id:
                continue
            code = asset_code(limble_name(asset_id))
            if code and asset_named.get(code) == asset_id and code not in taken and by_ids * 2 <= len(wo_numbers):
                number = code
            elif asset_id.upper() not in taken:
                number = asset_id
            elif code and code not in taken:
                number = code
            else:
                continue  # its id and its number are both other assets' Asset Numbers
            number_of_id[asset_id] = number
            taken.add(number.upper())

        # Without Limble's word: a bare id some work orders are filed under,
        # named for a number the plant files work orders under, is that asset.
        claimed = set(number_of_id.values())
        filed_upper = {other.upper(): other for other in wo_numbers}
        for number in sorted(wo_numbers):
            if number in aliases or number in claimed or number not in ids_of.get(number, {}):
                continue
            tally = wo_names.get(number, {})
            code = asset_code(max(tally, key=tally.get)) if tally else None
            if code and code != number.upper() and code in filed_upper:
                aliases[number] = filed_upper[code]

        # The Asset Number a Limble id stands for when Limble's list does not
        # have it: the number filed under that id itself, else the one whose
        # work orders carry it and the fewest other ids -- work orders filed
        # under 4002 can carry the id of the sub-asset they were on.
        def number_for_id(asset_id: str) -> str | None:
            if asset_id in number_of_id:
                return number_of_id[asset_id]
            if asset_id in wo_numbers:
                return aliases.get(asset_id, asset_id)
            carrying = [number for number in wo_numbers if asset_id in ids_of.get(number, {})]
            if not carrying:
                return None
            best = min(carrying, key=lambda number: (len(ids_of[number]), -ids_of[number][asset_id], _natural_key(number)))
            return aliases.get(best, best)

        # ---- Names: Limble's for an asset on its list; otherwise a work-order
        # name that is the number's own (a work order filed under 4002 can be
        # named for the sub-asset it was on, "4002-S09 Dry-off oven").
        names: dict[str, str] = {}
        for number in wo_numbers - set(aliases):
            own = {
                name: count for name, count in wo_names.get(number, {}).items()
                if asset_code(number) != number.upper() or _names_number(name, number)
            }
            if own:
                names[number] = max(own, key=lambda name: (own[name], name))
        for asset_id, number in number_of_id.items():
            if limble_name(asset_id):
                names[number] = limble_name(asset_id)

        # ---- Parents. Limble decides for every asset on its list, "top level"
        # included; the rest follow the plant's numbering, then their work orders.
        known = (wo_numbers | set(number_of_id.values())) - set(aliases)
        id_of_number = {number: asset_id for asset_id, number in number_of_id.items()}
        number_of_code = {number.upper(): number for number in known if asset_code(number) == number.upper()}
        for asset_id, number in number_of_id.items():
            code = asset_code(limble_name(asset_id))
            if code and asset_named.get(code) == asset_id:
                number_of_code.setdefault(code, number)

        parent: dict[str, str] = {}
        for number in known:
            if number in id_of_number:
                parent_id = limble_parent.get(id_of_number[number])
                candidate = number_of_id.get(parent_id) if parent_id else None
            else:
                above = parent_code(asset_code(number) if asset_code(number) == number.upper() else asset_code(names.get(number)))
                while above and above not in number_of_code:
                    above = parent_code(above)
                candidate = number_of_code[above] if above else None
                if candidate is None:
                    tally = parent_votes.get(number) or {}
                    parent_id = max(tally, key=tally.get) if tally else None
                    candidate = number_for_id(parent_id) if parent_id else None
            if candidate and candidate != number:
                parent[number] = candidate
        return cls(
            parent, names, aliases, on_limble_list=id_of_number, source=sources[0] if sources else None
        )

    def hierarchy_note(self, number: str) -> str:
        """Where an asset's sub-assets come from, in a line for under the dropdown."""

        number = self.aliases.get(number, number)
        if self.source is None:
            return (
                "No Limble asset list yet (the next Limble sync stores one), so these follow the Asset Numbers: "
                f"every {number}-… asset."
            )
        where = "Limble's asset list" if self.source == "gremlin" else "the PM calendar's copy of Limble's asset list"
        if number in self.on_limble_list:
            return f"Every asset under {self.label(number)}, by parentAssetID."
        return f"{where[0].upper()}{where[1:]} does not have {number}, so these follow the Asset Numbers: every {number}-… asset."

    def name(self, number: str | None) -> str:
        return self.names.get(number or "", "")

    def label(self, number: str | None) -> str:
        """How a page names an asset: "4002-S09 Dry-off oven", "4002-S14 Oven conveyor (5014)", or "4002"."""

        if not number:
            return ""
        name = self.name(number)
        if not name or name.casefold() == number.casefold():
            return number
        return name if _names_number(name, number) else f"{name} ({number})"

    def parent_of(self, number: str) -> str | None:
        return self.parent.get(self.aliases.get(number, number))

    def ancestors(self, number: str) -> list[str]:
        found: list[str] = []
        number = self.aliases.get(number, number)
        current = self.parent.get(number)
        while current and current not in found and current != number and len(found) < _MAX_TREE_DEPTH:
            found.append(current)
            current = self.parent.get(current)
        return found

    def descendants(self, number: str) -> list[dict[str, Any]]:
        """Every asset under ``number``, each after its parent, with how deep it sits."""

        found: list[dict[str, Any]] = []
        number = self.aliases.get(number, number)
        seen = {number}

        def walk(parent_number: str, depth: int) -> None:
            if depth > _MAX_TREE_DEPTH:
                return
            for child in self.children.get(parent_number, ()):
                if child in seen:
                    continue
                seen.add(child)
                found.append({
                    "asset_number": child,
                    "asset_name": self.name(child),
                    "label": self.label(child),
                    "parent": parent_number,
                    "depth": depth,
                })
                walk(child, depth + 1)

        walk(number, 1)
        return found


# Built trees, by what they were built from (see AssetTree.load), newest last.
_TREE_CACHE: dict[tuple, "AssetTree"] = {}
_TREE_CACHE_SIZE = 4
_TREE_CACHE_LOCK = threading.Lock()
# The same trees, by the database's change token and the PM calendar's rows.
_TREE_BY_TOKEN: dict[tuple, "AssetTree"] = {}
_TREE_SERIALS = itertools.count(1)


def _row_key(row: Mapping[str, Any]) -> tuple[Any, Any, Any]:
    return (row.get("asset_id"), row.get("asset_name"), row.get("parent_asset_id"))


def _stored_limble_assets(conn: sqlite3.Connection) -> list[Mapping[str, Any]]:
    """The asset list the Limble sync keeps in GREMLIN.db, or nothing before its first run."""

    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (LIMBLE_ASSET_TABLE,)
    ).fetchone()
    if not exists:
        return []
    return [
        {"asset_id": row[0], "asset_name": row[1], "parent_asset_id": row[2]}
        for row in conn.execute(f"SELECT asset_id, asset_name, parent_asset_id FROM {LIMBLE_ASSET_TABLE}")
    ]


def sub_asset_options(
    conn: sqlite3.Connection, asset_number: str, hierarchy: Iterable[Mapping[str, Any]] | None = None
) -> list[dict[str, Any]]:
    """An asset's sub-assets, every level down, as the editor offers them."""

    return AssetTree.load(conn, hierarchy).descendants(asset_number)


def parent_asset_numbers(
    conn: sqlite3.Connection, asset_numbers: Iterable[str], hierarchy: Iterable[Mapping[str, Any]] | None = None
) -> dict[str, str]:
    """Each asset's parent asset number, where the hierarchy names one."""

    tree = AssetTree.load(conn, hierarchy)
    return {number: tree.parent[number] for number in asset_numbers if number in tree.parent}


def renewed_items_from_json(text: Any) -> tuple[str, ...]:
    if not text:
        return ()
    try:
        values = json.loads(text)
    except (TypeError, ValueError):
        return ()
    return tuple(str(value) for value in values if value) if isinstance(values, list) else ()


def insert_wo_disposition(conn: sqlite3.Connection, mapped_record_id: int, values: dict[str, Any]) -> int:
    """Make ``values`` the record's current disposition, retiring the one before it."""

    conn.execute(
        "UPDATE wo_disposition SET is_current = 0 WHERE mapped_record_id = ? AND is_current = 1",
        (mapped_record_id,),
    )
    return int(
        conn.execute(
            """
            INSERT INTO wo_disposition(
                mapped_record_id, disposition_category, sub_asset, component_code, part_code, failure_mechanism_code,
                failure_mode_code, action_code, evidence, failure_cause_code, renewed_items_json, qty,
                linked_mapped_record_id, disposition_note, record_class, legacy_disposition_category,
                legacy_failure_mode, legacy_failure_mechanism, source, migrated_from_event_disposition_id,
                review_flag, decided_by, helper_run_id, suggestion_json, replaced_wo_disposition_id, is_current
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
            """,
            (
                mapped_record_id,
                values.get("disposition_category") or std.NOT_REVIEWED,
                values.get("sub_asset"),
                values.get("component_code"),
                values.get("part_code"),
                values.get("failure_mechanism_code"),
                values.get("failure_mode_code"),
                values.get("action_code"),
                values.get("evidence"),
                values.get("failure_cause_code"),
                json.dumps(list(values.get("renewed_items") or ())),
                values.get("qty"),
                values.get("linked_mapped_record_id"),
                values.get("disposition_note"),
                values.get("record_class"),
                values.get("legacy_disposition_category"),
                values.get("legacy_failure_mode"),
                values.get("legacy_failure_mechanism"),
                values.get("source") or "ENGINEER",
                values.get("migrated_from_event_disposition_id"),
                values.get("review_flag"),
                values.get("decided_by"),
                values.get("helper_run_id"),
                values.get("suggestion_json"),
                values.get("replaced_wo_disposition_id"),
            ),
        ).lastrowid
    )


def sync_migrated_dispositions(conn: sqlite3.Connection, mapped_record_ids: Iterable[int] | None = None) -> int:
    """Carry each REL disposition into the standard, until an engineer re-dispositions the record.

    A record with a current REL disposition and no standard one gets a migrated
    row (spec section 2.2): the category mapped, the REL failure mode and mechanism
    names kept as legacy text, and a review flag saying what to confirm. Coded
    fields are filled only when the legacy mechanism name is unambiguous. While the
    migrated row is still the current one, a later change on the old disposition
    screen is carried over too; once an engineer saves the record under the
    standard, the old screen no longer touches it.

    Returns how many records were (re)migrated.
    """

    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'event_disposition'").fetchone():
        return 0
    params: list[Any] = []
    record_filter = ""
    if mapped_record_ids is not None:
        ids = sorted({int(record_id) for record_id in mapped_record_ids})
        if not ids:
            return 0
        record_filter = f"AND d.mapped_record_id IN ({','.join('?' for _ in ids)})"
        params.extend(ids)
    rows = conn.execute(
        f"""
        SELECT d.event_disposition_id, d.mapped_record_id, d.disposition_category, d.include_in_weibull_candidate,
               COALESCE(NULLIF(TRIM(d.disposition_notes), ''), NULLIF(TRIM(d.disposition_text), '')) AS note,
               COALESCE(fm.failure_mode_name, rtfm.failure_mode_name) AS legacy_mode,
               COALESCE(fmech.failure_mechanism_name, rtfmech.failure_mechanism_name) AS legacy_mechanism,
               TRIM(m.asset_number) AS asset_number
        FROM event_disposition d
        JOIN mapped_cmms_record m ON m.mapped_record_id = d.mapped_record_id
        LEFT JOIN failure_mode fm ON fm.failure_mode_id = d.failure_mode_id
        LEFT JOIN failure_mechanism fmech ON fmech.failure_mechanism_id = d.failure_mechanism_id
        LEFT JOIN failure_mode rtfm ON rtfm.failure_mode_id = d.reset_target_failure_mode_id
        LEFT JOIN failure_mechanism rtfmech ON rtfmech.failure_mechanism_id = d.reset_target_failure_mechanism_id
        LEFT JOIN wo_disposition w ON w.mapped_record_id = d.mapped_record_id AND w.is_current = 1
        WHERE d.is_current = 1
          AND (
              w.wo_disposition_id IS NULL
              OR (w.source = 'MIGRATION' AND COALESCE(w.migrated_from_event_disposition_id, -1) <> d.event_disposition_id)
          )
          {record_filter}
        ORDER BY d.mapped_record_id
        """,
        params,
    ).fetchall()
    if not rows:
        return 0
    codebook = load_codebook(conn)
    children: dict[str, list[str]] = {}
    tree: AssetTree | None = None
    for row in rows:
        legacy_category = row[2]
        category = std.LEGACY_CATEGORY_MAP.get(legacy_category, std.NOT_REVIEWED)
        review = [std.LEGACY_REVIEW_NOTES.get(legacy_category, f"Migrated from {legacy_category}.")]
        if legacy_category.startswith("INCLUDED_") and not row[3]:
            review.append("It was left out of the old Weibull (include in Weibull candidate was off).")
        values: dict[str, Any] = {
            "disposition_category": category,
            "disposition_note": row[4],
            "legacy_disposition_category": legacy_category,
            "legacy_failure_mode": row[5],
            "legacy_failure_mechanism": row[6],
            "source": "MIGRATION",
            "migrated_from_event_disposition_id": row[0],
            "decided_by": "Migration",
        }
        asset_number = row[7] or ""
        legacy_mechanism = (row[6] or "").strip()
        if category != std.DUPLICATE_MISFILED and any(
            pattern.match(legacy_mechanism) for pattern, _ in std.LEGACY_AUTO_CONVERSIONS
        ):
            if tree is None:
                tree = AssetTree.load(conn)
            if asset_number not in children:
                children[asset_number] = [option["asset_number"] for option in tree.descendants(asset_number)]
            converted = std.legacy_auto_conversion(
                row[6], asset_number=asset_number, sub_asset_choices=children[asset_number], codebook=codebook,
                asset_names=tree.names,
            )
            if converted:
                values.update(converted)
                review.append("Sub-asset, component and mechanism were read off the legacy mechanism name.")
        if category in std.CODED_CATEGORIES:
            review.append("Complete the coded fields to bring it into the analysis.")
        values["review_flag"] = " ".join(review)
        insert_wo_disposition(conn, int(row[1]), values)
    return len(rows)
