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
from typing import Any, Iterable

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
    # Sub-assets are found by their parent's asset id, which nothing indexed before.
    "CREATE INDEX IF NOT EXISTS idx_mapped_cmms_parent_asset ON mapped_cmms_record(immediate_parent_asset_id)",
    "CREATE INDEX IF NOT EXISTS idx_mapped_cmms_asset_id ON mapped_cmms_record(asset_id_raw)",
)


def ensure_standard_disposition_schema(conn: sqlite3.Connection) -> None:
    """Create the standard's tables, seed empty code lists, and bring migrated rows up to date."""

    for statement in _STATEMENTS:
        conn.execute(statement)
    seed_code_lists(conn)
    sync_migrated_dispositions(conn)


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


def sub_asset_options(conn: sqlite3.Connection, asset_number: str) -> list[dict[str, str]]:
    """An asset's children in the asset hierarchy, as ``{asset_number, asset_name}``.

    Read from the hierarchy Limble attaches to every work order: a child is any
    asset whose records name this asset's id as their immediate parent. So a
    sub-asset appears here once it has a work order of its own in GREMLIN.
    """

    rows = conn.execute(
        """
        SELECT TRIM(c.asset_number) AS asset_number,
               COALESCE(NULLIF(TRIM(c.asset_name), ''), '') AS asset_name,
               COUNT(*) AS record_count
        FROM mapped_cmms_record c
        WHERE c.immediate_parent_asset_id IN (
                SELECT DISTINCT asset_id_raw FROM mapped_cmms_record
                WHERE asset_number = :asset AND asset_id_raw IS NOT NULL AND TRIM(asset_id_raw) <> ''
            )
          AND c.asset_number IS NOT NULL AND TRIM(c.asset_number) <> '' AND TRIM(c.asset_number) <> :asset
        GROUP BY TRIM(c.asset_number), COALESCE(NULLIF(TRIM(c.asset_name), ''), '')
        """,
        {"asset": asset_number},
    ).fetchall()
    best: dict[str, tuple[str, int]] = {}
    for row in rows:
        number, name, count = row[0], row[1], int(row[2] or 0)
        if number not in best or count > best[number][1]:
            best[number] = (name, count)
    return [
        {"asset_number": number, "asset_name": best[number][0]}
        for number in sorted(best, key=_natural_key)
    ]


def parent_asset_numbers(conn: sqlite3.Connection, asset_numbers: Iterable[str]) -> dict[str, str]:
    """Each asset's parent asset number, where the hierarchy and the records name one."""

    wanted = sorted({number for number in asset_numbers if number})
    if not wanted:
        return {}
    placeholders = ",".join("?" for _ in wanted)
    rows = conn.execute(
        f"""
        SELECT DISTINCT TRIM(child.asset_number) AS child_number, TRIM(parent.asset_number) AS parent_number
        FROM mapped_cmms_record child
        JOIN mapped_cmms_record parent ON parent.asset_id_raw = child.immediate_parent_asset_id
        WHERE TRIM(child.asset_number) IN ({placeholders})
          AND parent.asset_number IS NOT NULL AND TRIM(parent.asset_number) <> ''
          AND TRIM(parent.asset_number) <> TRIM(child.asset_number)
        """,
        wanted,
    ).fetchall()
    return {row[0]: row[1] for row in rows}


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
                linked_mapped_record_id, disposition_note, legacy_disposition_category, legacy_failure_mode,
                legacy_failure_mechanism, source, migrated_from_event_disposition_id, review_flag, decided_by,
                is_current
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
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
            if asset_number not in children:
                children[asset_number] = [option["asset_number"] for option in sub_asset_options(conn, asset_number)]
            converted = std.legacy_auto_conversion(
                row[6], asset_number=asset_number, sub_asset_choices=children[asset_number], codebook=codebook
            )
            if converted:
                values.update(converted)
                review.append("Sub-asset, component and mechanism were read off the legacy mechanism name.")
        if category in std.CODED_CATEGORIES:
            review.append("Complete the coded fields to bring it into the analysis.")
        values["review_flag"] = " ".join(review)
        insert_wo_disposition(conn, int(row[1]), values)
    return len(rows)
