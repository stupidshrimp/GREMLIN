"""Work orders dispositioned under the standard, and the analyses read from them.

The rules are in ``disposition_standard`` and the tables in
``standard_disposition_schema``; this is what puts them together against
GREMLIN.db: the code lists the team edits, the disposition editor's rows and
saves, the analysis population (spec section 7) with its triage views, and the
renewal-stream Weibull run.

It works through a ``LifeDataService`` rather than beside it: the same database
connection rules, the same plant time zone and schedule register for life hours,
and the same maximum-likelihood fit, intervals, Kaplan-Meier points and R², so a
fit here and a fit on Perform an Analysis are made the same way.
"""

from __future__ import annotations

import json
import math
import sqlite3
from datetime import date, datetime, timedelta, timezone, tzinfo
from typing import TYPE_CHECKING, Any, Iterable, Mapping

from services import disposition_standard as std
from services.standard_disposition_schema import (
    insert_wo_disposition,
    load_codebook,
    parent_asset_numbers,
    renewed_items_from_json,
    sub_asset_options,
)
from services.wo_narrative import NARRATIVE_KEYS

if TYPE_CHECKING:  # pragma: no cover - typing only
    from services.life_data_service import LifeDataService

# Stamped on every saved run, so a result can be traced to the rules that built it.
# Bump it whenever interval construction or the fit changes.
RENEWAL_METHOD_VERSION = "renewal-stream-v1"

DISPOSITION_PAGE_SIZE = 50
DISPOSITION_SCOPES = ("all", "not_reviewed", "migrated")
# The triage views' dimensions: what a Pareto bar or a matrix axis can be.
GROUP_BY_DIMENSIONS = {
    "asset": ("asset_number", None, "Asset"),
    "location": ("location", None, "Location"),
    "component": ("component_code", std.COMPONENT, "Component"),
    "failure_mode": ("failure_mode_code", std.FAILURE_MODE, "Failure mode"),
    "failure_mechanism": ("failure_mechanism_code", std.FAILURE_MECHANISM, "Failure mechanism"),
    "failure_cause": ("failure_cause_effective", std.FAILURE_CAUSE, "Failure cause"),
    "action": ("action_code", std.ACTION, "Action"),
    "category": ("disposition_category", None, "Disposition category"),
    "detection_method": ("detection_method", None, "Detection method"),
}
# The drill-down the Pareto steps through on a click (spec section 7.2).
DRILL_DOWN = ("location", "component", "failure_mechanism")
TREND_PERIODS = ("month", "quarter")
TREND_TOP_N = 5
RECORDS_LIMIT = 500
NOT_CODED = "Not coded"


def _clean(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


class StandardDispositionService:
    """The disposition standard's reads and writes, over one ``LifeDataService``."""

    def __init__(self, life: "LifeDataService") -> None:
        self.life = life

    # ------------------------------------------------------------------ #
    # Code lists
    # ------------------------------------------------------------------ #
    def codebook(self) -> std.CodeBook:
        with self.life.connect() as conn:
            return load_codebook(conn)

    @staticmethod
    def _entry_dict(entry: std.CodeEntry) -> dict[str, Any]:
        return {
            "code": entry.code,
            "name": entry.name,
            "description": entry.description,
            "iso_ref": entry.iso_ref,
            "default_cause_code": entry.default_cause_code,
            "renews_item": entry.renews_item,
            "semantic": entry.semantic,
            "sort_order": entry.sort_order,
            "is_active": entry.is_active,
        }

    def code_lists(self, *, include_inactive: bool = True) -> dict[str, Any]:
        """Every code list as configured, with what each list's codes can mean to the engine."""

        codebook = self.codebook()
        return {
            code_list: {
                "label": std.CODE_LIST_LABELS[code_list],
                "semantics": [
                    {"code": semantic, "label": std.SEMANTIC_LABELS[semantic]}
                    for semantic in std.CODE_LIST_SEMANTICS[code_list]
                ],
                "codes": [
                    self._entry_dict(entry) for entry in codebook.entries(code_list, include_inactive=include_inactive)
                ],
            }
            for code_list in std.CODE_LISTS
        }

    def save_code(self, values: Mapping[str, Any], *, updated_by: str | None = None) -> dict[str, Any]:
        """Add a code to a list, or change one already there, and return every list.

        A code's string is its key -- dispositions and saved populations store it --
        so an existing code keeps it; everything else about it can change. A code
        is retired (``is_active`` false) rather than deleted, so the dispositions
        that use it still read.
        """

        code_list = (_clean(values.get("code_list")) or "").upper()
        if code_list not in std.CODE_LISTS:
            raise ValueError(f"Choose one of the code lists: {', '.join(std.CODE_LISTS)}.")
        code = _clean(values.get("code"))
        if code is None or not std.CODE_PATTERN.match(code):
            raise ValueError(
                "A code is 1-32 letters, digits, '-', '_' or '.', starting with a letter or digit (e.g. CONT, MNT-INT)."
            )
        name = std.clean_text(values.get("name"))
        if name is None:
            raise ValueError("Give the code a name.")
        semantic = _clean(values.get("semantic"))
        if semantic is not None:
            semantic = semantic.upper()
            if semantic not in std.CODE_LIST_SEMANTICS[code_list]:
                allowed = ", ".join(std.CODE_LIST_SEMANTICS[code_list]) or "none"
                raise ValueError(f"A {std.CODE_LIST_LABELS[code_list].lower()} code can mean: {allowed}.")
        default_cause = _clean(values.get("default_cause_code"))
        renews_item = bool(values.get("renews_item")) if code_list == std.ACTION else False
        if code_list != std.FAILURE_MECHANISM:
            default_cause = None
        try:
            sort_order = int(values.get("sort_order") or 0)
        except (TypeError, ValueError):
            raise ValueError("Sort order must be a whole number.") from None
        is_active = values.get("is_active")
        is_active = True if is_active is None else bool(is_active)
        with self.life.write_connection() as conn:
            if default_cause is not None:
                cause = conn.execute(
                    "SELECT is_active FROM disposition_code WHERE code_list = ? AND code = ?",
                    (std.FAILURE_CAUSE, default_cause),
                ).fetchone()
                if cause is None:
                    raise ValueError(f"Default cause {default_cause!r} is not in the failure cause list.")
            existing = conn.execute(
                "SELECT disposition_code_id FROM disposition_code WHERE code_list = ? AND code = ?",
                (code_list, code),
            ).fetchone()
            fields = (
                name,
                std.clean_text(values.get("description")),
                std.clean_text(values.get("iso_ref")),
                default_cause,
                int(renews_item),
                semantic,
                sort_order,
                int(is_active),
                updated_by,
            )
            if existing:
                conn.execute(
                    """
                    UPDATE disposition_code
                    SET name = ?, description = ?, iso_ref = ?, default_cause_code = ?, renews_item = ?, semantic = ?,
                        sort_order = ?, is_active = ?, updated_by = ?, updated_at = datetime('now')
                    WHERE disposition_code_id = ?
                    """,
                    (*fields, int(existing[0])),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO disposition_code(name, description, iso_ref, default_cause_code, renews_item, semantic,
                        sort_order, is_active, updated_by, code_list, code)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (*fields, code_list, code),
                )
        return self.code_lists()

    # ------------------------------------------------------------------ #
    # Disposition editor
    # ------------------------------------------------------------------ #
    def editor_options(self, asset_number: str) -> dict[str, Any]:
        """What the editor offers for one asset: categories, active codes and its sub-assets."""

        with self.life.connect() as conn:
            codebook = load_codebook(conn)
            sub_assets = sub_asset_options(conn, asset_number)
        return {
            "asset_number": asset_number,
            "categories": [
                {"code": code, **{key: value for key, value in std.CATEGORY_GUIDE[code].items()}}
                for code in std.DISPOSITION_CATEGORIES
            ],
            "coded_categories": list(std.CODED_CATEGORIES),
            "renewing_categories": list(std.RENEWING_CATEGORIES),
            "evidence": [{"code": code, "label": std.EVIDENCE_LABELS[code]} for code in std.EVIDENCE_VALUES],
            "code_lists": {
                code_list: [self._entry_dict(entry) for entry in codebook.entries(code_list)]
                for code_list in std.CODE_LISTS
            },
            "sub_assets": sub_assets,
            "scopes": list(DISPOSITION_SCOPES),
        }

    _RECORD_COLUMNS = """
        m.mapped_record_id, m.task_id, m.task_name, m.request_title, m.requestor_description, m.completion_notes,
        m.area_affected, m.condition_found, m.cause, m.action_taken,
        m.created_date_final, m.start_date_final, m.completed_date_final,
        CASE WHEN m.downtime_hours IS NULL THEN NULL WHEN m.downtime_hours < 0 THEN 0 ELSE m.downtime_hours END AS downtime_hours,
        TRIM(m.asset_number) AS asset_number, m.asset_name,
        COALESCE(m.record_class_final, m.record_class_auto) AS record_class,
        w.wo_disposition_id, w.disposition_category, w.sub_asset, w.component_code, w.failure_mechanism_code,
        w.failure_mode_code, w.action_code, w.evidence, w.failure_cause_code, w.renewed_items_json, w.qty,
        w.item_detail, w.linked_mapped_record_id, w.disposition_note, w.legacy_disposition_category,
        w.legacy_failure_mode, w.legacy_failure_mechanism, w.source, w.review_flag, w.decided_by, w.decided_at,
        linked.task_id AS linked_task_id
    """
    _RECORD_FROM = """
        FROM mapped_cmms_record m
        LEFT JOIN wo_disposition w ON w.mapped_record_id = m.mapped_record_id AND w.is_current = 1
        LEFT JOIN mapped_cmms_record linked ON linked.mapped_record_id = w.linked_mapped_record_id
    """

    @staticmethod
    def _scope_clause(scope: str) -> str:
        if scope == "not_reviewed":
            return " AND (w.wo_disposition_id IS NULL OR w.disposition_category = 'NOT_REVIEWED')"
        if scope == "migrated":
            return " AND w.source = 'MIGRATION'"
        return ""

    def _disposition_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        if row["wo_disposition_id"] is None:
            return {
                "disposition_category": std.NOT_REVIEWED,
                "sub_asset": None,
                "component_code": None,
                "failure_mechanism_code": None,
                "failure_mode_code": None,
                "action_code": None,
                "evidence": None,
                "failure_cause_code": None,
                "renewed_items": [],
                "qty": None,
                "item_detail": None,
                "linked_mapped_record_id": None,
                "linked_task_id": None,
                "disposition_note": None,
                "legacy_disposition_category": None,
                "legacy_failure_mode": None,
                "legacy_failure_mechanism": None,
                "source": None,
                "review_flag": None,
                "decided_by": None,
                "decided_at": None,
            }
        return {
            "disposition_category": row["disposition_category"] or std.NOT_REVIEWED,
            "sub_asset": row["sub_asset"],
            "component_code": row["component_code"],
            "failure_mechanism_code": row["failure_mechanism_code"],
            "failure_mode_code": row["failure_mode_code"],
            "action_code": row["action_code"],
            "evidence": row["evidence"],
            "failure_cause_code": row["failure_cause_code"],
            "renewed_items": list(renewed_items_from_json(row["renewed_items_json"])),
            "qty": row["qty"],
            "item_detail": row["item_detail"],
            "linked_mapped_record_id": row["linked_mapped_record_id"],
            "linked_task_id": row["linked_task_id"],
            "disposition_note": row["disposition_note"],
            "legacy_disposition_category": row["legacy_disposition_category"],
            "legacy_failure_mode": row["legacy_failure_mode"],
            "legacy_failure_mechanism": row["legacy_failure_mechanism"],
            "source": row["source"],
            "review_flag": row["review_flag"],
            "decided_by": row["decided_by"],
            "decided_at": row["decided_at"],
        }

    def _record_dict(self, row: sqlite3.Row, codebook: std.CodeBook) -> dict[str, Any]:
        disposition = self._disposition_dict(row)
        return {
            "mapped_record_id": int(row["mapped_record_id"]),
            "task_id": row["task_id"],
            "task_name": row["task_name"],
            "request_title": row["request_title"],
            "requestor_description": row["requestor_description"],
            "completion_notes": row["completion_notes"],
            **{key: row[key] for key in NARRATIVE_KEYS},
            "created_date": row["created_date_final"],
            "completed_date": row["completed_date_final"],
            "downtime_hours": row["downtime_hours"],
            "asset_number": row["asset_number"],
            "asset_name": row["asset_name"],
            "record_class": row["record_class"],
            "disposition": disposition,
            "derived": std.derive_fields(
                disposition, asset_number=row["asset_number"] or "", record_class=row["record_class"], codebook=codebook
            ),
        }

    def disposition_page(
        self,
        asset_number: str,
        *,
        scope: str = "all",
        search: str | None = None,
        page: int = 0,
        page_size: int = DISPOSITION_PAGE_SIZE,
    ) -> dict[str, Any]:
        """One page of an asset's work orders with their standard dispositions, newest first."""

        if scope not in DISPOSITION_SCOPES:
            scope = "all"
        search_clause, search_params = self.life._disposition_search_clause(search)
        where = f"WHERE TRIM(m.asset_number) = ?{self._scope_clause(scope)}{search_clause}"
        params = [asset_number, *search_params]
        with self.life.read_transaction() as conn:
            codebook = load_codebook(conn)
            total = int(conn.execute(f"SELECT COUNT(*) {self._RECORD_FROM} {where}", params).fetchone()[0])
            counts = conn.execute(
                f"""
                SELECT COUNT(*) AS all_count,
                       SUM(CASE WHEN w.wo_disposition_id IS NULL OR w.disposition_category = 'NOT_REVIEWED' THEN 1 ELSE 0 END) AS not_reviewed,
                       SUM(CASE WHEN w.source = 'MIGRATION' THEN 1 ELSE 0 END) AS migrated
                {self._RECORD_FROM}
                WHERE TRIM(m.asset_number) = ?
                """,
                (asset_number,),
            ).fetchone()
            max_page = max(0, math.ceil(total / page_size) - 1) if total else 0
            page = min(max(0, int(page)), max_page)
            rows = conn.execute(
                f"""
                SELECT {self._RECORD_COLUMNS} {self._RECORD_FROM} {where}
                ORDER BY gremlin_sort_datetime(COALESCE(NULLIF(TRIM(m.created_date_final), ''), m.completed_date_final)) IS NULL,
                         gremlin_sort_datetime(COALESCE(NULLIF(TRIM(m.created_date_final), ''), m.completed_date_final)) DESC,
                         m.mapped_record_id DESC
                LIMIT ? OFFSET ?
                """,
                [*params, page_size, page * page_size],
            ).fetchall()
            records = [self._record_dict(row, codebook) for row in rows]
        return {
            "asset_number": asset_number,
            "scope": scope,
            "search": search or "",
            "rows": records,
            "page_index": page,
            "max_page_index": max_page,
            "page_size": page_size,
            "displayed_count": total,
            "counts": {
                "all": int(counts["all_count"] or 0),
                "not_reviewed": int(counts["not_reviewed"] or 0),
                "migrated": int(counts["migrated"] or 0),
            },
        }

    def disposition_record(self, mapped_record_id: int) -> dict[str, Any] | None:
        with self.life.read_transaction() as conn:
            codebook = load_codebook(conn)
            row = conn.execute(
                f"SELECT {self._RECORD_COLUMNS} {self._RECORD_FROM} WHERE m.mapped_record_id = ?",
                (int(mapped_record_id),),
            ).fetchone()
            return self._record_dict(row, codebook) if row else None

    def disposition_history(self, mapped_record_id: int) -> list[dict[str, Any]]:
        """Every disposition the record has had under the standard, newest first."""

        with self.life.connect() as conn:
            rows = conn.execute(
                """
                SELECT disposition_category, sub_asset, component_code, failure_mechanism_code, failure_mode_code,
                       action_code, evidence, failure_cause_code, renewed_items_json, qty, item_detail,
                       linked_mapped_record_id, disposition_note, source, review_flag, decided_by, decided_at, is_current
                FROM wo_disposition WHERE mapped_record_id = ?
                ORDER BY wo_disposition_id DESC
                """,
                (int(mapped_record_id),),
            ).fetchall()
        return [
            {**{key: row[key] for key in row.keys() if key != "renewed_items_json"},
             "renewed_items": list(renewed_items_from_json(row["renewed_items_json"]))}
            for row in rows
        ]

    def _resolve_linked(self, conn: sqlite3.Connection, value: Any, asset_number: str) -> int | None:
        """The record a duplicate links to, from a task ID as the editor types it."""

        text = _clean(value)
        if text is None:
            return None
        rows = conn.execute(
            "SELECT mapped_record_id, TRIM(asset_number) FROM mapped_cmms_record WHERE TRIM(CAST(task_id AS TEXT)) = ?",
            (text.lstrip("#"),),
        ).fetchall()
        if not rows:
            raise ValueError(f"No work order with task ID {text} is in GREMLIN.")
        if len(rows) > 1:
            same_asset = [row for row in rows if row[1] == asset_number]
            if len(same_asset) != 1:
                raise ValueError(f"Task ID {text} names more than one record; link it from the record itself.")
            rows = same_asset
        return int(rows[0][0])

    def save_dispositions(self, items: Iterable[Mapping[str, Any]], *, decided_by: str | None = None) -> int:
        """Save each item as its record's current disposition, all or none.

        Each item names its ``mapped_record_id`` and carries the fields of spec
        section 2; a duplicate's link can be given as ``linked_wo`` (a task ID) or
        ``linked_mapped_record_id``. Any refused item refuses the whole batch, with
        the record named in the message.
        """

        saved = 0
        with self.life.write_connection() as conn:
            codebook = load_codebook(conn)
            children: dict[str, tuple[str, ...]] = {}
            for item in items:
                try:
                    mapped_record_id = int(item.get("mapped_record_id"))
                except (TypeError, ValueError):
                    raise ValueError("Each disposition needs the mapped_record_id of its work order.") from None
                record = conn.execute(
                    f"SELECT {self._RECORD_COLUMNS} {self._RECORD_FROM} WHERE m.mapped_record_id = ?",
                    (mapped_record_id,),
                ).fetchone()
                if record is None:
                    raise ValueError(f"Record {mapped_record_id} was not found.")
                asset_number = record["asset_number"] or ""
                if not asset_number:
                    raise ValueError(f"Task {record['task_id']} has no asset number, so it cannot be dispositioned.")
                if asset_number not in children:
                    children[asset_number] = tuple(option["asset_number"] for option in sub_asset_options(conn, asset_number))
                values = dict(item)
                try:
                    if "linked_wo" in values and "linked_mapped_record_id" not in values:
                        values["linked_mapped_record_id"] = self._resolve_linked(conn, values.get("linked_wo"), asset_number)
                    previous = self._disposition_dict(record) if record["wo_disposition_id"] is not None else None
                    normalized = std.normalize_disposition(
                        values,
                        record=std.RecordContext(
                            mapped_record_id=mapped_record_id,
                            asset_number=asset_number,
                            sub_asset_choices=children[asset_number],
                            record_class=record["record_class"],
                        ),
                        codebook=codebook,
                        previous=previous,
                    )
                    linked = normalized.get("linked_mapped_record_id")
                    if linked is not None and not conn.execute(
                        "SELECT 1 FROM mapped_cmms_record WHERE mapped_record_id = ?", (linked,)
                    ).fetchone():
                        raise ValueError(f"Linked record {linked} was not found.")
                except ValueError as exc:
                    raise ValueError(f"Task {record['task_id'] or mapped_record_id}: {exc}") from None
                insert_wo_disposition(conn, mapped_record_id, {
                    **normalized,
                    # The legacy text describes the record's history, not one decision,
                    # so it travels with every disposition after the migrated one.
                    "legacy_disposition_category": record["legacy_disposition_category"],
                    "legacy_failure_mode": record["legacy_failure_mode"],
                    "legacy_failure_mechanism": record["legacy_failure_mechanism"],
                    "source": "ENGINEER",
                    "decided_by": decided_by,
                })
                saved += 1
        return saved

    # ------------------------------------------------------------------ #
    # Analysis population (spec section 7)
    # ------------------------------------------------------------------ #
    def _scope_rows(self, conn: sqlite3.Connection, population: std.Population) -> list[sqlite3.Row]:
        """Every record the population could reach: its assets, their sub-assets, and its locations.

        A record is in reach when the asset it was logged against, or the location
        it was dispositioned to, is one of them -- so selecting 4002 finds the work
        orders logged on 4002 itself, those logged on 4002-S09, and those logged on
        4002 and placed on S09. With no asset or location, every record is in reach.
        """

        reach = set(population.assets) | set(population.locations)
        for asset in population.assets:
            reach.update(option["asset_number"] for option in sub_asset_options(conn, asset))
        if not reach:
            return conn.execute(
                f"SELECT {self._RECORD_COLUMNS} {self._RECORD_FROM} "
                "WHERE m.asset_number IS NOT NULL AND TRIM(m.asset_number) <> ''"
            ).fetchall()
        placeholders = ",".join("?" for _ in reach)
        values = sorted(reach)
        return conn.execute(
            f"""
            SELECT {self._RECORD_COLUMNS} {self._RECORD_FROM}
            WHERE TRIM(m.asset_number) IN ({placeholders}) OR w.sub_asset IN ({placeholders})
            """,
            [*values, *values],
        ).fetchall()

    def _linked_completions(self, conn: sqlite3.Connection, record_ids: list[int]) -> dict[int, list[Any]]:
        """For each record, the completed dates of the duplicates that link to it."""

        completions: dict[int, list[Any]] = {}
        for start in range(0, len(record_ids), 500):
            chunk = record_ids[start:start + 500]
            placeholders = ",".join("?" for _ in chunk)
            for row in conn.execute(
                f"""
                SELECT w.linked_mapped_record_id, m.completed_date_final
                FROM wo_disposition w
                JOIN mapped_cmms_record m ON m.mapped_record_id = w.mapped_record_id
                WHERE w.is_current = 1 AND w.disposition_category = 'DUPLICATE_MISFILED'
                  AND w.linked_mapped_record_id IN ({placeholders})
                """,
                chunk,
            ):
                completions.setdefault(int(row[0]), []).append(row[1])
        return completions

    def _population_records(
        self, conn: sqlite3.Connection, population: std.Population, zone: tzinfo, codebook: std.CodeBook
    ) -> list[dict[str, Any]]:
        """The records in reach, each with its derived fields and the dates the analyses use."""

        rows = self._scope_rows(conn, population)
        linked = self._linked_completions(conn, [int(row["mapped_record_id"]) for row in rows])
        records = []
        for row in rows:
            record = self._record_dict(row, codebook)
            disposition = record["disposition"]
            derived = record["derived"]
            created = self.life._parse_event_datetime(row["created_date_final"], zone)
            started = self.life._parse_event_datetime(row["start_date_final"], zone)
            completed = self.life._parse_event_datetime(row["completed_date_final"], zone)
            event_date = created or started or completed
            renewal_date = completed
            links = linked.get(record["mapped_record_id"])
            if links is not None:
                # Diagnosed now, repaired later (spec section 6): the item is renewed
                # when the last repair work order linked to it is finished, and not
                # before -- an open one means the repair is not done.
                dates = [parsed for value in links if (parsed := self.life._parse_event_datetime(value, zone))]
                renewal_date = max(dates + ([completed] if completed else [])) if dates else None
            record.update({
                "category": disposition["disposition_category"],
                "location": derived["location"],
                "event_date": event_date,
                "renewal_date": renewal_date,
                "event_day": event_date.astimezone(zone).date() if event_date else None,
            })
            records.append(record)
        return records

    @staticmethod
    def _pattern_value(record: Mapping[str, Any], field: str) -> Any:
        """A record's value on one dimension, as the pattern views read it.

        A HELD record shows in the pattern views as unknown (spec section 2.1):
        reviewed, but with nothing it says that an analysis can rest on, so its
        coded fields read as not coded whatever was entered.
        """

        disposition = record["disposition"]
        if field == "asset_number":
            return record["asset_number"]
        if field == "location":
            return record["location"]
        if field == "disposition_category":
            return record["category"]
        if field in ("failure_cause_effective", "detection_method"):
            value = record["derived"][field]
        else:
            value = disposition.get(field)
        if record["category"] == std.HELD and field != "detection_method":
            return None
        return value

    def _pattern_records(
        self, records: list[dict[str, Any]], population: std.Population, start: date | None, end: date | None
    ) -> list[dict[str, Any]]:
        categories = population.categories or frozenset(std.PATTERN_VIEW_CATEGORIES)
        selected = []
        for record in records:
            if record["category"] not in categories:
                continue
            if start is not None and (record["event_day"] is None or record["event_day"] < start):
                continue
            if end is not None and (record["event_day"] is None or record["event_day"] > end):
                continue
            keep = True
            for attribute, field, _ in std.POPULATION_DIMENSIONS:
                chosen = getattr(population, attribute)
                if not chosen or attribute == "assets":
                    continue
                if self._pattern_value(record, field) not in chosen:
                    keep = False
                    break
            if keep:
                selected.append(record)
        return selected

    @staticmethod
    def _label(codebook: std.CodeBook, code_list: str | None, value: Any) -> str:
        if value is None:
            return NOT_CODED
        if code_list is not None:
            return f"{value} · {codebook.name(code_list, value)}" if codebook.name(code_list, value) != value else str(value)
        if value in std.CATEGORY_GUIDE:
            return std.CATEGORY_GUIDE[value]["label"]
        if value in std.DETECTION_LABELS:
            return std.DETECTION_LABELS[value]
        return str(value)

    def population_options(self, assets: Iterable[str]) -> dict[str, Any]:
        """The values each selector dimension takes among the records in reach of ``assets``."""

        population = std.Population(assets=frozenset(_clean(asset) for asset in assets if _clean(asset)))
        with self.life.read_transaction() as conn:
            codebook = load_codebook(conn)
            zone = self.life._plant_time_zone(conn)[0]
            records = self._population_records(conn, population, zone, codebook)
            sub_assets = {
                asset: sub_asset_options(conn, asset) for asset in sorted(population.assets)
            }
        dimensions: dict[str, list[dict[str, Any]]] = {}
        for attribute, field, _ in std.POPULATION_DIMENSIONS:
            code_list = next(
                (dimension[1] for dimension in GROUP_BY_DIMENSIONS.values() if dimension[0] == field), None
            )
            counts: dict[Any, int] = {}
            for record in records:
                if record["category"] in (std.NOT_REVIEWED, std.DUPLICATE_MISFILED):
                    continue
                value = self._pattern_value(record, field)
                if value is not None:
                    counts[value] = counts.get(value, 0) + 1
            values = set(counts)
            if attribute == "locations":
                values |= set(population.assets)
                for options in sub_assets.values():
                    values |= {option["asset_number"] for option in options}
            dimensions[attribute] = [
                {"value": value, "label": self._label(codebook, code_list, value), "count": counts.get(value, 0)}
                for value in sorted(values, key=lambda value: (-counts.get(value, 0), str(value)))
            ]
        return {
            "assets": sorted(population.assets),
            "dimensions": dimensions,
            "categories": [
                {"value": code, "label": std.CATEGORY_GUIDE[code]["label"], "pattern_default": code in std.PATTERN_VIEW_CATEGORIES}
                for code in std.DISPOSITION_CATEGORIES
                if code not in (std.NOT_REVIEWED, std.DUPLICATE_MISFILED)
            ],
            "group_by": [{"value": key, "label": value[2]} for key, value in GROUP_BY_DIMENSIONS.items()],
            "drill_down": list(DRILL_DOWN),
            "record_count": len(records),
        }

    def patterns(
        self,
        selection: Mapping[str, Any],
        *,
        start: date | None = None,
        end: date | None = None,
        group_by: str = "location",
        period: str = "month",
        top_n: int = TREND_TOP_N,
    ) -> dict[str, Any]:
        """The triage views for a population (spec section 7.2): Pareto, trend, matrix and the records."""

        if start is not None and end is not None and start > end:
            raise ValueError("The From date can't be later than the To date.")
        if group_by not in GROUP_BY_DIMENSIONS:
            raise ValueError(f"Group the Pareto by one of: {', '.join(GROUP_BY_DIMENSIONS)}.")
        if period not in TREND_PERIODS:
            raise ValueError("The trend period must be month or quarter.")
        top_n = max(1, min(int(top_n), 12))
        population = std.Population.from_mapping(selection)
        with self.life.read_transaction() as conn:
            codebook = load_codebook(conn)
            zone = self.life._plant_time_zone(conn)[0]
            records = self._population_records(conn, population, zone, codebook)
        selected = self._pattern_records(records, population, start, end)
        return {
            "selection": population.to_mapping(),
            "record_count": len(selected),
            "downtime_hours": round(sum(float(record["downtime_hours"] or 0) for record in selected), 4),
            "pareto": self._pareto(selected, group_by, codebook),
            "trend": self._trend(selected, period, top_n, codebook),
            "matrix": self._matrix(selected, codebook),
            "records": [self._record_summary(record, codebook) for record in sorted(
                selected, key=lambda record: (record["event_date"] is None, record["event_date"] or datetime.min.replace(tzinfo=timezone.utc)),
                reverse=True,
            )[:RECORDS_LIMIT]],
            "records_truncated": len(selected) > RECORDS_LIMIT,
        }

    def _pareto(self, records: list[dict[str, Any]], group_by: str, codebook: std.CodeBook) -> dict[str, Any]:
        field, code_list, label = GROUP_BY_DIMENSIONS[group_by]
        groups: dict[Any, dict[str, Any]] = {}
        for record in records:
            key = self._pattern_value(record, field)
            cause = self._pattern_value(record, "failure_cause_effective")
            downtime = float(record["downtime_hours"] or 0)
            group = groups.setdefault(key, {"count": 0, "downtime_hours": 0.0, "causes": {}})
            group["count"] += 1
            group["downtime_hours"] += downtime
            by_cause = group["causes"].setdefault(cause, {"count": 0, "downtime_hours": 0.0})
            by_cause["count"] += 1
            by_cause["downtime_hours"] += downtime
        rows = [
            {
                "key": key,
                "label": self._label(codebook, code_list, key),
                "count": group["count"],
                "downtime_hours": round(group["downtime_hours"], 4),
                "causes": [
                    {
                        "key": cause,
                        "label": self._label(codebook, std.FAILURE_CAUSE, cause),
                        "count": values["count"],
                        "downtime_hours": round(values["downtime_hours"], 4),
                    }
                    for cause, values in sorted(group["causes"].items(), key=lambda item: (-item[1]["count"], str(item[0])))
                ],
            }
            for key, group in groups.items()
        ]
        rows.sort(key=lambda row: (-row["count"], -row["downtime_hours"], str(row["label"])))
        next_level = None
        if group_by in DRILL_DOWN and DRILL_DOWN.index(group_by) + 1 < len(DRILL_DOWN):
            next_level = DRILL_DOWN[DRILL_DOWN.index(group_by) + 1]
        return {"group_by": group_by, "label": label, "next_group_by": next_level, "rows": rows}

    @staticmethod
    def _period_key(day: date, period: str) -> str:
        if period == "quarter":
            return f"{day.year}-Q{(day.month - 1) // 3 + 1}"
        return f"{day.year:04d}-{day.month:02d}"

    @classmethod
    def _period_range(cls, first: date, last: date, period: str) -> list[str]:
        keys = []
        year, month = first.year, first.month
        if period == "quarter":
            month = ((month - 1) // 3) * 3 + 1
        step = 3 if period == "quarter" else 1
        while (year, month) <= (last.year, last.month):
            key = cls._period_key(date(year, month, 1), period)
            if not keys or keys[-1] != key:
                keys.append(key)
            month += step
            if month > 12:
                year, month = year + 1, month - 12
        return keys

    def _trend(self, records: list[dict[str, Any]], period: str, top_n: int, codebook: std.CodeBook) -> dict[str, Any]:
        """Counts per period, stacked by the top components and the rest."""

        dated = [record for record in records if record["event_day"] is not None]
        if not dated:
            return {"period": period, "periods": [], "series": [], "undated": len(records)}
        totals: dict[Any, int] = {}
        for record in dated:
            component = self._pattern_value(record, "component_code")
            totals[component] = totals.get(component, 0) + 1
        ranked = sorted(totals, key=lambda key: (-totals[key], str(key)))
        top = ranked[:top_n]
        periods = self._period_range(min(r["event_day"] for r in dated), max(r["event_day"] for r in dated), period)
        index = {key: position for position, key in enumerate(periods)}
        series: dict[Any, list[int]] = {key: [0] * len(periods) for key in top}
        other = [0] * len(periods)
        for record in dated:
            component = self._pattern_value(record, "component_code")
            slot = index[self._period_key(record["event_day"], period)]
            if component in series:
                series[component][slot] += 1
            else:
                other[slot] += 1
        result = [
            {"key": key, "label": self._label(codebook, std.COMPONENT, key), "counts": series[key]}
            for key in top
        ]
        if any(other):
            result.append({"key": "__other__", "label": f"Other components ({len(ranked) - len(top)})", "counts": other})
        return {
            "period": period,
            "periods": periods,
            "series": result,
            "totals": [sum(values[i] for values in [s["counts"] for s in result]) for i in range(len(periods))],
            "undated": len(records) - len(dated),
        }

    def _matrix(self, records: list[dict[str, Any]], codebook: std.CodeBook) -> dict[str, Any]:
        """Event counts by location (rows) and component (columns)."""

        cells: dict[tuple[Any, Any], dict[str, float]] = {}
        locations: dict[Any, int] = {}
        components: dict[Any, int] = {}
        for record in records:
            location = record["location"]
            component = self._pattern_value(record, "component_code")
            cell = cells.setdefault((location, component), {"count": 0, "downtime_hours": 0.0})
            cell["count"] += 1
            cell["downtime_hours"] += float(record["downtime_hours"] or 0)
            locations[location] = locations.get(location, 0) + 1
            components[component] = components.get(component, 0) + 1
        location_keys = sorted(locations, key=lambda key: self.life._natural_key(str(key)))
        component_keys = sorted(components, key=lambda key: (-components[key], str(key)))
        return {
            "rows": [{"key": key, "label": str(key), "total": locations[key]} for key in location_keys],
            "columns": [
                {"key": key, "label": self._label(codebook, std.COMPONENT, key), "total": components[key]}
                for key in component_keys
            ],
            "cells": [
                [
                    {
                        "count": int(cells.get((row, column), {}).get("count", 0)),
                        "downtime_hours": round(cells.get((row, column), {}).get("downtime_hours", 0.0), 4),
                    }
                    for column in component_keys
                ]
                for row in location_keys
            ],
            "max_count": max((int(cell["count"]) for cell in cells.values()), default=0),
        }

    def _record_summary(self, record: Mapping[str, Any], codebook: std.CodeBook) -> dict[str, Any]:
        disposition = record["disposition"]
        return {
            "mapped_record_id": record["mapped_record_id"],
            "task_id": record["task_id"],
            "task_name": record["task_name"],
            "asset_number": record["asset_number"],
            "event_date": record["event_day"].isoformat() if record["event_day"] else None,
            "category": record["category"],
            "location": record["location"],
            "component": self._label(codebook, std.COMPONENT, disposition["component_code"]),
            "failure_mode": self._label(codebook, std.FAILURE_MODE, disposition["failure_mode_code"]),
            "failure_mechanism": self._label(codebook, std.FAILURE_MECHANISM, disposition["failure_mechanism_code"]),
            "failure_cause": self._label(codebook, std.FAILURE_CAUSE, record["derived"]["failure_cause_effective"]),
            "action": self._label(codebook, std.ACTION, disposition["action_code"]),
            "qty": disposition["qty"],
            "downtime_hours": record["downtime_hours"],
        }

    # ------------------------------------------------------------------ #
    # Renewal-stream Weibull (spec section 5)
    # ------------------------------------------------------------------ #
    def _stream_schedules(
        self, conn: sqlite3.Connection, records: list[dict[str, Any]], locations: Iterable[str]
    ) -> dict[str, dict[str, Any]]:
        """The schedule each location's life hours are counted on.

        A sub-asset runs when its parent runs, so a location with no entry of its own
        in the Weibull schedule register takes its parent's; with neither, the plant
        default (REL-WBL-DAT-003 §7).
        """

        locations = sorted(set(locations))
        parents = parent_asset_numbers(conn, locations)
        for record in records:
            sub_asset = record["disposition"]["sub_asset"]
            if sub_asset and record["asset_number"]:
                parents.setdefault(sub_asset, record["asset_number"])
        registered = {
            str(row[0]).strip(): int(row[1])
            for row in conn.execute("SELECT asset_number, schedule_class_id FROM asset_schedule_assignment")
        }
        classes = {
            int(row["schedule_class_id"]): row
            for row in conn.execute(
                "SELECT schedule_class_id, schedule_class_code, schedule_class_name, hours_per_day, exclude_weekends "
                "FROM asset_schedule_class"
            )
        }
        default_id = self.life._schedule_class_id(conn, "")
        schedules = {}
        for location in locations:
            class_id = registered.get(location) or registered.get(parents.get(location, "")) or default_id
            row = classes[class_id]
            schedules[location] = {
                "location": location,
                "schedule_code": row["schedule_class_code"],
                "schedule_name": row["schedule_class_name"],
                "hours_per_day": float(row["hours_per_day"]) if row["hours_per_day"] is not None else 24.0,
                "exclude_weekends": bool(row["exclude_weekends"]),
            }
        return schedules

    def _resolve_window(
        self,
        conn: sqlite3.Connection,
        events: list[std.StreamEvent],
        zone: tzinfo,
        analysis_start: date | None,
        analysis_cutoff: date | None,
    ) -> tuple[datetime | None, datetime, str]:
        """(start, cutoff, cutoff source), in UTC, by the rules Perform an Analysis uses.

        A cutoff date runs to the end of that plant day; without one, the last
        completed Limble import when that is no earlier than every event, else now.
        """

        now = datetime.now(timezone.utc).replace(microsecond=0)
        today = now.astimezone(zone).date()
        if analysis_start is not None and analysis_start > today:
            raise ValueError("The analysis start date can't be later than today.")
        if analysis_cutoff is not None:
            if analysis_cutoff > today:
                raise ValueError("The analysis cutoff date can't be later than today.")
            cutoff = min(now, self.life._plant_midnight(analysis_cutoff + timedelta(days=1), zone))
            source = "USER"
        else:
            latest = max(
                (when for event in events for when in (event.event_date, event.renewal_date) if when is not None),
                default=None,
            )
            last_import = self.life._last_completed_import_at(conn)
            if last_import is not None:
                last_import = last_import.replace(microsecond=0)
            if last_import is not None and last_import <= now and (latest is None or last_import >= latest):
                cutoff, source = last_import, "LAST_IMPORT"
            else:
                cutoff, source = now, "NOW"
        start = self.life._plant_midnight(analysis_start, zone) if analysis_start is not None else None
        if start is not None and start >= cutoff:
            raise ValueError("The analysis start date has to be before the cutoff.")
        return start, cutoff, source

    def _stream_event(self, record: Mapping[str, Any], codebook: std.CodeBook) -> std.StreamEvent:
        disposition = record["disposition"]
        mechanism = disposition["failure_mechanism_code"]
        cause = record["derived"]["failure_cause_effective"]
        return std.StreamEvent(
            record_id=record["mapped_record_id"],
            task_id=None if record["task_id"] is None else str(record["task_id"]),
            category=record["category"],
            location=record["location"],
            component=disposition["component_code"],
            mechanism=mechanism,
            mode=disposition["failure_mode_code"],
            cause=cause,
            action=disposition["action_code"],
            renews_item=record["derived"]["renews_item"],
            renewed_items=tuple(disposition["renewed_items"]),
            event_date=record["event_date"],
            renewal_date=record["renewal_date"],
            qty=disposition["qty"],
            mechanism_unknown=mechanism is None or codebook.has_semantic(std.FAILURE_MECHANISM, mechanism, std.SEMANTIC_UNKNOWN),
            cause_induced=codebook.has_semantic(std.FAILURE_CAUSE, cause, std.SEMANTIC_INDUCED),
        )

    def _fit(self, analysis: std.RenewalAnalysis | None, label: str) -> dict[str, Any] | None:
        """The 2P Weibull MLE of one run's lives, made exactly as Perform an Analysis makes it."""

        from services.life_data_service import MIN_WEIBULL_FAILURE_LIVES, WeibullFitError

        if analysis is None:
            return None
        data = sorted(analysis.fit_data())
        failures = sum(failed for _, failed in data)
        fit: dict[str, Any] = {
            "label": label,
            "failure_count": failures,
            "suspension_count": len(data) - failures,
            "life_count": len(data),
            "fitted": False,
            "reason": None,
            "min_failure_lives": MIN_WEIBULL_FAILURE_LIVES,
        }
        if failures < MIN_WEIBULL_FAILURE_LIVES:
            fit["reason"] = (
                f"Weibull analysis needs at least {MIN_WEIBULL_FAILURE_LIVES} lives that end in a failure, and this "
                f"population has {failures}. The intervals are listed so they can be checked."
            )
            return fit
        try:
            beta, eta, log_likelihood = self.life._fit_weibull_2p(data)
        except WeibullFitError as exc:
            fit["reason"] = str(exc)
            return fit
        beta_lo, beta_hi, eta_lo, eta_hi = self.life._weibull_confidence_intervals(data, beta, eta)
        km_points = self.life._kaplan_meier_points(data)
        r_squared = self.life._probability_plot_r_squared(km_points)
        threshold = self.life.r_squared_review_threshold(failures)
        fit.update({
            "fitted": True,
            "beta": beta,
            "eta": eta,
            "beta_lower_ci": beta_lo,
            "beta_upper_ci": beta_hi,
            "eta_lower_ci": eta_lo,
            "eta_upper_ci": eta_hi,
            "log_likelihood": log_likelihood,
            "mean_time_to_failure": eta * math.gamma(1 + 1 / beta),
            "b10_life": eta * (-math.log(0.90)) ** (1 / beta),
            "b50_life": eta * math.log(2) ** (1 / beta),
            "probability_plot_r_squared": r_squared,
            "probability_plot_r_squared_threshold": threshold,
            "probability_plot_review": bool(r_squared is not None and r_squared < threshold),
            "km_points": km_points,
            "curve_points": self.life._curve_points(beta, eta, max(t for t, _ in data)),
            "interpretation": self.life._weibull_interpretation_summary(
                beta, eta, eta * math.gamma(1 + 1 / beta), beta_lo, beta_hi, eta_lo, eta_hi, r_squared, failures
            ),
        })
        return fit

    @staticmethod
    def _iso(value: datetime | None) -> str | None:
        return value.isoformat() if value is not None else None

    def _intervals_payload(self, analysis: std.RenewalAnalysis, tasks: Mapping[int, Any]) -> list[dict[str, Any]]:
        return [
            {
                "location": interval.location,
                "component": interval.component,
                "start": self._iso(interval.start),
                "end": self._iso(interval.end),
                "end_type": interval.end_type,
                "note": interval.note,
                "start_record_id": interval.start_record_id,
                "start_task_id": tasks.get(interval.start_record_id),
                "end_record_id": interval.end_record_id,
                "end_task_id": tasks.get(interval.end_record_id),
                "life_hours": interval.life_hours,
                "raw_days": interval.raw_days,
            }
            for interval in sorted(analysis.intervals, key=lambda interval: (interval.stream, interval.start))
        ]

    def run_weibull(
        self,
        selection: Mapping[str, Any],
        *,
        analysis_start: date | None = None,
        analysis_cutoff: date | None = None,
        save: bool = False,
        run_by: str | None = None,
    ) -> dict[str, Any]:
        """Build a population's lives stream by stream, fit them, and (optionally) save the run.

        Reads every FAILURE and PM_RESET in reach; the population's location and
        component choose the streams that are pooled, and its mode, mechanism, cause
        and action choose which failures count (``build_renewal_intervals``). When a
        failure with an unknown mechanism could be the selected one, both readings
        are fitted. The category filter of the pattern views does not apply here.
        """

        population = std.Population.from_mapping(selection)
        connection = self.life.write_connection() if save else self.life.read_transaction()
        with connection as conn:
            codebook = load_codebook(conn)
            zone, zone_name, zone_warning = self.life._plant_time_zone(conn)
            records = self._population_records(conn, population, zone, codebook)
            events = []
            uncoded = 0
            unreviewed = 0
            for record in records:
                if record["category"] == std.NOT_REVIEWED:
                    unreviewed += 1
                    continue
                if record["category"] not in std.RENEWING_CATEGORIES:
                    continue
                event = self._stream_event(record, codebook)
                if not std.stream_touches(event):
                    uncoded += 1
                    continue
                events.append(event)
            start, cutoff, cutoff_source = self._resolve_window(conn, events, zone, analysis_start, analysis_cutoff)
            locations = {event.location for event in events}
            schedules = self._stream_schedules(conn, records, locations)

            def life_hours(stream: std.StreamKey, begin: datetime, end: datetime) -> float:
                schedule = schedules[stream[0]]
                return self.life._scheduled_life_hours(
                    begin, end, schedule["hours_per_day"], exclude_weekends=schedule["exclude_weekends"], tz=zone
                )[0]

            main = std.build_renewal_intervals(
                events, population, start=start, cutoff=cutoff, life_hours=life_hours,
                unknown_mechanism_as=std.UNKNOWN_AS_SUSPENSION,
            )
            sensitivity = None
            if main.unknown_decided:
                sensitivity = std.build_renewal_intervals(
                    events, population, start=start, cutoff=cutoff, life_hours=life_hours,
                    unknown_mechanism_as=std.UNKNOWN_AS_FAILURE,
                )
            window_start = (
                analysis_start
                if analysis_start is not None
                else min((event.event_date.astimezone(zone).date() for event in events if event.event_date), default=None)
            )
            window_end = analysis_cutoff if analysis_cutoff is not None else cutoff.astimezone(zone).date()
            definition = std.population_definition(population, window_start, window_end)
            tasks = {record["mapped_record_id"]: record["task_id"] for record in records}
            warnings = std.validity_warnings(
                main, population, sensitivity=sensitivity, codebook=codebook,
                uncoded_count=uncoded, unreviewed_count=unreviewed,
            )
            if zone_warning:
                warnings.append({"code": "TIME_ZONE", "level": "info", "title": "Days split in UTC", "message": zone_warning})
            stream_rows = []
            for location, component in main.streams:
                stream_intervals = [i for i in main.intervals if i.stream == (location, component)]
                stream_rows.append({
                    "location": location,
                    "component": component,
                    "component_name": codebook.name(std.COMPONENT, component),
                    "failures": sum(1 for i in stream_intervals if i.end_type == std.FAILURE_END),
                    "suspensions": sum(1 for i in stream_intervals if i.end_type == std.SUSPENSION_END),
                    "recurrent": (location, component) in main.recurrent_streams,
                    "schedule_name": schedules.get(location, {}).get("schedule_name"),
                })
            payload: dict[str, Any] = {
                "analysis_id": None,
                "population_definition": definition,
                "selection": population.to_mapping(),
                "method_version": RENEWAL_METHOD_VERSION,
                "software_version": self._software_version(),
                "window_start": self._iso(start),
                "window_end": self._iso(cutoff),
                "window_start_day": window_start.isoformat() if window_start else None,
                "window_end_day": window_end.isoformat(),
                "cutoff_source": cutoff_source,
                "time_zone": zone_name,
                "life_basis": "Schedule-adjusted elapsed hours on each location's Weibull schedule",
                "streams": stream_rows,
                "fit": self._fit(main, "Unknown mechanisms as suspensions" if sensitivity else "Fit"),
                "sensitivity_fit": self._fit(sensitivity, "Unknown mechanisms as failures"),
                "warnings": warnings,
                "intervals": self._intervals_payload(main, tasks),
                "sensitivity_intervals": self._intervals_payload(sensitivity, tasks) if sensitivity else [],
                "events": [
                    {
                        "location": note.location,
                        "component": note.component,
                        "record_id": note.record_id,
                        "task_id": tasks.get(note.record_id),
                        "when": self._iso(note.when),
                        "role": note.role,
                        "note": note.note,
                    }
                    for note in main.notes
                ],
                "run_by": run_by,
            }
            if save:
                fit = payload["fit"] or {}
                analysis_id = conn.execute(
                    """
                    INSERT INTO population_weibull_analysis(population_definition, selection_json, window_start,
                        window_end, cutoff_source, method_version, software_version, failure_count, suspension_count,
                        beta, eta, result_json, run_by)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        definition,
                        json.dumps(payload["selection"]),
                        payload["window_start"],
                        payload["window_end"],
                        cutoff_source,
                        RENEWAL_METHOD_VERSION,
                        payload["software_version"],
                        fit.get("failure_count"),
                        fit.get("suspension_count"),
                        fit.get("beta"),
                        fit.get("eta"),
                        "{}",
                        run_by,
                    ),
                ).lastrowid
                payload["analysis_id"] = int(analysis_id)
                payload["run_at"] = conn.execute(
                    "SELECT run_at FROM population_weibull_analysis WHERE population_weibull_analysis_id = ?",
                    (analysis_id,),
                ).fetchone()[0]
                conn.execute(
                    "UPDATE population_weibull_analysis SET result_json = ? WHERE population_weibull_analysis_id = ?",
                    (json.dumps(payload), analysis_id),
                )
        return payload

    @staticmethod
    def _software_version() -> str:
        from services.life_data_service import gremlin_code_version

        return gremlin_code_version()

    def saved_analyses(self, *, asset_number: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        """The most recent saved runs, newest first; only those that read ``asset_number`` when given."""

        with self.life.connect() as conn:
            rows = conn.execute(
                """
                SELECT population_weibull_analysis_id, population_definition, selection_json, window_start, window_end,
                       method_version, failure_count, suspension_count, beta, eta, run_by, run_at
                FROM population_weibull_analysis
                ORDER BY run_at DESC, population_weibull_analysis_id DESC
                """
            ).fetchall()
        saved = []
        for row in rows:
            try:
                selection = json.loads(row["selection_json"] or "{}")
            except ValueError:
                selection = {}
            if asset_number and asset_number not in (selection.get("assets") or []) and asset_number not in (
                selection.get("locations") or []
            ):
                continue
            saved.append({
                "analysis_id": int(row["population_weibull_analysis_id"]),
                "population_definition": row["population_definition"],
                "selection": selection,
                "method_version": row["method_version"],
                "method_current": row["method_version"] == RENEWAL_METHOD_VERSION,
                "failure_count": row["failure_count"],
                "suspension_count": row["suspension_count"],
                "beta": row["beta"],
                "eta": row["eta"],
                "run_by": row["run_by"],
                "run_at": row["run_at"],
            })
            if len(saved) >= limit:
                break
        return saved

    def load_saved_analysis(self, analysis_id: int) -> dict[str, Any] | None:
        with self.life.connect() as conn:
            row = conn.execute(
                "SELECT result_json, method_version FROM population_weibull_analysis WHERE population_weibull_analysis_id = ?",
                (int(analysis_id),),
            ).fetchone()
        if row is None:
            return None
        payload = json.loads(row["result_json"])
        payload["method_current"] = row["method_version"] == RENEWAL_METHOD_VERSION
        return payload
