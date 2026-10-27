"""Work orders dispositioned under the standard: the code lists, the editor, and its workbook.

The rules are in ``disposition_standard`` and the tables in
``standard_disposition_schema``; this is what puts them together against
GREMLIN.db: the code lists the team edits, the Disposition page's rows and saves,
and the Excel workbook an asset's work orders can be dispositioned in offline.
What is read *from* the dispositions -- the population and every analysis on it --
is ``population_analysis_service``.

It works through a ``LifeDataService`` rather than beside it: the same database
connection rules, search and spreadsheet writer.
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Iterable, Mapping

from services import disposition_standard as std
from services.standard_disposition_schema import (
    AssetTree,
    insert_wo_disposition,
    load_codebook,
    renewed_items_from_json,
)
from services.wo_narrative import NARRATIVE_KEYS

if TYPE_CHECKING:  # pragma: no cover - typing only
    from services.life_data_service import LifeDataService

DISPOSITION_PAGE_SIZE = 50
DISPOSITION_SCOPES = ("all", "not_reviewed", "migrated")

_FOUND_SQL = "COALESCE(NULLIF(TRIM(m.created_date_final), ''), m.completed_date_final)"
_LOCATION_SQL = "COALESCE(NULLIF(TRIM(w.sub_asset), ''), TRIM(m.asset_number))"
# The Disposition list's columns, as it sorts and filters them: the value a
# column filter matches (the text its cell shows) and the order a sort puts the
# rows in. Blanks sort last whichever way. The sub-asset sorts by the name the
# cell shows, through gremlin_asset_label, which each query registers from the
# asset tree.
LIST_COLUMNS: dict[str, tuple[str, str]] = {
    "task_id": ("CAST(m.task_id AS TEXT)", "CAST(m.task_id AS INTEGER)"),
    "found": (
        f"CASE WHEN {_FOUND_SQL} GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]*' "
        f"THEN substr({_FOUND_SQL}, 1, 10) ELSE {_FOUND_SQL} END",
        f"gremlin_sort_datetime({_FOUND_SQL})",
    ),
    "title": (
        "COALESCE(NULLIF(m.task_name, ''), m.request_title)",
        "COALESCE(NULLIF(m.task_name, ''), m.request_title) COLLATE NOCASE",
    ),
    "category": ("COALESCE(w.disposition_category, 'NOT_REVIEWED')", "COALESCE(w.disposition_category, 'NOT_REVIEWED')"),
    "sub_asset": (_LOCATION_SQL, f"gremlin_asset_label({_LOCATION_SQL}) COLLATE NOCASE"),
    "component": ("w.component_code", "w.component_code COLLATE NOCASE"),
    "mechanism": ("w.failure_mechanism_code", "w.failure_mechanism_code COLLATE NOCASE"),
    "mode": ("w.failure_mode_code", "w.failure_mode_code COLLATE NOCASE"),
}
# The code list behind each coded column, for the names its filter lists.
LIST_COLUMN_CODE_LISTS = {
    "component": std.COMPONENT,
    "mechanism": std.FAILURE_MECHANISM,
    "mode": std.FAILURE_MODE,
}
LIST_DEFAULT_ORDER = (
    f"gremlin_sort_datetime({_FOUND_SQL}) IS NULL, gremlin_sort_datetime({_FOUND_SQL}) DESC, m.mapped_record_id DESC"
)
# Most values one column filter may name, and most a column's filter lists.
LIST_FILTER_LIMIT = 1000
LIST_VALUES_LIMIT = 1000

# The workbook an asset's work orders are dispositioned in offline. The record
# columns are there to read and are never written back; the disposition columns
# after them are the reader's, with a dropdown wherever the value comes from a list.
EXCEL_SHEET_NAME = "Dispositions"
EXCEL_RECORD_COLUMNS = (
    "mapped_record_id",
    "task_id",
    "task_name",
    "created_date",
    "completed_date",
    "downtime_hours",
    "request_title",
    "requestor_description",
    "completion_notes",
    *NARRATIVE_KEYS,
    "legacy_disposition",
    "review_flag",
)
EXCEL_DISPOSITION_COLUMNS = (
    "disposition_category",
    "record_class",
    "sub_asset",
    "component_code",
    "failure_mechanism_code",
    "failure_mode_code",
    "action_code",
    "evidence",
    "failure_cause_code",
    "renewed_items",
    "qty",
    "item_detail",
    "linked_wo",
    "disposition_note",
)
EXCEL_COLUMNS = EXCEL_RECORD_COLUMNS + EXCEL_DISPOSITION_COLUMNS
# The code list behind each coded column's dropdown.
EXCEL_CODE_COLUMNS = {
    "component_code": std.COMPONENT,
    "failure_mechanism_code": std.FAILURE_MECHANISM,
    "failure_mode_code": std.FAILURE_MODE,
    "action_code": std.ACTION,
    "failure_cause_code": std.FAILURE_CAUSE,
}


# The workbook the code lists are kept in offline (Developer > Code lists): one
# row per code. The columns the upload reads come first; how many work orders use
# a code is there to read, since an upload never deletes and that count is what
# says a code can be retired or deleted without leaving a disposition unreadable.
CODE_EXCEL_SHEET_NAME = "Code lists"
CODE_EXCEL_EDITABLE_COLUMNS = (
    "code_list",
    "code",
    "name",
    "description",
    "iso_ref",
    "default_cause_code",
    "renews_item",
    "semantic",
    "sort_order",
    "is_active",
)
CODE_EXCEL_COLUMNS = CODE_EXCEL_EDITABLE_COLUMNS + ("work_orders_using",)
# Empty rows drawn under the codes, highlighted like the rest, for adding new ones.
CODE_EXCEL_BLANK_ROWS = 25
# What a code holds besides its list and its string: what an upload compares to
# tell a changed code from one it left as it was.
_CODE_FIELDS = ("name", "description", "iso_ref", "default_cause_code", "renews_item", "semantic", "sort_order", "is_active")
# A refused upload lists this many refused rows, then counts the rest.
CODE_EXCEL_REFUSALS_LISTED = 25


def _clean(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _listed(items: list[str]) -> str:
    return items[0] if len(items) == 1 else f"{', '.join(items[:-1])} and {items[-1]}"


def _semantic_lists(semantic: str) -> str:
    """The lists a semantic is for, as a sentence ends: "components and failure causes"."""

    return _listed([
        f"{std.CODE_LIST_LABELS[code_list].lower()}s" for code_list in std.CODE_LISTS
        if semantic in std.CODE_LIST_SEMANTICS[code_list]
    ])


def _semantic_refusal(code_list: str, semantic: str) -> str:
    """Why a code of this list can't take this semantic, and what it can take instead."""

    known = any(semantic in allowed for allowed in std.CODE_LIST_SEMANTICS.values())
    said = f"Semantic {semantic} is for {_semantic_lists(semantic)} only." if known else f"Semantic {semantic} is not one GREMLIN knows."
    kind = std.CODE_LIST_LABELS[code_list].lower()
    article = "An" if kind[0] in "aeiou" else "A"
    allowed = std.CODE_LIST_SEMANTICS[code_list]
    if not allowed:
        return f"{said} {article} {kind} can mean nothing to the engine: leave semantic blank."
    return f"{said} {article} {kind} can mean {' or '.join(allowed)}, or leave semantic blank."


def _upload_refusal(problems: list[str]) -> str:
    """Every row an upload refused, so one fix-up covers them all."""

    if len(problems) == 1:
        return f"{problems[0]} Nothing was saved."
    lines = [f"{len(problems)} rows were refused, so nothing was saved:", *problems[:CODE_EXCEL_REFUSALS_LISTED]]
    if len(problems) > CODE_EXCEL_REFUSALS_LISTED:
        lines.append(f"…and {len(problems) - CODE_EXCEL_REFUSALS_LISTED} more.")
    return "\n".join(lines)


class StandardDispositionService:
    """The disposition standard's reads and writes, over one ``LifeDataService``."""

    def __init__(
        self,
        life: "LifeDataService",
        asset_hierarchy: Callable[[], Iterable[Mapping[str, Any]] | None] | None = None,
    ) -> None:
        """``asset_hierarchy`` hands back Limble's asset hierarchy as the PM calendar
        keeps it (``asset_id``, ``asset_name``, ``parent_asset_id``), or None when
        there is none; see ``AssetTree``. Without it the work orders' own record
        of their parents is all there is."""

        self.life = life
        self.asset_hierarchy = asset_hierarchy

    def asset_tree(self, conn: sqlite3.Connection, *, token: Any = None) -> AssetTree:
        """Who hangs under whom, by asset number, as ``conn`` reads the database.

        ``token`` is GREMLIN.db's change token read before ``conn``'s transaction
        first queried, if the caller has one; see ``AssetTree.load``. Outside a
        transaction every query is its own read of the latest database, so one
        read here, before the tree's own queries, is as good.
        """

        if token is None and not conn.in_transaction:
            token = self.life.change_token()
        hierarchy = None
        if self.asset_hierarchy is not None:
            try:
                hierarchy = self.asset_hierarchy()
            except Exception:  # noqa: BLE001 - the PM calendar's database is optional here
                hierarchy = None
        return AssetTree.load(conn, hierarchy, token=token)

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
                    {"code": semantic, "label": std.SEMANTIC_LABELS[semantic], "short": std.SEMANTIC_SHORT_LABELS[semantic]}
                    for semantic in std.CODE_LIST_SEMANTICS[code_list]
                ],
                "codes": [
                    self._entry_dict(entry) for entry in codebook.entries(code_list, include_inactive=include_inactive)
                ],
            }
            for code_list in std.CODE_LISTS
        }

    # The wo_disposition column each list's codes are stored in.
    _CODE_COLUMNS = {
        std.COMPONENT: "component_code",
        std.FAILURE_MECHANISM: "failure_mechanism_code",
        std.FAILURE_MODE: "failure_mode_code",
        std.ACTION: "action_code",
        std.FAILURE_CAUSE: "failure_cause_code",
    }
    # The Population field a saved analysis keeps each list's codes in.
    _SELECTION_KEYS = {
        std.COMPONENT: "components",
        std.FAILURE_MECHANISM: "failure_mechanisms",
        std.FAILURE_MODE: "failure_modes",
        std.ACTION: "actions",
        std.FAILURE_CAUSE: "failure_causes",
    }

    def code_uses(self, conn: sqlite3.Connection, code_list: str, code: str) -> list[str]:
        """Where a code is in use, in words, or [] when nothing uses it."""

        uses = []
        column = self._CODE_COLUMNS[code_list]
        records = int(conn.execute(
            f"SELECT COUNT(*) FROM wo_disposition WHERE is_current = 1 AND {column} = ?", (code,)
        ).fetchone()[0])
        if code_list == std.COMPONENT:
            records += sum(
                1 for (text,) in conn.execute(
                    "SELECT renewed_items_json FROM wo_disposition WHERE is_current = 1 AND component_code IS NOT ? "
                    "AND renewed_items_json LIKE ?",
                    (code, f'%"{code}"%'),
                )
                if code in renewed_items_from_json(text)
            )
        if records:
            uses.append("1 work order's current disposition" if records == 1 else f"{records} work orders' current dispositions")
        if code_list == std.FAILURE_CAUSE:
            mechanisms = [row[0] for row in conn.execute(
                "SELECT code FROM disposition_code WHERE code_list = ? AND default_cause_code = ? ORDER BY code",
                (std.FAILURE_MECHANISM, code),
            )]
            if mechanisms:
                uses.append(f"the default cause of mechanism{'s' if len(mechanisms) != 1 else ''} {', '.join(mechanisms)}")
        key = self._SELECTION_KEYS[code_list]
        saved = 0
        for (text,) in conn.execute("SELECT selection_json FROM population_weibull_analysis"):
            try:
                selection = json.loads(text or "{}")
            except ValueError:
                continue
            if code in (selection.get(key) or ()):
                saved += 1
        if saved:
            uses.append(f"{saved} saved Weibull run{'s' if saved != 1 else ''}")
        return uses

    def delete_code(self, code_list: str, code: str) -> dict[str, Any]:
        """Remove a code nothing uses, and return every list.

        A code a current disposition, a mechanism's default cause or a saved
        Weibull run still names is refused, with where it is used: deleting it
        would leave those reading a code no list has. Retire it instead, which
        keeps them reading true and takes it off the dropdowns.
        """

        code_list = (_clean(code_list) or "").upper()
        if code_list not in std.CODE_LISTS:
            raise ValueError(f"Choose one of the code lists: {', '.join(std.CODE_LISTS)}.")
        code = _clean(code)
        label = std.CODE_LIST_LABELS[code_list].lower()
        with self.life.write_connection() as conn:
            exists = conn.execute(
                "SELECT 1 FROM disposition_code WHERE code_list = ? AND code = ?", (code_list, code)
            ).fetchone()
            if code is None or exists is None:
                raise ValueError(f"{code!r} is not in the {label} list.")
            uses = self.code_uses(conn, code_list, code)
            if uses:
                raise ValueError(
                    f"{code} is still used by {' and '.join(uses)}. Change those first, or retire the code "
                    "(untick Active) to take it off the dropdowns while they keep reading it."
                )
            # An emptied list is seeded again on the next start (seed_code_lists), which
            # would bring back every starting code the team deleted from it.
            remaining = conn.execute("SELECT COUNT(*) FROM disposition_code WHERE code_list = ?", (code_list,)).fetchone()[0]
            if int(remaining) <= 1:
                raise ValueError(
                    f"{code} is the last code in the {label} list, and a list is never left empty. Add its "
                    "replacement first, or retire it (untick Active)."
                )
            conn.execute("DELETE FROM disposition_code WHERE code_list = ? AND code = ?", (code_list, code))
        return self.code_lists()

    @staticmethod
    def _code_fields(values: Mapping[str, Any]) -> dict[str, Any]:
        """One code's fields, checked and cleaned: everything but whether its default cause exists.

        Raises ValueError, in words for whoever typed the value. The Code lists
        dialog and the code-list workbook both come through here, so a code
        either can save, the other can too.
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
                raise ValueError(_semantic_refusal(code_list, semantic))
        default_cause = _clean(values.get("default_cause_code"))
        if code_list != std.FAILURE_MECHANISM:
            default_cause = None
        try:
            sort_order = int(values.get("sort_order") or 0)
        except (TypeError, ValueError):
            raise ValueError("Sort order must be a whole number.") from None
        is_active = values.get("is_active")
        return {
            "code_list": code_list,
            "code": code,
            "name": name,
            "description": std.clean_text(values.get("description")),
            "iso_ref": std.clean_text(values.get("iso_ref")),
            "default_cause_code": default_cause,
            "renews_item": bool(values.get("renews_item")) if code_list == std.ACTION else False,
            "semantic": semantic,
            "sort_order": sort_order,
            "is_active": True if is_active is None else bool(is_active),
        }

    @staticmethod
    def _write_code(conn: sqlite3.Connection, fields: Mapping[str, Any], updated_by: str | None) -> None:
        """Add a checked code to its list, or change the one already there."""

        existing = conn.execute(
            "SELECT disposition_code_id FROM disposition_code WHERE code_list = ? AND code = ?",
            (fields["code_list"], fields["code"]),
        ).fetchone()
        values = (
            fields["name"],
            fields["description"],
            fields["iso_ref"],
            fields["default_cause_code"],
            int(fields["renews_item"]),
            fields["semantic"],
            fields["sort_order"],
            int(fields["is_active"]),
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
                (*values, int(existing[0])),
            )
        else:
            conn.execute(
                """
                INSERT INTO disposition_code(name, description, iso_ref, default_cause_code, renews_item, semantic,
                    sort_order, is_active, updated_by, code_list, code)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (*values, fields["code_list"], fields["code"]),
            )

    def save_code(self, values: Mapping[str, Any], *, updated_by: str | None = None) -> dict[str, Any]:
        """Add a code to a list, or change one already there, and return every list.

        A code's string is its key -- dispositions and saved populations store it --
        so an existing code keeps it; everything else about it can change. A code
        is retired (``is_active`` false) rather than deleted, so the dispositions
        that use it still read.
        """

        fields = self._code_fields(values)
        with self.life.write_connection() as conn:
            default_cause = fields["default_cause_code"]
            if default_cause is not None:
                cause = conn.execute(
                    "SELECT 1 FROM disposition_code WHERE code_list = ? AND code = ?",
                    (std.FAILURE_CAUSE, default_cause),
                ).fetchone()
                if cause is None:
                    raise ValueError(f"Default cause {default_cause!r} is not in the failure cause list.")
            self._write_code(conn, fields, updated_by)
        return self.code_lists()

    def _code_use_counts(self, conn: sqlite3.Connection) -> dict[tuple[str, str], int]:
        """How many work orders' current dispositions use each code, counted as ``code_uses`` counts one."""

        counts: dict[tuple[str, str], int] = {}
        for code_list, column in self._CODE_COLUMNS.items():
            for code, count in conn.execute(
                f"SELECT {column}, COUNT(*) FROM wo_disposition WHERE is_current = 1 AND {column} IS NOT NULL GROUP BY {column}"
            ):
                counts[(code_list, code)] = int(count)
        # A component renewed alongside another is used by that work order too.
        for component, text in conn.execute(
            "SELECT component_code, renewed_items_json FROM wo_disposition WHERE is_current = 1 AND renewed_items_json LIKE '%\"%'"
        ):
            for code in set(renewed_items_from_json(text)) - {component}:
                counts[(std.COMPONENT, code)] = counts.get((std.COMPONENT, code), 0) + 1
        return counts

    @staticmethod
    def _lookup_rows(lists: list[tuple[str, list[str], list[str]]]) -> list[list[Any]]:
        """A workbook's 'Lookup Lists' sheet: each list's codes in one column and their names beside them."""

        depth = max((len(codes) for _, codes, _ in lists), default=0)
        rows: list[list[Any]] = [[header for column, _, _ in lists for header in (column, f"{column} name")]]
        for index in range(depth):
            rows.append([
                value
                for _, codes, names in lists
                for value in ((codes[index], names[index]) if index < len(codes) else ("", ""))
            ])
        return rows

    def _lookup_range(self, position: int, codes: list[str]) -> str:
        """The cells of the ``position``-th list on the 'Lookup Lists' sheet, for a dropdown."""

        letter = self.life._xlsx_column_name(position * 2 + 1)
        return f"'Lookup Lists'!${letter}$2:${letter}${max(len(codes), 1) + 1}"

    def export_code_lists_excel(self, output_path: str | Path) -> int:
        """Write every code list, retired codes too, to a workbook to edit offline, and return how many codes.

        One row per code, list by list in the order the dropdowns show them. The
        columns an upload reads are highlighted, and so are empty rows under the
        codes, where new ones are typed; the list, meaning, yes/no and default
        cause columns have dropdowns. The default cause's dropdown suggests
        rather than insists, so a mechanism can name a cause added further down
        the same sheet.
        """

        from services.life_data_service import COLUMN_TYPE_BOOLEAN, COLUMN_TYPE_NUMBER, COLUMN_TYPE_TEXT, ExcelValidation

        codebook = self.codebook()
        with self.life.connect() as conn:
            uses = self._code_use_counts(conn)
        sheet_rows: list[list[Any]] = [list(CODE_EXCEL_COLUMNS)]
        for code_list in std.CODE_LISTS:
            for entry in codebook.entries(code_list, include_inactive=True):
                values = {**self._entry_dict(entry), "code_list": code_list, "work_orders_using": uses.get((code_list, entry.code), 0)}
                # Only an action renews items; on any other list the column is left empty rather than FALSE.
                if code_list != std.ACTION:
                    values["renews_item"] = None
                sheet_rows.append([values[column] for column in CODE_EXCEL_COLUMNS])
        written = len(sheet_rows) - 1
        sheet_rows.extend([None] * len(CODE_EXCEL_COLUMNS) for _ in range(CODE_EXCEL_BLANK_ROWS))

        semantics = list(dict.fromkeys(semantic for code_list in std.CODE_LISTS for semantic in std.CODE_LIST_SEMANTICS[code_list]))
        causes = codebook.entries(std.FAILURE_CAUSE, include_inactive=True)
        lists: list[tuple[str, list[str], list[str]]] = [
            ("code_list", list(std.CODE_LISTS), [std.CODE_LIST_LABELS[code_list] for code_list in std.CODE_LISTS]),
            # One dropdown serves every row, so each meaning says which lists take it.
            ("semantic", semantics,
             [f"{std.SEMANTIC_LABELS[semantic]} — {_semantic_lists(semantic)} only" for semantic in semantics]),
            ("default_cause_code", [entry.code for entry in causes], [entry.name for entry in causes]),
            ("yes_no", ["TRUE", "FALSE"], ["Yes", "No"]),
        ]
        lookup_rows = self._lookup_rows(lists)
        source = self._lookup_range
        validations = [
            ExcelValidation(
                column_name="code_list", validation_type="list", formula1=source(0, lists[0][1]),
                error=f"Choose the code's list: {', '.join(std.CODE_LISTS)}.",
            ),
            ExcelValidation(
                column_name="code", validation_type="textLength", operator="lessThanOrEqual", formula1="32",
                error="A code is 1-32 letters, digits, '-', '_' or '.', starting with a letter or digit.",
            ),
            ExcelValidation(
                column_name="semantic", validation_type="list", formula1=source(1, lists[1][1]),
                error="Choose what the code means to the engine from the list, or leave it blank.",
            ),
            ExcelValidation(
                column_name="default_cause_code", validation_type="list", formula1=source(2, lists[2][1]),
                show_error=False,
            ),
            ExcelValidation(
                column_name="sort_order", validation_type="whole", operator="greaterThanOrEqual", formula1="0",
                error="Sort order is a whole number; the dropdowns list a code list from the lowest up.",
            ),
        ]
        for column in ("renews_item", "is_active"):
            validations.append(ExcelValidation(
                column_name=column, validation_type="list", formula1=source(3, lists[3][1]),
                error="Choose TRUE or FALSE.",
            ))
        column_types = {column: COLUMN_TYPE_TEXT for column in CODE_EXCEL_COLUMNS}
        column_types.update({
            "renews_item": COLUMN_TYPE_BOOLEAN,
            "is_active": COLUMN_TYPE_BOOLEAN,
            "sort_order": COLUMN_TYPE_NUMBER,
            "work_orders_using": COLUMN_TYPE_NUMBER,
        })
        self.life._write_xlsx(
            output_path,
            sheet_rows,
            CODE_EXCEL_SHEET_NAME,
            validations=validations,
            lookup_rows=lookup_rows,
            column_types=column_types,
            editable_columns=frozenset(CODE_EXCEL_EDITABLE_COLUMNS),
        )
        return written

    @staticmethod
    def _excel_flag(value: Any) -> bool | None:
        """A yes/no cell: None when empty, and ValueError when it is neither."""

        if value is None or value == "":
            return None
        if isinstance(value, bool):
            return value
        text = str(value).strip().lower()
        if text in ("true", "yes", "y", "1", "1.0"):
            return True
        if text in ("false", "no", "n", "0", "0.0"):
            return False
        raise ValueError(f"{value!r} is not TRUE or FALSE.")

    def import_code_lists_excel(self, input_path: str | Path, *, updated_by: str | None = None) -> dict[str, Any]:
        """Add and change codes from a filled-in code-list workbook, all or none.

        Each row is one code, found by its list and its code. A row that reads as
        the code already does is left alone; every other row adds a code or
        changes one through the Code lists dialog's own checks, and one refused
        row refuses the upload, named by its sheet row. A code's string is its
        key, so retyping one adds a new code rather than renaming the old.

        Codes the workbook leaves out are kept: an upload adds and changes, and
        never deletes, so a sheet trimmed to the few codes being worked on cannot
        take the rest with it. A code is retired with is_active FALSE.
        """

        rows = self.life._read_xlsx(input_path)
        if not rows:
            raise ValueError("The workbook is empty. Download the code lists and fill that in.")
        headers = [str(value).strip().lower() if value is not None else "" for value in rows[0]]
        missing = [column for column in ("code_list", "code", "name") if column not in headers]
        if missing:
            raise ValueError(
                f"The workbook needs the {', '.join(missing)} column{'s' if len(missing) != 1 else ''} "
                "from the downloaded code lists."
            )
        present = [column for column in CODE_EXCEL_EDITABLE_COLUMNS if column in headers and column not in ("code_list", "code")]

        def list_key(text: str) -> str:
            return re.sub(r"[\s_-]+", " ", text).strip().lower()

        # The key or the label, in any case: FAILURE_MECHANISM and "Failure mechanism" are the same list.
        lists_by_key = {list_key(code_list): code_list for code_list in std.CODE_LISTS}
        codebook = self.codebook()
        existing = {
            (entry.code_list, entry.code): entry
            for code_list in std.CODE_LISTS
            for entry in codebook.entries(code_list, include_inactive=True)
        }
        spelled = {(code_list, code.casefold()): code for code_list, code in existing}
        seen: dict[tuple[str, str], int] = {}
        uploaded: list[tuple[int, str, dict[str, Any], bool]] = []
        # Every row is checked before any is refused, so one upload names them all.
        problems: list[tuple[int, str]] = []
        # The failure causes the sheet names, refused rows too: a mechanism whose
        # default cause is on a refused row is refused for that row, not again here.
        # A cause spelled in other capitals than the saved one is not one, though:
        # it can never be saved so, and a mechanism naming it so is refused too.
        causes = {code for code_list, code in existing if code_list == std.FAILURE_CAUSE}
        for row_number, values in enumerate(rows[1:], start=2):
            data = {header: values[index] if index < len(values) else None for index, header in enumerate(headers) if header}
            cells = {column: self._excel_cell(data.get(column)) for column in ("code_list", "code", *present)}
            if not any(cells.values()):
                continue
            list_text, code = cells["code_list"], cells["code"]
            if list_text is None or code is None:
                problems.append((row_number, f"Row {row_number}: give the code's list and the code itself."))
                continue
            code_list = lists_by_key.get(list_key(list_text))
            if code_list is None:
                problems.append((row_number, f"Row {row_number}: {list_text!r} is not a code list. Use one of {', '.join(std.CODE_LISTS)}."))
                continue
            label = std.CODE_LIST_LABELS[code_list]
            where = f"Row {row_number} ({label} {code})"
            folded = (code_list, code.casefold())
            if folded in seen:
                problems.append((row_number, f"{where}: the code is in the workbook twice, also on row {seen[folded]}."))
                continue
            seen[folded] = row_number
            stored = spelled.get(folded)
            if stored is not None and stored != code:
                problems.append((
                    row_number,
                    f"{where}: the {label.lower()} list already has {stored}, which differs only in capitals. "
                    f"Write it as {stored} to change that code; a code's spelling is its key.",
                ))
                continue
            if code_list == std.FAILURE_CAUSE:
                causes.add(code)
            before = existing.get((code_list, code))
            item: dict[str, Any] = (
                self._entry_dict(before) if before is not None else {
                    "description": None, "iso_ref": None, "default_cause_code": None, "renews_item": False,
                    "semantic": None, "sort_order": None, "is_active": True,
                }
            )
            item.update({"code_list": code_list, "code": code})
            refused = None
            for column in present:
                if column in ("renews_item", "is_active"):
                    try:
                        flag = self._excel_flag(data.get(column))
                    except ValueError as exc:
                        refused = f"{where}: {column} {exc}"
                        break
                    # Empty keeps what the code had: a new code is active and renews nothing.
                    if flag is not None:
                        item[column] = flag
                elif column == "sort_order":
                    if cells[column] is not None:
                        item[column] = cells[column]
                else:
                    item[column] = cells[column]
            if refused is not None:
                problems.append((row_number, refused))
                continue
            unordered = item["sort_order"] is None
            try:
                fields = self._code_fields(item)
            except ValueError as exc:
                problems.append((row_number, f"{where}: {exc}"))
                continue
            uploaded.append((row_number, where, fields, unordered))
        if not uploaded and not problems:
            raise ValueError("The workbook has no codes in it. Download the code lists and fill that in.")

        for row_number, where, fields, _ in uploaded:
            cause = fields["default_cause_code"]
            if cause is not None and cause not in causes:
                problems.append((row_number, f"{where}: default cause {cause!r} is not in the failure cause list."))
        if problems:
            # Sheet order, though the default causes are checked last.
            problems.sort(key=lambda problem: problem[0])
            raise ValueError(_upload_refusal([message for _, message in problems]))
        # A new code given no sort order goes to the end of its list, in the sheet's order.
        last = {code_list: 0 for code_list in std.CODE_LISTS}
        for (code_list, _), entry in existing.items():
            last[code_list] = max(last[code_list], entry.sort_order)
        for _, _, fields, unordered in uploaded:
            if not unordered:
                last[fields["code_list"]] = max(last[fields["code_list"]], fields["sort_order"])
        for _, _, fields, unordered in uploaded:
            if unordered:
                last[fields["code_list"]] += 10
                fields["sort_order"] = last[fields["code_list"]]

        added: list[dict[str, Any]] = []
        updated: list[dict[str, Any]] = []
        for _, _, fields, _ in uploaded:
            before = existing.get((fields["code_list"], fields["code"]))
            summary = {
                "code_list": fields["code_list"],
                "list_label": std.CODE_LIST_LABELS[fields["code_list"]],
                "code": fields["code"],
                "name": fields["name"],
            }
            if before is None:
                added.append(summary)
            elif any(fields[key] != self._entry_dict(before)[key] for key in _CODE_FIELDS):
                updated.append(summary)
        if added or updated:
            changed = {(item["code_list"], item["code"]) for item in (*added, *updated)}
            with self.life.write_connection() as conn:
                for _, _, fields, _ in uploaded:
                    if (fields["code_list"], fields["code"]) in changed:
                        self._write_code(conn, fields, updated_by)
        matched = sum(1 for _, _, fields, _ in uploaded if (fields["code_list"], fields["code"]) in existing)
        return {
            "added": added,
            "updated": updated,
            "unchanged": matched - len(updated),
            "not_in_workbook": len(existing) - matched,
            "code_lists": self.code_lists(),
        }

    # ------------------------------------------------------------------ #
    # Disposition editor
    # ------------------------------------------------------------------ #
    def editor_options(self, asset_number: str) -> dict[str, Any]:
        """What the editor offers for one asset: categories, active codes and its sub-assets."""

        with self.life.connect() as conn:
            codebook = load_codebook(conn)
            tree = self.asset_tree(conn)
            sub_assets = tree.descendants(asset_number)
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
            "sub_asset_note": tree.hierarchy_note(asset_number),
            "record_classes": list(self._record_classes()),
            "scopes": list(DISPOSITION_SCOPES),
        }

    @staticmethod
    def _record_classes() -> tuple[str, ...]:
        from services.life_data_service import RECORD_CLASSES

        return RECORD_CLASSES

    RECORD_COLUMNS = """
        m.mapped_record_id, m.task_id, m.task_name, m.request_title, m.requestor_description, m.completion_notes,
        m.area_affected, m.condition_found, m.cause, m.action_taken,
        m.created_date_final, m.start_date_final, m.completed_date_final,
        CASE WHEN m.downtime_hours IS NULL THEN NULL WHEN m.downtime_hours < 0 THEN 0 ELSE m.downtime_hours END AS downtime_hours,
        TRIM(m.asset_number) AS asset_number, m.asset_name,
        COALESCE(m.record_class_final, m.record_class_auto) AS record_class, m.record_class_auto,
        w.wo_disposition_id, w.disposition_category, w.sub_asset, w.component_code, w.failure_mechanism_code,
        w.failure_mode_code, w.action_code, w.evidence, w.failure_cause_code, w.renewed_items_json, w.qty,
        w.item_detail, w.linked_mapped_record_id, w.disposition_note, w.legacy_disposition_category,
        w.legacy_failure_mode, w.legacy_failure_mechanism, w.source, w.review_flag, w.decided_by, w.decided_at,
        linked.task_id AS linked_task_id
    """
    RECORD_FROM = """
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

    @staticmethod
    def clean_list_filters(filters: Any) -> dict[str, list[str]]:
        """The column filters a request names, keyed by column; anything else is dropped.

        Each is the cell values to keep, "" standing for a blank cell, so a
        filter that names none keeps no row.
        """

        cleaned: dict[str, list[str]] = {}
        if not isinstance(filters, Mapping):
            return cleaned
        for key, values in filters.items():
            if key not in LIST_COLUMNS or not isinstance(values, (list, tuple)):
                continue
            kept = []
            for value in values:
                text = "" if value is None else str(value).strip()
                if text not in kept:
                    kept.append(text)
            cleaned[key] = kept[:LIST_FILTER_LIMIT]
        return cleaned

    @staticmethod
    def _filter_clause(filters: Mapping[str, list[str]], *, skip: str | None = None) -> tuple[str, list[Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        for key, values in filters.items():
            if key == skip:
                continue
            value_sql = LIST_COLUMNS[key][0]
            named = [value for value in values if value]
            matches = []
            if named:
                matches.append(f"{value_sql} IN ({', '.join('?' * len(named))})")
                params.extend(named)
            if "" in values:
                matches.append(f"NULLIF(TRIM({value_sql}), '') IS NULL")
            clauses.append(f" AND ({' OR '.join(matches) or '0'})")
        return "".join(clauses), params

    def _list_where(
        self,
        asset_number: str,
        scope: str,
        search: str | None,
        filters: Mapping[str, list[str]],
        *,
        skip: str | None = None,
    ) -> tuple[str, list[Any]]:
        """The WHERE the list reads: the asset, the Show setting, the search and the column filters."""

        search_clause, search_params = self.life._disposition_search_clause(search)
        filter_clause, filter_params = self._filter_clause(filters, skip=skip)
        where = f"WHERE TRIM(m.asset_number) = ?{self._scope_clause(scope)}{search_clause}{filter_clause}"
        return where, [asset_number, *search_params, *filter_params]

    @staticmethod
    def _list_order(sort: str | None, direction: str | None) -> str:
        if sort not in LIST_COLUMNS:
            return LIST_DEFAULT_ORDER
        value_sql, order_sql = LIST_COLUMNS[sort]
        way = "DESC" if (direction or "").lower() == "desc" else "ASC"
        return f"NULLIF(TRIM({value_sql}), '') IS NULL, {order_sql} {way}, {LIST_DEFAULT_ORDER}"

    @staticmethod
    def _register_labels(conn: sqlite3.Connection, tree: AssetTree) -> None:
        conn.create_function("gremlin_asset_label", 1, lambda number: tree.label(number) if number else number)

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

    def record_dict(self, row: sqlite3.Row, codebook: std.CodeBook) -> dict[str, Any]:
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
            "start_date": row["start_date_final"],
            "completed_date": row["completed_date_final"],
            "downtime_hours": row["downtime_hours"],
            "asset_number": row["asset_number"],
            "asset_name": row["asset_name"],
            "record_class": row["record_class"],
            "record_class_auto": row["record_class_auto"],
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
        filters: Mapping[str, Any] | None = None,
        sort: str | None = None,
        direction: str | None = None,
        page: int = 0,
        page_size: int = DISPOSITION_PAGE_SIZE,
    ) -> dict[str, Any]:
        """One page of an asset's work orders with their standard dispositions.

        Newest first, unless ``sort`` names one of LIST_COLUMNS; ``filters`` keeps
        the rows whose column shows one of the values named for it. Both apply to
        every row in the selection, not only the page on screen.
        """

        if scope not in DISPOSITION_SCOPES:
            scope = "all"
        filters = self.clean_list_filters(filters)
        sort = sort if sort in LIST_COLUMNS else None
        direction = "desc" if (direction or "").lower() == "desc" else "asc"
        where, params = self._list_where(asset_number, scope, search, filters)
        token = self.life.change_token()
        with self.life.read_transaction() as conn:
            codebook = load_codebook(conn)
            tree = self.asset_tree(conn, token=token)
            self._register_labels(conn, tree)
            total = int(conn.execute(f"SELECT COUNT(*) {self.RECORD_FROM} {where}", params).fetchone()[0])
            counts = conn.execute(
                f"""
                SELECT COUNT(*) AS all_count,
                       SUM(CASE WHEN w.wo_disposition_id IS NULL OR w.disposition_category = 'NOT_REVIEWED' THEN 1 ELSE 0 END) AS not_reviewed,
                       SUM(CASE WHEN w.source = 'MIGRATION' THEN 1 ELSE 0 END) AS migrated
                {self.RECORD_FROM}
                WHERE TRIM(m.asset_number) = ?
                """,
                (asset_number,),
            ).fetchone()
            max_page = max(0, math.ceil(total / page_size) - 1) if total else 0
            page = min(max(0, int(page)), max_page)
            rows = conn.execute(
                f"""
                SELECT {self.RECORD_COLUMNS} {self.RECORD_FROM} {where}
                ORDER BY {self._list_order(sort, direction)}
                LIMIT ? OFFSET ?
                """,
                [*params, page_size, page * page_size],
            ).fetchall()
            records = [self.record_dict(row, codebook) for row in rows]
            self._add_plant_times(conn, records)
        for record in records:
            self._add_labels(record, tree)
        return {
            "asset_number": asset_number,
            "asset_label": tree.label(asset_number),
            "scope": scope,
            "search": search or "",
            "filters": filters,
            "sort": {"key": sort, "dir": direction} if sort else None,
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

    def list_column_values(
        self,
        asset_number: str,
        column: str,
        *,
        scope: str = "all",
        search: str | None = None,
        filters: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """The values one list column shows, for its filter menu, with how many rows show each.

        Read under the Show setting, the search and every other column's filter,
        as a spreadsheet's filter lists them, so a value this column's own filter
        hides can always be ticked back.
        """

        if column not in LIST_COLUMNS:
            raise ValueError(f"The list has no column {column!r}.")
        if scope not in DISPOSITION_SCOPES:
            scope = "all"
        where, params = self._list_where(asset_number, scope, search, self.clean_list_filters(filters), skip=column)
        value_sql = LIST_COLUMNS[column][0]
        token = self.life.change_token()
        with self.life.read_transaction() as conn:
            codebook = load_codebook(conn)
            tree = self.asset_tree(conn, token=token)
            rows = conn.execute(
                f"""
                SELECT COALESCE(NULLIF(TRIM({value_sql}), ''), '') AS value, COUNT(*) AS n
                {self.RECORD_FROM} {where}
                GROUP BY 1
                """,
                params,
            ).fetchall()
        values = []
        for row in rows:
            value = row["value"]
            if not value:
                label = "(Blanks)"
            elif column == "sub_asset":
                label = tree.label(value)
            elif column in LIST_COLUMN_CODE_LISTS:
                name = codebook.name(LIST_COLUMN_CODE_LISTS[column], value)
                label = f"{value} — {name}" if name and name != value else value
            else:
                label = value
            values.append({"value": value, "label": label, "count": int(row["n"])})

        def order(item: Mapping[str, Any]) -> tuple:
            value = item["value"]
            if column == "task_id":
                return (not value, int(value) if value.isdigit() else math.inf, value)
            return (not value, str(item["label"]).casefold())

        values.sort(key=order)
        if column == "found":
            # Newest day first, as the list opens; blanks stay last.
            dated = [item for item in values if item["value"]]
            values = dated[::-1] + [item for item in values if not item["value"]]
        return {
            "column": column,
            "values": values[:LIST_VALUES_LIMIT],
            "truncated": len(values) > LIST_VALUES_LIMIT,
        }

    def disposition_record(self, mapped_record_id: int) -> dict[str, Any] | None:
        records = self.disposition_records([mapped_record_id])
        return records[0] if records else None

    def disposition_records(self, mapped_record_ids: Iterable[int]) -> list[dict[str, Any]]:
        """Records as the Disposition page's list holds them, in the order asked for; missing ids are left out."""

        ids = [int(mapped_record_id) for mapped_record_id in mapped_record_ids]
        if not ids:
            return []
        token = self.life.change_token()
        with self.life.read_transaction() as conn:
            codebook = load_codebook(conn)
            rows = conn.execute(
                f"SELECT {self.RECORD_COLUMNS} {self.RECORD_FROM} WHERE m.mapped_record_id IN ({', '.join('?' * len(ids))})",
                ids,
            ).fetchall()
            by_id = {int(row["mapped_record_id"]): self.record_dict(row, codebook) for row in rows}
            records = [by_id[mapped_record_id] for mapped_record_id in ids if mapped_record_id in by_id]
            self._add_plant_times(conn, records)
            tree = self.asset_tree(conn, token=token)
            for record in records:
                self._add_labels(record, tree)
            return records

    @staticmethod
    def _add_labels(record: dict[str, Any], tree: AssetTree) -> None:
        """The record's asset and sub-asset by name, as the record card and the list show them."""

        record["asset_label"] = tree.label(record["asset_number"])
        record["location_label"] = tree.label(record["derived"]["location"])
        # The name the work order itself carries, when it is not the asset's own:
        # one filed under 4002 can be named for the sub-asset it was on.
        own = (record.get("asset_name") or "").strip()
        record["logged_asset_name"] = (
            own if own and own.casefold() not in (tree.name(record["asset_number"]).casefold(), str(record["asset_number"]).casefold())
            else None
        )

    def _add_plant_times(self, conn: sqlite3.Connection, records: Iterable[dict[str, Any]]) -> None:
        """Each record's created, started and completed times on the plant's clock, for its card.

        The CMMS dates are stored in UTC; the card reads them as the plant does,
        with the zone's abbreviation. A date stored without a clock time stays a
        date, and one that is not a date at all is shown as it was stored.
        """

        zone, zone_name, _ = self.life._plant_time_zone(conn)
        for record in records:
            record["plant_times"] = {
                key: self._plant_time(record.get(field), zone)
                for key, field in (("created", "created_date"), ("started", "start_date"), ("completed", "completed_date"))
            }
            record["plant_time_zone"] = zone_name

    def _plant_time(self, value: Any, zone: Any) -> str | None:
        text = "" if value is None else str(value).strip()
        if not text:
            return None
        parsed = self.life._parse_datetime(text)
        if parsed is None:
            return text
        if not re.search(r"\d{1,2}:\d{2}", text):
            return parsed.date().isoformat()
        return parsed.astimezone(zone).strftime("%Y-%m-%d %H:%M %Z").strip()

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
        ``linked_mapped_record_id``. ``record_class`` corrects what kind of work
        order the record is (a PM logged as a corrective, say): it is kept on the
        record itself, where every screen reads it, and decides the detection
        method. Any refused item refuses the whole batch, with the record named in
        the message.
        """

        saved = 0
        record_classes = self._record_classes()
        with self.life.write_connection() as conn:
            codebook = load_codebook(conn)
            # Read inside the write transaction, before anything is written: no
            # other write can land while this one holds the writer slot, so the
            # token is the database this transaction reads.
            tree = self.asset_tree(conn, token=self.life.change_token())
            children: dict[str, tuple[str, ...]] = {}
            for item in items:
                try:
                    mapped_record_id = int(item.get("mapped_record_id"))
                except (TypeError, ValueError):
                    raise ValueError("Each disposition needs the mapped_record_id of its work order.") from None
                record = conn.execute(
                    f"SELECT {self.RECORD_COLUMNS} {self.RECORD_FROM} WHERE m.mapped_record_id = ?",
                    (mapped_record_id,),
                ).fetchone()
                if record is None:
                    raise ValueError(f"Record {mapped_record_id} was not found.")
                asset_number = record["asset_number"] or ""
                if not asset_number:
                    raise ValueError(f"Task {record['task_id']} has no asset number, so it cannot be dispositioned.")
                if asset_number not in children:
                    children[asset_number] = tuple(option["asset_number"] for option in tree.descendants(asset_number))
                values = dict(item)
                try:
                    record_class = record["record_class"]
                    chosen_class = _clean(values.get("record_class"))
                    if chosen_class is not None:
                        chosen_class = chosen_class.upper()
                        if chosen_class not in record_classes:
                            raise ValueError(f"Record class must be one of {', '.join(record_classes)}.")
                    if "linked_wo" in values and "linked_mapped_record_id" not in values:
                        values["linked_mapped_record_id"] = self._resolve_linked(conn, values.get("linked_wo"), asset_number)
                    previous = self._disposition_dict(record) if record["wo_disposition_id"] is not None else None
                    normalized = std.normalize_disposition(
                        values,
                        record=std.RecordContext(
                            mapped_record_id=mapped_record_id,
                            asset_number=asset_number,
                            sub_asset_choices=children[asset_number],
                            record_class=chosen_class or record_class,
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
                if chosen_class is not None and chosen_class != record_class:
                    conn.execute(
                        "UPDATE mapped_cmms_record SET record_class_final = ? WHERE mapped_record_id = ?",
                        (chosen_class, mapped_record_id),
                    )
                    record_class = chosen_class
                insert_wo_disposition(conn, mapped_record_id, {
                    **normalized,
                    "record_class": record_class,
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
    # Excel workbook
    # ------------------------------------------------------------------ #
    def _excel_records(
        self,
        asset_number: str,
        scope: str,
        *,
        search: str | None = None,
        filters: Mapping[str, Any] | None = None,
        sort: str | None = None,
        direction: str | None = None,
    ) -> list[dict[str, Any]]:
        if scope not in DISPOSITION_SCOPES:
            scope = "all"
        where, params = self._list_where(asset_number, scope, search, self.clean_list_filters(filters))
        token = self.life.change_token()
        with self.life.read_transaction() as conn:
            codebook = load_codebook(conn)
            self._register_labels(conn, self.asset_tree(conn, token=token))
            rows = conn.execute(
                f"SELECT {self.RECORD_COLUMNS} {self.RECORD_FROM} {where} ORDER BY {self._list_order(sort, direction)}",
                params,
            ).fetchall()
            return [self.record_dict(row, codebook) for row in rows]

    @staticmethod
    def _excel_values(record: Mapping[str, Any]) -> dict[str, Any]:
        """A record's disposition as the workbook's columns hold it."""

        disposition = record["disposition"]
        return {
            "disposition_category": disposition["disposition_category"],
            "record_class": record["record_class"],
            "sub_asset": disposition["sub_asset"],
            "component_code": disposition["component_code"],
            "failure_mechanism_code": disposition["failure_mechanism_code"],
            "failure_mode_code": disposition["failure_mode_code"],
            "action_code": disposition["action_code"],
            "evidence": disposition["evidence"],
            "failure_cause_code": disposition["failure_cause_code"],
            "renewed_items": ", ".join(disposition["renewed_items"]),
            "qty": disposition["qty"],
            "item_detail": disposition["item_detail"],
            "linked_wo": disposition["linked_task_id"],
            "disposition_note": disposition["disposition_note"],
        }

    def export_excel(
        self,
        asset_number: str,
        output_path: str | Path,
        *,
        scope: str = "all",
        search: str | None = None,
        filters: Mapping[str, Any] | None = None,
        sort: str | None = None,
        direction: str | None = None,
    ) -> int:
        """Write an asset's work orders and their dispositions to a workbook, and return how many.

        ``scope`` is the page's Show setting (every record, not reviewed, or
        migrated to confirm), and ``search``, ``filters`` and ``sort`` the list's,
        so the workbook holds the rows the list shows, in its order: a workbook
        for working through a backlog holds only that backlog. Each coded column has a dropdown of the active codes,
        drawn from a second sheet that also says what each code means.
        """

        from services.life_data_service import COLUMN_TYPE_DATETIME, COLUMN_TYPE_NUMBER, COLUMN_TYPE_TEXT, ExcelValidation

        records = self._excel_records(
            asset_number, scope, search=search, filters=filters, sort=sort, direction=direction
        )
        options = self.editor_options(asset_number)
        sheet_rows: list[list[Any]] = [list(EXCEL_COLUMNS)]
        for record in records:
            disposition = record["disposition"]
            legacy = " / ".join(
                text for text in (
                    disposition["legacy_disposition_category"],
                    disposition["legacy_failure_mode"],
                    disposition["legacy_failure_mechanism"],
                ) if text
            )
            values = {
                "mapped_record_id": record["mapped_record_id"],
                "task_id": record["task_id"],
                "task_name": record["task_name"],
                "created_date": record["created_date"],
                "completed_date": record["completed_date"],
                "downtime_hours": record["downtime_hours"],
                "request_title": record["request_title"],
                "requestor_description": record["requestor_description"],
                "completion_notes": record["completion_notes"],
                **{key: record[key] for key in NARRATIVE_KEYS},
                "legacy_disposition": legacy or None,
                "review_flag": disposition["review_flag"],
                **self._excel_values(record),
            }
            sheet_rows.append([values.get(column) for column in EXCEL_COLUMNS])

        lists: list[tuple[str, list[str], list[str]]] = [
            ("disposition_category", [c["code"] for c in options["categories"]], [c["label"] for c in options["categories"]]),
            ("record_class", list(options["record_classes"]), list(options["record_classes"])),
            ("sub_asset", [o["asset_number"] for o in options["sub_assets"]], [o.get("asset_name") or "" for o in options["sub_assets"]]),
            ("evidence", [e["code"] for e in options["evidence"]], [e["label"] for e in options["evidence"]]),
        ]
        for column, code_list in EXCEL_CODE_COLUMNS.items():
            entries = options["code_lists"][code_list]
            lists.append((column, [entry["code"] for entry in entries], [entry["name"] for entry in entries]))
        lookup_rows = self._lookup_rows(lists)
        validations = []
        for position, (column, codes, _) in enumerate(lists):
            if not codes:
                continue
            validations.append(ExcelValidation(
                column_name=column,
                validation_type="list",
                formula1=self._lookup_range(position, codes),
                error=f"Choose a {column.replace('_', ' ')} from the list (see the Lookup Lists sheet).",
            ))
        validations.append(ExcelValidation(
            column_name="qty", validation_type="whole", operator="greaterThanOrEqual", formula1="1",
            error="Qty is a whole number of units, at least 1.",
        ))
        column_types = {column: COLUMN_TYPE_TEXT for column in EXCEL_COLUMNS}
        column_types.update({
            "mapped_record_id": COLUMN_TYPE_NUMBER,
            "task_id": COLUMN_TYPE_NUMBER,
            "downtime_hours": COLUMN_TYPE_NUMBER,
            "qty": COLUMN_TYPE_NUMBER,
            "linked_wo": COLUMN_TYPE_NUMBER,
            "created_date": COLUMN_TYPE_DATETIME,
            "completed_date": COLUMN_TYPE_DATETIME,
        })
        self.life._write_xlsx(
            output_path,
            sheet_rows,
            EXCEL_SHEET_NAME,
            validations=validations,
            lookup_rows=lookup_rows,
            column_types=column_types,
            editable_columns=frozenset(EXCEL_DISPOSITION_COLUMNS),
        )
        return len(records)

    @staticmethod
    def _excel_cell(value: Any) -> str | None:
        """A workbook cell as text: a whole number Excel stored as 7.0 reads back as 7."""

        if value is None:
            return None
        if isinstance(value, float) and value.is_integer():
            value = int(value)
        return _clean(value)

    def import_excel(self, asset_number: str, input_path: str | Path, *, decided_by: str | None = None) -> dict[str, int]:
        """Save the dispositions a filled-in workbook changed, all or none.

        Rows are matched by ``mapped_record_id`` and must be the asset's. A row
        whose disposition columns read as the record's current disposition is left
        alone, so re-uploading a workbook does not stamp every record as decided
        again; every other row is saved through the editor's own checks, and one
        refused row refuses the upload, named by its sheet row and task.
        """

        rows = self.life._read_xlsx(input_path)
        if not rows:
            raise ValueError("The workbook is empty. Download the template and fill that in.")
        headers = [str(value).strip().lower() if value is not None else "" for value in rows[0]]
        if "mapped_record_id" not in headers:
            raise ValueError("The workbook needs the mapped_record_id column from the downloaded template.")
        present = [column for column in EXCEL_DISPOSITION_COLUMNS if column in headers]
        if "disposition_category" not in present:
            raise ValueError("The workbook needs the disposition_category column from the downloaded template.")
        current = {record["mapped_record_id"]: record for record in self._excel_records(asset_number, "all")}
        changed: list[tuple[int, dict[str, Any]]] = []
        seen: set[int] = set()
        unchanged = 0
        for row_number, values in enumerate(rows[1:], start=2):
            data = {header: values[index] if index < len(values) else None for index, header in enumerate(headers) if header}
            raw_id = self._excel_cell(data.get("mapped_record_id"))
            if raw_id is None:
                continue
            try:
                mapped_record_id = int(raw_id)
            except ValueError:
                raise ValueError(f"Row {row_number}: mapped_record_id {raw_id!r} is not a record number.") from None
            if mapped_record_id not in current:
                raise ValueError(f"Row {row_number}: record {mapped_record_id} is not one of asset {asset_number}'s work orders.")
            if mapped_record_id in seen:
                raise ValueError(f"Row {row_number}: record {mapped_record_id} is in the workbook twice.")
            seen.add(mapped_record_id)
            record = current[mapped_record_id]
            existing = {key: self._excel_cell(value) for key, value in self._excel_values(record).items()}
            uploaded = {column: self._excel_cell(data.get(column)) for column in present}
            if uploaded.get("disposition_category"):
                uploaded["disposition_category"] = uploaded["disposition_category"].upper()
            if all(uploaded[column] == existing[column] for column in present):
                unchanged += 1
                continue
            item: dict[str, Any] = {**existing, **uploaded, "mapped_record_id": mapped_record_id}
            item["renewed_items"] = [code for code in (item.get("renewed_items") or "").replace(";", ",").split(",") if code.strip()]
            changed.append((row_number, item))
        try:
            saved = self.save_dispositions([item for _, item in changed], decided_by=decided_by)
        except ValueError as exc:
            message = str(exc)
            for row_number, item in changed:
                task = current[item["mapped_record_id"]]["task_id"] or item["mapped_record_id"]
                if message.startswith(f"Task {task}:"):
                    raise ValueError(f"Row {row_number}, {message} Nothing was saved.") from None
            raise ValueError(f"{message} Nothing was saved.") from None
        return {"saved": saved, "unchanged": unchanged}
