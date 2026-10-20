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

import json
import re
import sqlite3
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
        failure_mechanism_code TEXT,
        failure_mode_code TEXT,
        action_code TEXT,
        evidence TEXT,
        failure_cause_code TEXT,
        renewed_items_json TEXT NOT NULL DEFAULT '[]',
        qty INTEGER,
        item_detail TEXT,
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
        FOREIGN KEY (mapped_record_id) REFERENCES mapped_cmms_record(mapped_record_id) ON UPDATE CASCADE ON DELETE RESTRICT,
        FOREIGN KEY (linked_mapped_record_id) REFERENCES mapped_cmms_record(mapped_record_id)
    )
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_wo_disposition_one_current ON wo_disposition(mapped_record_id) WHERE is_current = 1",
    "CREATE INDEX IF NOT EXISTS idx_wo_disposition_current ON wo_disposition(is_current, mapped_record_id)",
    "CREATE INDEX IF NOT EXISTS idx_wo_disposition_linked ON wo_disposition(linked_mapped_record_id) WHERE linked_mapped_record_id IS NOT NULL",
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
    seed_code_lists(conn)
    sync_migrated_dispositions(conn)


# Columns added after a table first shipped. CREATE TABLE IF NOT EXISTS leaves an
# existing table as it is, so anything missing is ALTERed in.
_ADDED_COLUMNS = {
    "wo_disposition": {"record_class": "TEXT"},
    # A numbered report on a population analysis says which run it reports.
    "weibull_report_log": {"population_weibull_analysis_id": "INTEGER"},
}


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

    Work orders are filed under the curated Asset Number the plant uses --
    "4002" for the Panel Finishing System, "4002-S09" for its dry-off oven --
    which a sub-asset takes from its parent with a suffix. Limble numbers the
    same assets by its own ids (13042, 4109), and the Limble sync files a new
    work order under the curated number its asset's history uses. So an
    asset's parent is read, in this order, from:

    1. **Its number.** "4002-S09" (or an asset named "4002-S09 ...") is a
       sub-asset of "4002", wherever "4002" is known. This is the plant's own
       rule and needs no hierarchy at all.
    2. **Limble's hierarchy**, by ``parentAssetID``: the asset list the Limble
       sync keeps in GREMLIN.db, then the PM calendar's copy. A Limble asset is
       the Asset Number the code in its name is, so "4002-S09 Dry-off oven" is
       4002-S09 -- never 4002, even where work orders on it were filed under
       4002 with its name. One no work order is filed under yet goes by its
       code too (by its id where work orders go by ids).
    3. **The work orders' own parent**, as they were imported. Last, because
       those imported before the sync stopped reading ``parentID`` as a
       parent can name another top-level machine.

    Every asset any of these knows is in the tree, so a sub-asset with no work
    orders of its own is still offered.
    """

    def __init__(
        self, parent: Mapping[str, str], names: Mapping[str, str], aliases: Mapping[str, str] | None = None
    ) -> None:
        self.parent = dict(parent)
        self.names = dict(names)
        # A Limble id some work orders are filed under, for the curated Asset
        # Number the rest of its work orders use: the same asset.
        self.aliases = dict(aliases or {})
        self.children: dict[str, list[str]] = {}
        for child, parent_number in self.parent.items():
            self.children.setdefault(parent_number, []).append(child)
        for kids in self.children.values():
            kids.sort(key=lambda number: _natural_key(self.names.get(number) or number))

    @classmethod
    def load(cls, conn: sqlite3.Connection, hierarchy: Iterable[Mapping[str, Any]] | None = None) -> "AssetTree":
        """The tree from the work orders, Limble's asset list and (when given) the PM calendar's.

        ``hierarchy`` is the PM calendar's asset rows: ``asset_id``,
        ``asset_name`` and ``parent_asset_id`` (None at the top).
        """

        # ---- The work orders: the Asset Numbers they are filed under, the
        # names and Limble ids they carry, and the parent they were imported with.
        wo_numbers: set[str] = set()
        wo_names: dict[str, dict[str, int]] = {}
        ids_of: dict[str, set[str]] = {}
        parent_votes: dict[str, dict[str, int]] = {}
        for row in conn.execute(
            """
            SELECT TRIM(asset_number) AS asset_number, asset_id_raw, asset_name, immediate_parent_asset_id,
                   COUNT(*) AS record_count
            FROM mapped_cmms_record
            WHERE asset_number IS NOT NULL AND TRIM(asset_number) <> ''
            GROUP BY TRIM(asset_number), asset_id_raw, asset_name, immediate_parent_asset_id
            """
        ):
            number = row["asset_number"]
            count = int(row["record_count"] or 0)
            wo_numbers.add(number)
            asset_id = _id_text(row["asset_id_raw"])
            if asset_id is not None:
                ids_of.setdefault(number, set()).add(asset_id)
            name = (row["asset_name"] or "").strip()
            if name:
                tally = wo_names.setdefault(number, {})
                tally[name] = tally.get(name, 0) + count
            parent_id = _id_text(row["immediate_parent_asset_id"])
            if parent_id is not None:
                tally = parent_votes.setdefault(number, {})
                tally[parent_id] = tally.get(parent_id, 0) + count

        # Limble's hierarchy: GREMLIN's own copy of the asset list first, then
        # whatever the PM calendar knows that it does not.
        limble_rows: dict[str, Mapping[str, Any]] = {}
        for rows in (_stored_limble_assets(conn), hierarchy or ()):
            for row in rows:
                asset_id = _id_text(row.get("asset_id"))
                if asset_id is not None and asset_id not in limble_rows:
                    limble_rows[asset_id] = row

        def limble_name(asset_id: str) -> str:
            row = limble_rows.get(asset_id)
            return ((row.get("asset_name") if row else None) or "").strip()

        # ---- Two kinds of Asset Number. One a work order carries as its own
        # Limble id (13042) names that Limble asset. Any other is the plant's
        # curated number (4002, 4002-S09), and is its own code.
        by_id = {number for number in wo_numbers if number in ids_of.get(number, ())}
        curated = wo_numbers - by_id
        names: dict[str, str] = {}
        code_of: dict[str, str] = {}
        for number in curated:
            code = asset_code(number)
            if code == number.upper():
                code_of[number] = code
            # A work order filed under 4002 can carry the name of the sub-asset
            # it was on ("4002-S09 Dry-off oven"); only a name that is this
            # number's own is its name.
            own = {
                name: count for name, count in wo_names.get(number, {}).items()
                if code_of.get(number) is None or _names_number(name, number)
            }
            if own:
                names[number] = max(own, key=lambda name: (own[name], name))
        for number in by_id:
            tally = wo_names.get(number, {})
            name = limble_name(number) or (max(tally, key=lambda name: (tally[name], name)) if tally else "")
            if name:
                names[number] = name
            code = asset_code(name)
            if code:
                code_of[number] = code
        curated_by_code = {code_of[number]: number for number in curated if number in code_of}

        # ---- Each Limble asset as an Asset Number.
        number_of_id: dict[str, str] = {number: number for number in by_id}
        # A curated number whose work orders carry a Limble id named with its
        # code is that Limble asset.
        for number in curated:
            for asset_id in ids_of.get(number, ()):
                if asset_id not in number_of_id and code_of.get(number) and asset_code(limble_name(asset_id)) == code_of[number]:
                    number_of_id[asset_id] = number
        # The rest by the code in their name. Several can carry one ("4002-S09
        # Dry-off oven" and "4002-S09 Burner"): the one not under another of
        # them in Limble is the asset that code numbers; the others are parts
        # of it, each by its own id.
        prefer_codes = len(curated) >= len(by_id)
        taken = {number.upper() for number in wo_numbers} | {number.upper() for number in number_of_id.values()}

        def under_same_code(asset_id: str, code: str) -> bool:
            parent_id = _id_text((limble_rows.get(asset_id) or {}).get("parent_asset_id"))
            return parent_id is not None and asset_code(limble_name(parent_id)) == code

        for asset_id in sorted(limble_rows, key=lambda asset_id: under_same_code(asset_id, asset_code(limble_name(asset_id)) or "")):
            if asset_id in number_of_id:
                continue
            code = asset_code(limble_name(asset_id))
            primary = code is not None and not under_same_code(asset_id, code)
            if primary and code in curated_by_code and curated_by_code[code] not in number_of_id.values():
                number = curated_by_code[code]
            elif primary and prefer_codes and code not in taken:
                number = code
            elif asset_id.upper() not in taken:
                number = asset_id
            elif code and code not in taken:
                number = code
            else:
                continue  # its id and its code are both other assets' numbers
            number_of_id[asset_id] = number
            taken.add(number.upper())
        in_limble: set[str] = set()
        for asset_id, number in number_of_id.items():
            name = limble_name(asset_id)
            if asset_id not in limble_rows:
                continue
            in_limble.add(number)
            # Limble's current name for it, over the one a work order was imported with.
            if name:
                names[number] = name
                code = asset_code(name)
                if code and number not in curated:
                    code_of.setdefault(number, code)

        limble_parent: dict[str, str] = {}
        for asset_id, row in limble_rows.items():
            parent_id = _id_text(row.get("parent_asset_id"))
            child, parent_number = number_of_id.get(asset_id), number_of_id.get(parent_id or "")
            if child and parent_number and child != parent_number:
                limble_parent[child] = parent_number

        # A bare Limble id some work orders are filed under, beside the curated
        # number the plant files its asset under (13042 beside 4002), is that
        # asset again, not a sub-asset of it.
        aliases = {
            number: curated_by_code[code_of[number]]
            for number in by_id
            if code_of.get(number) in curated_by_code and curated_by_code[code_of[number]] != number
        }
        for alias, number in aliases.items():
            if alias in limble_parent:
                limble_parent.setdefault(number, limble_parent.pop(alias))
            if alias in in_limble:
                in_limble.add(number)
        known = (wo_numbers | set(number_of_id.values())) - set(aliases)

        # ---- The asset each code numbers: the one numbered by it, else the
        # one that is not under another of them in Limble.
        sharing: dict[str, list[str]] = {}
        for number in known:
            if number in code_of:
                sharing.setdefault(code_of[number], []).append(number)
        number_of_code = {
            code: min(numbers, key=lambda number: (
                number.upper() != code,
                code_of.get(limble_parent.get(number, "")) == code,
                _natural_key(number),
            ))
            for code, numbers in sharing.items()
        }

        def imported_parent(number: str) -> str | None:
            # Limble's word on an asset it knows is final, "top level" included,
            # and a parent id is believed only as an asset it is known to be.
            tally = parent_votes.get(number)
            if not tally or number in in_limble:
                return None
            return number_of_id.get(max(tally, key=tally.get))

        parent: dict[str, str] = {}
        for number in known:
            code = code_of.get(number)
            owner = number_of_code.get(code) if code else None
            if owner is not None and owner != number:
                # Another asset carries its code: it is a part of that one,
                # unless Limble hangs it somewhere more precise.
                candidate = limble_parent.get(number) or owner
            else:
                # 1. Its number: the nearest code above it that is a known asset.
                above = parent_code(code)
                while above and above not in number_of_code:
                    above = parent_code(above)
                # 2. Limble's hierarchy, then 3. the work orders' imported parent.
                candidate = (
                    (number_of_code[above] if above else None)
                    or limble_parent.get(number)
                    or imported_parent(number)
                )
            candidate = aliases.get(candidate, candidate) if candidate else None
            if candidate and candidate != number:
                parent[number] = candidate
        return cls(parent, names, aliases)

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
                mapped_record_id, disposition_category, sub_asset, component_code, failure_mechanism_code,
                failure_mode_code, action_code, evidence, failure_cause_code, renewed_items_json, qty, item_detail,
                linked_mapped_record_id, disposition_note, record_class, legacy_disposition_category,
                legacy_failure_mode, legacy_failure_mechanism, source, migrated_from_event_disposition_id,
                review_flag, decided_by, is_current
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
            """,
            (
                mapped_record_id,
                values.get("disposition_category") or std.NOT_REVIEWED,
                values.get("sub_asset"),
                values.get("component_code"),
                values.get("failure_mechanism_code"),
                values.get("failure_mode_code"),
                values.get("action_code"),
                values.get("evidence"),
                values.get("failure_cause_code"),
                json.dumps(list(values.get("renewed_items") or ())),
                values.get("qty"),
                values.get("item_detail"),
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
