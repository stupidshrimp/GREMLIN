"""SQLite repository and raw-work-order adapter for the Availability Dashboard."""

from __future__ import annotations

import json
import re
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from services.life_data_service import DB_WRITE_TIMEOUT_SECONDS, DEFAULT_DB_PATH, resolve_default_db_path

from .availability_calculator import AssetGroup, AvailabilityResult, LinkedDowntimeRule, RawWorkOrder
from .availability_config import DEFAULT_ASSET_GROUPS, DEFAULT_DISPLAY_NAMES, DEFAULT_LINKED_RULES, DEFAULT_SETTINGS


_DEFAULT_DB_PATH_SENTINEL = object()


class AvailabilityRepository:
    """Owns availability tables and adapts existing GREMLIN work-order rows."""

    def __init__(self, db_path: Path | str | object = _DEFAULT_DB_PATH_SENTINEL) -> None:
        self.db_path = Path(r"\\sandc.ws\depts\Facilities\FACIL\MAIN-ENG\901 Reliability Projects\901 Reliability Projects\Weibull Data\Database\GREMLIN.db")

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=DB_WRITE_TIMEOUT_SECONDS)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(f"PRAGMA busy_timeout = {DB_WRITE_TIMEOUT_SECONDS * 1000}")
        return conn

    @contextmanager
    def write_connection(self) -> Iterator[sqlite3.Connection]:
        conn: sqlite3.Connection | None = None
        try:
            conn = self.connect()
            conn.execute("PRAGMA journal_mode = DELETE")
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except Exception:
            if conn is not None:
                conn.rollback()
            raise
        finally:
            if conn is not None:
                conn.close()

    def ensure_schema(self) -> None:
        with self.write_connection() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS availability_settings (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    selected_year INTEGER NOT NULL,
                    utc_offset_hours REAL NOT NULL DEFAULT 5,
                    last_updated TEXT
                );
                CREATE TABLE IF NOT EXISTS availability_asset_groups (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    asset_group TEXT NOT NULL UNIQUE,
                    schedule_hours_per_day REAL NOT NULL,
                    break_hours_per_day REAL NOT NULL DEFAULT 0,
                    lunch_hours_per_day REAL NOT NULL DEFAULT 0,
                    setup_hours_per_day REAL NOT NULL DEFAULT 0,
                    net_scheduled_hours_per_day REAL NOT NULL,
                    include_flag INTEGER NOT NULL DEFAULT 1,
                    notes TEXT,
                    sort_order INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS availability_asset_group_assets (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    asset_group_id INTEGER NOT NULL,
                    asset_number TEXT NOT NULL,
                    sort_order INTEGER NOT NULL,
                    UNIQUE(asset_group_id, asset_number),
                    FOREIGN KEY(asset_group_id) REFERENCES availability_asset_groups(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS availability_asset_display_names (
                    asset_number TEXT PRIMARY KEY,
                    display_name TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS availability_linked_downtime_rules (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    rule_group TEXT NOT NULL,
                    parent_asset_number TEXT NOT NULL,
                    linked_asset_number TEXT NOT NULL,
                    impact_factor REAL NOT NULL DEFAULT 0.5
                );
                CREATE TABLE IF NOT EXISTS availability_manual_ot (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    asset_group TEXT NOT NULL,
                    asset_number TEXT NOT NULL,
                    month_date TEXT NOT NULL,
                    manual_ot_hours REAL NOT NULL DEFAULT 0,
                    UNIQUE(asset_group, asset_number, month_date)
                );
                CREATE TABLE IF NOT EXISTS availability_goal_percent (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    asset_group TEXT NOT NULL,
                    month_date TEXT NOT NULL,
                    goal_percent REAL NOT NULL DEFAULT 0.95,
                    UNIQUE(asset_group, month_date)
                );
                CREATE TABLE IF NOT EXISTS availability_results (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_timestamp TEXT NOT NULL,
                    selected_year INTEGER NOT NULL,
                    asset_group TEXT NOT NULL,
                    asset_number TEXT NOT NULL,
                    asset_display_name TEXT,
                    month_date TEXT NOT NULL,
                    month_label TEXT NOT NULL,
                    scheduled_hours REAL NOT NULL,
                    downtime_minutes REAL NOT NULL,
                    downtime_hours REAL NOT NULL,
                    availability_percent REAL NOT NULL,
                    flagged INTEGER NOT NULL DEFAULT 0,
                    overlap_count INTEGER NOT NULL DEFAULT 0,
                    manual_ot_hours REAL NOT NULL DEFAULT 0,
                    adjusted_scheduled_hours REAL NOT NULL,
                    linked_downtime_hours REAL NOT NULL DEFAULT 0,
                    adjusted_downtime_hours REAL NOT NULL,
                    adjusted_availability_percent REAL NOT NULL,
                    downtime_logic TEXT NOT NULL,
                    total_wo_count INTEGER NOT NULL DEFAULT 0,
                    zero_downtime_wo_count INTEGER NOT NULL DEFAULT 0,
                    no_wo_entries_flag INTEGER NOT NULL DEFAULT 0,
                    zero_downtime_wo_percent REAL NOT NULL DEFAULT 0,
                    zero_no_entry_note TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_availability_results_run ON availability_results(run_timestamp, selected_year);
                CREATE INDEX IF NOT EXISTS idx_availability_results_group_month ON availability_results(asset_group, month_date);
                """
            )
            self._seed_defaults(conn)

    def _seed_defaults(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            "INSERT OR IGNORE INTO availability_settings(id, selected_year, utc_offset_hours) VALUES (1, ?, ?)",
            (DEFAULT_SETTINGS["selected_year"], DEFAULT_SETTINGS["utc_offset_hours"]),
        )
        if conn.execute("SELECT COUNT(*) FROM availability_asset_groups").fetchone()[0] == 0:
            for index, (name, assets, schedule, brk, lunch, setup, include, notes) in enumerate(DEFAULT_ASSET_GROUPS, start=1):
                net = max(0.0, float(schedule) - float(brk) - float(lunch) - float(setup))
                cur = conn.execute(
                    """
                    INSERT INTO availability_asset_groups(asset_group, schedule_hours_per_day, break_hours_per_day, lunch_hours_per_day,
                        setup_hours_per_day, net_scheduled_hours_per_day, include_flag, notes, sort_order)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (name, schedule, brk, lunch, setup, net, include, notes, index),
                )
                for asset_index, asset_number in enumerate(assets, start=1):
                    conn.execute(
                        "INSERT INTO availability_asset_group_assets(asset_group_id, asset_number, sort_order) VALUES (?, ?, ?)",
                        (cur.lastrowid, str(asset_number), asset_index),
                    )
        for asset_number, display_name in DEFAULT_DISPLAY_NAMES.items():
            conn.execute(
                "INSERT OR IGNORE INTO availability_asset_display_names(asset_number, display_name) VALUES (?, ?)",
                (str(asset_number), display_name),
            )
        if conn.execute("SELECT COUNT(*) FROM availability_linked_downtime_rules").fetchone()[0] == 0:
            conn.executemany(
                "INSERT INTO availability_linked_downtime_rules(rule_group, parent_asset_number, linked_asset_number, impact_factor) VALUES (?, ?, ?, ?)",
                [(group, str(parent), str(linked), factor) for group, parent, linked, factor in DEFAULT_LINKED_RULES],
            )

    def load_settings(self) -> dict[str, Any]:
        with self.connect() as conn:
            row = conn.execute("SELECT selected_year, utc_offset_hours, last_updated FROM availability_settings WHERE id = 1").fetchone()
        return dict(row) if row else {"selected_year": date.today().year, "utc_offset_hours": 5.0, "last_updated": None}

    def save_settings(self, selected_year: int, utc_offset_hours: float, *, touch_last_updated: bool = False) -> None:
        last_updated = date.today().isoformat() if touch_last_updated else self.load_settings().get("last_updated")
        with self.write_connection() as conn:
            conn.execute(
                """
                INSERT INTO availability_settings(id, selected_year, utc_offset_hours, last_updated) VALUES (1, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET selected_year=excluded.selected_year, utc_offset_hours=excluded.utc_offset_hours, last_updated=excluded.last_updated
                """,
                (selected_year, utc_offset_hours, last_updated),
            )

    def load_asset_groups(self, include_only: bool = False) -> list[AssetGroup]:
        with self.connect() as conn:
            where = "WHERE g.include_flag = 1" if include_only else ""
            rows = conn.execute(
                f"""
                SELECT g.*,
                       (
                           SELECT GROUP_CONCAT(ordered.asset_number, ',')
                           FROM (
                               SELECT asset_number
                               FROM availability_asset_group_assets
                               WHERE asset_group_id = g.id
                               ORDER BY sort_order, id
                           ) AS ordered
                       ) AS asset_numbers
                FROM availability_asset_groups g
                {where}
                ORDER BY g.sort_order, g.asset_group
                """
            ).fetchall()
        return [self._group_from_row(row) for row in rows]

    def save_asset_groups(self, groups: list[AssetGroup]) -> None:
        self._validate_groups(groups)
        with self.write_connection() as conn:
            conn.execute("DELETE FROM availability_asset_group_assets")
            conn.execute("DELETE FROM availability_asset_groups")
            for index, group in enumerate(groups, start=1):
                net = max(0.0, group.schedule_hours_per_day - group.break_hours_per_day - group.lunch_hours_per_day - group.setup_hours_per_day)
                cur = conn.execute(
                    """
                    INSERT INTO availability_asset_groups(asset_group, schedule_hours_per_day, break_hours_per_day, lunch_hours_per_day,
                        setup_hours_per_day, net_scheduled_hours_per_day, include_flag, notes, sort_order)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (group.asset_group.strip(), group.schedule_hours_per_day, group.break_hours_per_day, group.lunch_hours_per_day,
                     group.setup_hours_per_day, net, 1 if group.include_flag else 0, group.notes, index),
                )
                for asset_index, asset_number in enumerate(group.asset_numbers, start=1):
                    asset = str(asset_number).strip()
                    if asset:
                        conn.execute(
                            "INSERT OR IGNORE INTO availability_asset_group_assets(asset_group_id, asset_number, sort_order) VALUES (?, ?, ?)",
                            (cur.lastrowid, asset, asset_index),
                        )

    def _group_from_row(self, row: sqlite3.Row) -> AssetGroup:
        return AssetGroup(
            id=row["id"],
            asset_group=row["asset_group"],
            asset_numbers=[asset for asset in str(row["asset_numbers"] or "").split(",") if asset],
            schedule_hours_per_day=float(row["schedule_hours_per_day"]),
            break_hours_per_day=float(row["break_hours_per_day"]),
            lunch_hours_per_day=float(row["lunch_hours_per_day"]),
            setup_hours_per_day=float(row["setup_hours_per_day"]),
            net_scheduled_hours_per_day=max(0.0, float(row["net_scheduled_hours_per_day"])),
            include_flag=bool(row["include_flag"]),
            notes=row["notes"] or "",
            sort_order=int(row["sort_order"]),
        )

    def load_display_names(self) -> dict[str, str]:
        with self.connect() as conn:
            return {str(row["asset_number"]): row["display_name"] for row in conn.execute("SELECT asset_number, display_name FROM availability_asset_display_names ORDER BY asset_number")}

    def save_display_names(self, names: dict[str, str]) -> None:
        with self.write_connection() as conn:
            conn.execute("DELETE FROM availability_asset_display_names")
            for asset_number, display_name in names.items():
                if str(asset_number).strip() and str(display_name).strip():
                    conn.execute("INSERT INTO availability_asset_display_names(asset_number, display_name) VALUES (?, ?)", (str(asset_number).strip(), str(display_name).strip()))

    def load_linked_rules(self) -> list[LinkedDowntimeRule]:
        with self.connect() as conn:
            rows = conn.execute("SELECT rule_group, parent_asset_number, linked_asset_number, impact_factor FROM availability_linked_downtime_rules ORDER BY id").fetchall()
        return [LinkedDowntimeRule(row["rule_group"], str(row["parent_asset_number"]), str(row["linked_asset_number"]), float(row["impact_factor"])) for row in rows]

    def save_linked_rules(self, rules: list[LinkedDowntimeRule]) -> None:
        for rule in rules:
            if not rule.rule_group.strip() or not rule.parent_asset_number.strip() or not rule.linked_asset_number.strip() or rule.impact_factor < 0:
                raise ValueError("Linked downtime rules require rule group, parent asset, linked asset, and non-negative impact factor.")
        with self.write_connection() as conn:
            conn.execute("DELETE FROM availability_linked_downtime_rules")
            conn.executemany(
                "INSERT INTO availability_linked_downtime_rules(rule_group, parent_asset_number, linked_asset_number, impact_factor) VALUES (?, ?, ?, ?)",
                [(rule.rule_group.strip(), str(rule.parent_asset_number).strip(), str(rule.linked_asset_number).strip(), float(rule.impact_factor)) for rule in rules],
            )

    def load_manual_ot(self, selected_year: int) -> dict[tuple[str, str, str], float]:
        with self.connect() as conn:
            rows = conn.execute("SELECT asset_group, asset_number, month_date, manual_ot_hours FROM availability_manual_ot WHERE substr(month_date, 1, 4) = ?", (str(selected_year),)).fetchall()
        return {(row["asset_group"], str(row["asset_number"]), row["month_date"]): float(row["manual_ot_hours"]) for row in rows}

    def save_manual_ot_entries(self, entries: list[tuple[str, str, str, float]]) -> None:
        with self.write_connection() as conn:
            self._upsert_manual_ot_entries(conn, entries)

    def replace_manual_ot_entries_for_year(self, selected_year: int, entries: list[tuple[str, str, str, float]]) -> None:
        with self.write_connection() as conn:
            conn.execute("DELETE FROM availability_manual_ot WHERE substr(month_date, 1, 4) = ?", (str(selected_year),))
            self._upsert_manual_ot_entries(conn, entries)

    def _upsert_manual_ot_entries(self, conn: sqlite3.Connection, entries: list[tuple[str, str, str, float]]) -> None:
        for group, asset, month_date, hours in entries:
            if float(hours) < 0:
                raise ValueError("Manual OT hours must be non-negative.")
            conn.execute(
                """
                INSERT INTO availability_manual_ot(asset_group, asset_number, month_date, manual_ot_hours) VALUES (?, ?, ?, ?)
                ON CONFLICT(asset_group, asset_number, month_date) DO UPDATE SET manual_ot_hours=excluded.manual_ot_hours
                """,
                (group, str(asset), month_date, float(hours)),
            )

    def load_goal_percent(self, selected_year: int) -> dict[tuple[str, str], float]:
        with self.connect() as conn:
            rows = conn.execute("SELECT asset_group, month_date, goal_percent FROM availability_goal_percent WHERE substr(month_date, 1, 4) = ?", (str(selected_year),)).fetchall()
        return {(row["asset_group"], row["month_date"]): float(row["goal_percent"]) for row in rows}

    def save_goal_entries(self, entries: list[tuple[str, str, float]]) -> None:
        with self.write_connection() as conn:
            self._upsert_goal_entries(conn, entries)

    def replace_goal_entries_for_year(self, selected_year: int, entries: list[tuple[str, str, float]]) -> None:
        with self.write_connection() as conn:
            conn.execute("DELETE FROM availability_goal_percent WHERE substr(month_date, 1, 4) = ?", (str(selected_year),))
            self._upsert_goal_entries(conn, entries)

    def _upsert_goal_entries(self, conn: sqlite3.Connection, entries: list[tuple[str, str, float]]) -> None:
        for group, month_date, goal in entries:
            decimal_goal = self._parse_goal_decimal(goal)
            conn.execute(
                """
                INSERT INTO availability_goal_percent(asset_group, month_date, goal_percent) VALUES (?, ?, ?)
                ON CONFLICT(asset_group, month_date) DO UPDATE SET goal_percent=excluded.goal_percent
                """,
                (group, month_date, decimal_goal),
            )

    def save_results(self, selected_year: int, results: list[AvailabilityResult]) -> str:
        run_timestamp = datetime.now().isoformat(timespec="seconds")
        with self.write_connection() as conn:
            conn.execute("DELETE FROM availability_results WHERE selected_year = ?", (selected_year,))
            for result in results:
                values = asdict(result)
                values["run_timestamp"] = run_timestamp
                cols = ", ".join(values)
                placeholders = ", ".join(f":{key}" for key in values)
                conn.execute(f"INSERT INTO availability_results ({cols}) VALUES ({placeholders})", values)
            conn.execute("UPDATE availability_settings SET last_updated = ? WHERE id = 1", (date.today().isoformat(),))
        return run_timestamp

    def load_results(self, selected_year: int | None = None) -> list[sqlite3.Row]:
        with self.connect() as conn:
            if selected_year is None:
                selected_year = self.load_settings()["selected_year"]
            return conn.execute("SELECT * FROM availability_results WHERE selected_year = ? ORDER BY asset_group, asset_number, month_date", (selected_year,)).fetchall()

    def load_raw_work_orders(self, selected_year: int, utc_offset_hours: float) -> list[RawWorkOrder]:
        with self.connect() as conn:
            has_mapped = self._table_exists(conn, "mapped_cmms_record")
            has_raw = self._table_exists(conn, "raw_cmms_record")
            if has_mapped:
                mapped_rows = self._load_from_mapped(conn, selected_year, utc_offset_hours)
                if mapped_rows or not has_raw:
                    return mapped_rows
            if has_raw:
                return self._load_from_raw_json(conn, selected_year, utc_offset_hours)
        return []

    def _load_from_mapped(self, conn: sqlite3.Connection, selected_year: int, utc_offset_hours: float) -> list[RawWorkOrder]:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(mapped_cmms_record)")}
        record_class_auto_expr = "record_class_auto" if "record_class_auto" in columns else "NULL AS record_class_auto"
        record_class_final_expr = "record_class_final" if "record_class_final" in columns else "NULL AS record_class_final"
        is_pm_candidate_expr = "is_pm_candidate" if "is_pm_candidate" in columns else "0 AS is_pm_candidate"
        rows = conn.execute(
            f"""
            SELECT task_id, asset_number, asset_name, created_date_final, created_datetime_raw, created_date_raw,
                   completed_date_final, completed_datetime_raw, completed_date_raw, downtime_minutes, type_raw,
                   status_raw, task_name, description_raw, completion_notes,
                   {record_class_auto_expr}, {record_class_final_expr}, {is_pm_candidate_expr}
            FROM mapped_cmms_record
            WHERE asset_number IS NOT NULL AND TRIM(asset_number) <> ''
            """
        ).fetchall()
        parsed: list[RawWorkOrder] = []
        for row in rows:
            created = self._parse_datetime(row["created_date_final"] or row["created_datetime_raw"] or row["created_date_raw"], utc_offset_hours)
            if created is None or created.year != selected_year:
                continue
            completed = self._parse_datetime(row["completed_date_final"] or row["completed_datetime_raw"] or row["completed_date_raw"], utc_offset_hours)
            parsed.append(RawWorkOrder(
                task_id=str(row["task_id"] or ""),
                asset_number=str(row["asset_number"]).strip(),
                asset_name=row["asset_name"],
                created_date=created,
                completed_date=completed,
                downtime_minutes=float(row["downtime_minutes"] or 0.0),
                type=row["type_raw"],
                status=row["status_raw"],
                name=row["task_name"],
                description=row["description_raw"],
                completion_notes=row["completion_notes"],
                is_pm_candidate=self._is_mapped_pm_row(row),
            ))
        return parsed

    @staticmethod
    def _is_mapped_pm_row(row: sqlite3.Row) -> bool:
        class_values = {str(row["record_class_auto"] or "").strip().upper(), str(row["record_class_final"] or "").strip().upper()}
        is_pm_candidate = str(row["is_pm_candidate"] or "").strip().lower() in {"1", "true", "yes", "y"}
        return is_pm_candidate or "PM" in class_values or "PM_RESET_CANDIDATE" in class_values

    def _load_from_raw_json(self, conn: sqlite3.Connection, selected_year: int, utc_offset_hours: float) -> list[RawWorkOrder]:
        rows = conn.execute("SELECT raw_json FROM raw_cmms_record").fetchall()
        parsed: list[RawWorkOrder] = []
        for row in rows:
            try:
                raw = json.loads(row["raw_json"] or "{}")
            except json.JSONDecodeError:
                continue
            asset_number = self._first(raw, "Asset Number", "asset_number", "assetNumber")
            created = self._parse_datetime(self._first(raw, "createdDate_Final", "createdDateFinal", "createdDateTime", "createdDate"), utc_offset_hours)
            if not asset_number or created is None or created.year != selected_year:
                continue
            parsed.append(RawWorkOrder(
                task_id=str(self._first(raw, "taskID", "task_id", "id") or ""),
                asset_number=str(asset_number).strip(),
                asset_name=self._first(raw, "Asset Name", "asset_name"),
                created_date=created,
                completed_date=self._parse_datetime(self._first(raw, "completedDate_Final", "dateCompletedfinal", "dateCompleted_Final", "completedDateFinal", "completedDateTime", "dateCompleted"), utc_offset_hours),
                downtime_minutes=self._parse_downtime_minutes(self._first(raw, "downtime", "downtime_minutes")),
                type=self._first(raw, "type"),
                status=self._first(raw, "status", "statusID"),
                name=self._first(raw, "name"),
                description=self._first(raw, "description", "requestorDescription"),
                completion_notes=self._first(raw, "completionNotes", "CompletionNotes"),
            ))
        return parsed

    @staticmethod
    def _parse_datetime(value: Any, utc_offset_hours: float) -> datetime | None:
        if value in (None, ""):
            return None
        if isinstance(value, (int, float)) or (isinstance(value, str) and value.strip().replace(".", "", 1).isdigit()):
            numeric = float(value)
            seconds = numeric / 1000.0 if numeric > 10_000_000_000 else numeric
            return datetime.fromtimestamp(seconds, tz=timezone.utc).replace(tzinfo=None) + timedelta(hours=utc_offset_hours)
        text = str(value).strip().replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(text)
            if parsed.tzinfo is not None:
                return parsed.astimezone(timezone.utc).replace(tzinfo=None) + timedelta(hours=utc_offset_hours)
            return parsed
        except ValueError:
            pass
        for fmt in ("%Y-%m-%d", "%Y-%m-%d %H:%M:%S", "%m/%d/%Y", "%m/%d/%Y %H:%M:%S"):
            try:
                return datetime.strptime(text[:19], fmt)
            except ValueError:
                pass
        return None

    @staticmethod
    def _parse_downtime_minutes(value: Any) -> float:
        if value in (None, ""):
            return 0.0
        if isinstance(value, (int, float)):
            return max(0.0, float(value))
        text = str(value).strip().lower()
        match = re.search(r"[-+]?\d*\.?\d+", text)
        if not match:
            return 0.0
        number = float(match.group())
        if "hour" in text or re.search(r"\bhr\b", text):
            number *= 60.0
        return max(0.0, number)

    @staticmethod
    def _parse_goal_decimal(goal: float) -> float:
        value = float(goal)
        return value / 100.0 if value > 1 else value

    @staticmethod
    def _first(raw: dict[str, Any], *keys: str) -> Any:
        for key in keys:
            if raw.get(key) not in (None, ""):
                return raw[key]
        return None

    @staticmethod
    def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
        return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table_name,)).fetchone() is not None

    @staticmethod
    def _validate_groups(groups: list[AssetGroup]) -> None:
        for group in groups:
            if not group.asset_group.strip():
                raise ValueError("Asset group name cannot be blank.")
            if min(group.schedule_hours_per_day, group.break_hours_per_day, group.lunch_hours_per_day, group.setup_hours_per_day) < 0:
                raise ValueError("Schedule, break, lunch, and setup hours must be non-negative.")
