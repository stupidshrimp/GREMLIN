"""Availability Dashboard storage and work-order loading.

Owns the seven ``availability_*`` configuration tables and adapts
``mapped_cmms_record`` into the ``WorkOrder`` values the calculator consumes.

Only *configuration* is stored -- schedules, group membership, display names,
linked rules, goals and manual overtime. Results are never persisted: they are
a pure function of configuration and work orders, and caching them recreates
the workbook's failure mode where numbers are only correct if someone
remembered to press Recalculate. See ``docs/availability-dashboard-design.md``
§2.5.

The one rule this module must never break: **work orders are loaded without any
filter on type or classification**. Both earlier implementations filtered and
both filtered wrongly; excluding ``is_pm_candidate`` alone would drop 16.3% of
downtime, because a ``\\bpm\\b`` regex matches the clock time in
return-to-service completion notes. See design doc §2.1.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Iterator
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from services.availability_config import (
    DEFAULT_ASSET_GROUPS,
    DEFAULT_DISPLAY_NAMES,
    DEFAULT_LINKED_RULES,
    DEFAULT_TIMEZONE,
)
from services.availability_service import AssetGroup, LinkedRule, WorkOrder, WorkOrderDetail
from services.life_data_service import database_write_error

DB_WRITE_TIMEOUT_SECONDS = 30

# One entry per configuration table: the body of its CREATE TABLE, and the
# columns it is expected to have. Kept per-table rather than as one script so
# the same definition can both create a table and rebuild a legacy one (see
# _migrate_legacy_tables).
_TABLES: dict[str, str] = {
    "availability_settings": """
        id INTEGER PRIMARY KEY CHECK (id = 1),
        timezone TEXT NOT NULL DEFAULT 'America/Chicago',
        seeded_defaults INTEGER NOT NULL DEFAULT 0,
        updated_at TEXT
    """,
    "availability_asset_groups": """
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        asset_group TEXT NOT NULL UNIQUE,
        schedule_hours_per_day REAL NOT NULL,
        break_hours_per_day REAL NOT NULL DEFAULT 0,
        lunch_hours_per_day REAL NOT NULL DEFAULT 0,
        setup_hours_per_day REAL NOT NULL DEFAULT 0,
        include_flag INTEGER NOT NULL DEFAULT 1,
        notes TEXT,
        sort_order INTEGER NOT NULL DEFAULT 0,
        updated_at TEXT
    """,
    "availability_asset_group_assets": """
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        asset_group_id INTEGER NOT NULL,
        asset_number TEXT NOT NULL,
        sort_order INTEGER NOT NULL DEFAULT 0,
        UNIQUE(asset_group_id, asset_number),
        FOREIGN KEY(asset_group_id) REFERENCES availability_asset_groups(id) ON DELETE CASCADE
    """,
    "availability_asset_display_names": """
        asset_number TEXT PRIMARY KEY,
        display_name TEXT NOT NULL,
        updated_at TEXT
    """,
    "availability_linked_downtime_rules": """
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        rule_group TEXT NOT NULL DEFAULT '',
        parent_asset_number TEXT NOT NULL,
        linked_asset_number TEXT NOT NULL,
        impact_factor REAL NOT NULL DEFAULT 0.5,
        updated_at TEXT,
        UNIQUE(parent_asset_number, linked_asset_number)
    """,
    "availability_goal_percent": """
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        asset_group TEXT NOT NULL,
        month_date TEXT NOT NULL,
        goal_percent REAL NOT NULL DEFAULT 0.95,
        updated_at TEXT,
        UNIQUE(asset_group, month_date)
    """,
    "availability_manual_ot": """
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        asset_number TEXT NOT NULL,
        month_date TEXT NOT NULL,
        manual_ot_hours REAL NOT NULL DEFAULT 0,
        updated_at TEXT,
        UNIQUE(asset_number, month_date)
    """,
}

_EXPECTED_COLUMNS: dict[str, tuple[str, ...]] = {
    "availability_settings": ("id", "timezone", "seeded_defaults", "updated_at"),
    "availability_asset_groups": (
        "id", "asset_group", "schedule_hours_per_day", "break_hours_per_day",
        "lunch_hours_per_day", "setup_hours_per_day", "include_flag", "notes",
        "sort_order", "updated_at",
    ),
    "availability_asset_group_assets": ("id", "asset_group_id", "asset_number", "sort_order"),
    "availability_asset_display_names": ("asset_number", "display_name", "updated_at"),
    "availability_linked_downtime_rules": (
        "id", "rule_group", "parent_asset_number", "linked_asset_number",
        "impact_factor", "updated_at",
    ),
    "availability_goal_percent": ("id", "asset_group", "month_date", "goal_percent", "updated_at"),
    "availability_manual_ot": ("id", "asset_number", "month_date", "manual_ot_hours", "updated_at"),
}

# Tables whose legacy shape can produce more than one row per new unique key, so
# the copy has to resolve the collision instead of failing. availability_manual_ot
# was keyed (asset_group, asset_number, month_date) and is now keyed
# (asset_number, month_date): if an asset ever moved between groups it has two
# rows for one month. Ordering by id and letting the later row win keeps the most
# recent edit, which is the one the user last intended.
_MIGRATION_ORDER_BY: dict[str, str] = {
    "availability_manual_ot": "id",
}

_SCHEMA = "\n".join(
    f"CREATE TABLE IF NOT EXISTS {name} ({body});" for name, body in _TABLES.items()
)


class AvailabilityConfigError(ValueError):
    """A configuration value the dashboard cannot accept."""


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _month_text(value: date | str) -> str:
    """Normalise a month to a first-of-month ISO date string."""

    if isinstance(value, date):
        return date(value.year, value.month, 1).isoformat()
    text = str(value).strip()
    try:
        parsed = date.fromisoformat(text[:10])
    except ValueError as exc:
        raise AvailabilityConfigError(f"'{value}' is not a valid month (expected YYYY-MM-DD).") from exc
    return date(parsed.year, parsed.month, 1).isoformat()


class AvailabilityRepository:
    """Reads and writes availability configuration, and loads work orders."""

    def __init__(self, db_path: Path | str) -> None:
        # Always the caller's path. The earlier attempt hardcoded a UNC share
        # here and ignored its argument, which would have pointed this page at
        # a different database than the rest of the app.
        self.db_path = Path(db_path)

    # ------------------------------------------------------------------
    # Connections
    # ------------------------------------------------------------------
    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=DB_WRITE_TIMEOUT_SECONDS)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(f"PRAGMA busy_timeout = {DB_WRITE_TIMEOUT_SECONDS * 1000}")
        return conn

    @contextmanager
    def write_connection(self) -> Iterator[sqlite3.Connection]:
        """Serialize writes the way the rest of GREMLIN does.

        ``BEGIN IMMEDIATE`` claims the writer slot up front and rollback-journal
        mode is used because the database lives on a shared drive where WAL is
        unsafe across many network filesystems.
        """

        conn: sqlite3.Connection | None = None
        try:
            conn = self.connect()
            conn.execute("PRAGMA journal_mode = DELETE")
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except Exception as exc:
            if conn is not None:
                conn.rollback()
            # Translate the everyday shared-drive failures -- another user
            # holding the writer slot, a read-only share, a full disk -- into the
            # actionable message the rest of GREMLIN uses. Left raw, a
            # "database is locked" surfaces as a generic 500 that tells a plant
            # user nothing they can do something about.
            if isinstance(exc, (sqlite3.Error, OSError)):
                raise database_write_error(exc, self.db_path) from exc
            raise
        finally:
            if conn is not None:
                conn.close()

    # ------------------------------------------------------------------
    # Schema and seeding
    # ------------------------------------------------------------------
    def ensure_schema(self) -> None:
        """Create, migrate and seed the availability configuration tables.

        Runs on its own connection with foreign keys disabled, because migrating
        a legacy table means rebuilding it, and SQLite only honours a
        ``foreign_keys`` change outside a transaction.
        """

        conn = self.connect()
        try:
            conn.execute("PRAGMA foreign_keys = OFF")
            conn.execute("PRAGMA journal_mode = DELETE")
            conn.execute("BEGIN IMMEDIATE")
            conn.executescript(_SCHEMA)
            self._migrate_legacy_tables(conn)
            self._seed(conn)
            conn.commit()
        except Exception as exc:
            conn.rollback()
            if isinstance(exc, (sqlite3.Error, OSError)):
                raise database_write_error(exc, self.db_path) from exc
            raise
        finally:
            conn.close()

    def _migrate_legacy_tables(self, conn: sqlite3.Connection) -> None:
        """Bring tables left by the earlier attempt up to the current shape.

        ``CREATE TABLE IF NOT EXISTS`` preserves the *data* in an existing table
        but does nothing about its *schema*, and this feature's schema changed:
        ``updated_at`` was added everywhere, ``availability_settings`` swapped
        ``selected_year``/``utc_offset_hours`` for a timezone name, and
        ``availability_manual_ot`` dropped ``asset_group`` from its key. Without
        this step, a database from the earlier attempt -- the expected state for
        every existing installation -- fails on the first request with
        ``no column named timezone``.

        Any table whose columns differ from the target is rebuilt: create the
        new shape, copy the columns the two have in common, swap it in. That
        also repairs missing UNIQUE constraints, which ``ALTER TABLE ADD COLUMN``
        cannot. Columns that only existed in the old shape are dropped; they
        have no meaning under the current model.
        """

        for table, expected in _EXPECTED_COLUMNS.items():
            actual = [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]
            if not actual or tuple(actual) == expected:
                continue

            shared = [column for column in expected if column in actual]
            staging = f"{table}__migrating"
            conn.execute(f"DROP TABLE IF EXISTS {staging}")
            conn.execute(f"CREATE TABLE {staging} ({_TABLES[table]})")
            if shared:
                columns = ", ".join(shared)
                order_by = _MIGRATION_ORDER_BY.get(table)
                # OR REPLACE, not a plain INSERT: a legacy row set can collide on
                # a unique key the old shape did not have, and losing the whole
                # migration to one duplicate would be worse than keeping the
                # later row.
                conn.execute(
                    f"INSERT OR REPLACE INTO {staging} ({columns}) "
                    f"SELECT {columns} FROM {table}"
                    + (f" ORDER BY {order_by}" if order_by else "")
                )
            conn.execute(f"DROP TABLE {table}")
            conn.execute(f"ALTER TABLE {staging} RENAME TO {table}")

    def _seed(self, conn: sqlite3.Connection) -> None:
        """Populate defaults exactly once, on a database that has never had them.

        Gated on a stored ``seeded_defaults`` flag rather than on the tables
        being empty. Emptiness is not the same as uninitialised: a user who
        deletes every linked-downtime rule has *configured* an empty rule set,
        and a count-based guard would treat that as a fresh database and restore
        all thirteen on the next restart -- silently changing Salvagnini's
        availability to something nobody asked for.

        The flag defaults to 0, so a database from the earlier attempt seeds
        once on upgrade and is authoritative from then on.
        """

        conn.execute(
            "INSERT OR IGNORE INTO availability_settings(id, timezone, updated_at) VALUES (1, ?, ?)",
            (DEFAULT_TIMEZONE, _now()),
        )
        already_seeded = conn.execute(
            "SELECT seeded_defaults FROM availability_settings WHERE id = 1"
        ).fetchone()
        if already_seeded is not None and int(already_seeded[0] or 0):
            return

        if conn.execute("SELECT COUNT(*) FROM availability_asset_groups").fetchone()[0] == 0:
            for order, (name, assets, sched, brk, lunch, setup, include, notes) in enumerate(
                DEFAULT_ASSET_GROUPS, start=1
            ):
                cursor = conn.execute(
                    """
                    INSERT INTO availability_asset_groups
                        (asset_group, schedule_hours_per_day, break_hours_per_day, lunch_hours_per_day,
                         setup_hours_per_day, include_flag, notes, sort_order, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (name, sched, brk, lunch, setup, int(include), notes, order, _now()),
                )
                for asset_order, asset in enumerate(assets, start=1):
                    conn.execute(
                        "INSERT INTO availability_asset_group_assets"
                        "(asset_group_id, asset_number, sort_order) VALUES (?, ?, ?)",
                        (cursor.lastrowid, asset, asset_order),
                    )

        for asset, name in DEFAULT_DISPLAY_NAMES.items():
            conn.execute(
                "INSERT OR IGNORE INTO availability_asset_display_names"
                "(asset_number, display_name, updated_at) VALUES (?, ?, ?)",
                (asset, name, _now()),
            )

        if conn.execute("SELECT COUNT(*) FROM availability_linked_downtime_rules").fetchone()[0] == 0:
            for rule_group, parent, linked, factor in DEFAULT_LINKED_RULES:
                conn.execute(
                    "INSERT OR IGNORE INTO availability_linked_downtime_rules"
                    "(rule_group, parent_asset_number, linked_asset_number, impact_factor, updated_at)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (rule_group, parent, linked, factor, _now()),
                )

        conn.execute("UPDATE availability_settings SET seeded_defaults = 1 WHERE id = 1")

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------
    def load_timezone(self) -> str:
        with self.connect() as conn:
            row = conn.execute("SELECT timezone FROM availability_settings WHERE id = 1").fetchone()
        return (row["timezone"] if row else None) or DEFAULT_TIMEZONE

    def load_groups(self) -> list[AssetGroup]:
        with self.connect() as conn:
            groups = conn.execute(
                "SELECT * FROM availability_asset_groups ORDER BY sort_order, asset_group"
            ).fetchall()
            members: dict[int, list[str]] = {}
            for row in conn.execute(
                "SELECT asset_group_id, asset_number FROM availability_asset_group_assets"
                " ORDER BY asset_group_id, sort_order, asset_number"
            ):
                members.setdefault(row["asset_group_id"], []).append(str(row["asset_number"]).strip())
        return [
            AssetGroup(
                asset_group=row["asset_group"],
                asset_numbers=tuple(members.get(row["id"], [])),
                schedule_hours_per_day=float(row["schedule_hours_per_day"]),
                break_hours_per_day=float(row["break_hours_per_day"]),
                lunch_hours_per_day=float(row["lunch_hours_per_day"]),
                setup_hours_per_day=float(row["setup_hours_per_day"]),
                include=bool(row["include_flag"]),
                notes=row["notes"] or "",
                sort_order=int(row["sort_order"]),
            )
            for row in groups
        ]

    def load_display_names(self) -> dict[str, str]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT asset_number, display_name FROM availability_asset_display_names"
            ).fetchall()
        return {str(r["asset_number"]).strip(): r["display_name"] for r in rows}

    def load_linked_rules(self) -> list[LinkedRule]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT rule_group, parent_asset_number, linked_asset_number, impact_factor"
                " FROM availability_linked_downtime_rules ORDER BY parent_asset_number, linked_asset_number"
            ).fetchall()
        return [
            LinkedRule(
                parent_asset_number=str(r["parent_asset_number"]).strip(),
                linked_asset_number=str(r["linked_asset_number"]).strip(),
                impact_factor=float(r["impact_factor"]),
                rule_group=r["rule_group"] or "",
            )
            for r in rows
        ]

    def load_goals(self) -> dict[tuple[str, date], float]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT asset_group, month_date, goal_percent FROM availability_goal_percent"
            ).fetchall()
        goals: dict[tuple[str, date], float] = {}
        for row in rows:
            try:
                month = date.fromisoformat(str(row["month_date"])[:10])
            except ValueError:
                continue
            goals[(row["asset_group"], date(month.year, month.month, 1))] = float(row["goal_percent"])
        return goals

    def load_manual_ot(self) -> dict[tuple[str, date], float]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT asset_number, month_date, manual_ot_hours FROM availability_manual_ot"
            ).fetchall()
        overtime: dict[tuple[str, date], float] = {}
        for row in rows:
            try:
                month = date.fromisoformat(str(row["month_date"])[:10])
            except ValueError:
                continue
            key = (str(row["asset_number"]).strip(), date(month.year, month.month, 1))
            overtime[key] = float(row["manual_ot_hours"])
        return overtime

    # ------------------------------------------------------------------
    # Work orders
    # ------------------------------------------------------------------
    def _resolve_zone(self, name: str) -> tuple[ZoneInfo | timezone, str | None]:
        """Load the plant timezone, reporting why if it could not be loaded.

        Falling back to UTC keeps the dashboard computable, but it must not be
        silent: UTC bucketing moves work orders created near local midnight into
        the wrong month, and it defeats ``plant_today`` exactly when that guard
        matters -- during the first UTC hours of a new month. A number that is
        quietly wrong is worse than a page that says why.
        """

        try:
            return ZoneInfo(name), None
        except (ZoneInfoNotFoundError, ValueError, KeyError):
            pass

        # ZoneInfo raises the same error for "there is no timezone database" and
        # "that is not a timezone", but the two need opposite remedies: install a
        # package, or fix a typo. Probing a key every database contains tells
        # them apart, so the message names the thing actually worth doing.
        try:
            ZoneInfo("UTC")
        except Exception:
            return timezone.utc, (
                f"The timezone database is unavailable, so '{name}' could not be loaded and months "
                "are being bucketed in UTC. Work orders created near local midnight may land in the "
                "wrong month. Install the 'tzdata' package (pip install -r requirements.txt)."
            )
        return timezone.utc, (
            f"'{name}' is not a recognised timezone, so months are being bucketed in UTC. "
            "Set a valid IANA name such as 'America/Chicago'."
        )

    def _zone(self) -> ZoneInfo | timezone:
        return self._resolve_zone(self.load_timezone())[0]

    def timezone_warning(self) -> str | None:
        """The reason the plant timezone could not be used, if it could not."""

        return self._resolve_zone(self.load_timezone())[1]

    def plant_today(self) -> date:
        """Today's date on the plant's clock, not the server's.

        Work orders are bucketed into months in plant local time, so the
        decision "is this month over yet?" has to be asked on the same clock. A
        UTC server would otherwise call July complete during the first hours of
        August while the plant is still working July 31 -- charting a month
        whose downtime is still arriving against its full scheduled hours, which
        makes availability read high.
        """

        return datetime.now(self._zone()).date()

    @staticmethod
    def _parse_utc(value: object) -> datetime | None:
        """Parse a stored date, treating a naive value as UTC.

        Matches ``LifeDataService._parse_datetime`` so availability buckets work
        orders the same way every other page dates them.
        """

        if value in (None, ""):
            return None
        text = str(value).strip().replace("Z", "+00:00")
        parsed: datetime | None = None
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            for fmt in ("%Y-%m-%d", "%Y-%m-%d %H:%M:%S", "%m/%d/%Y", "%m/%d/%Y %H:%M", "%m/%d/%y", "%m/%d/%y %H:%M"):
                try:
                    parsed = datetime.strptime(text, fmt)
                    break
                except ValueError:
                    continue
        if parsed is None:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    def load_work_orders(self, asset_numbers: list[str] | None = None) -> list[WorkOrder]:
        """Load every work order for the given assets, localized to plant time.

        There is deliberately **no** predicate on ``type_raw``,
        ``record_class_auto``, ``record_class_final`` or ``is_pm_candidate``.
        Availability counts all downtime regardless of what the work order was
        called (design doc §2.1). Rows with zero downtime are kept too -- they
        feed the counts that tell "ran perfectly" apart from "nobody logged
        anything".
        """

        zone = self._zone()
        rows = self._fetch_work_order_rows(asset_numbers, self._OPTIONAL_MAPPED_COLUMNS)
        orders = (self._build_order(row, zone) for row in rows)
        return [order for order in orders if order is not None]

    def load_work_order_details(
        self, asset_numbers: list[str] | None, month: date | None = None
    ) -> list[WorkOrderDetail]:
        """The same work orders, carrying the text the drill-down displays.

        Split from :meth:`load_work_orders` rather than folded into it because
        the hot path runs on every page view and wants none of this: the card
        loads ~2,000 rows to draw nine charts, and their names, descriptions and
        completion notes are read only when someone opens one bar.

        ``month`` filters *after* localization, matching how the chart buckets.
        The month a work order belongs to is decided in plant local time, and
        the stored created dates arrive in several formats, so the same
        predicate cannot be pushed into SQL without changing which month a
        boundary-crossing order lands in.
        """

        zone = self._zone()
        rows = self._fetch_work_order_rows(
            asset_numbers, self._OPTIONAL_MAPPED_COLUMNS + self._DETAIL_MAPPED_COLUMNS
        )
        details: list[WorkOrderDetail] = []
        for row in rows:
            order = self._build_order(row, zone)
            if order is None:
                continue
            if month is not None and (order.created_local.year, order.created_local.month) != (
                month.year,
                month.month,
            ):
                continue
            details.append(
                WorkOrderDetail(
                    order=order,
                    task_name=self._text(row, "task_name"),
                    status=self._text(row, "status_raw"),
                    type_raw=self._text(row, "type_raw"),
                    # The disposition screen's final call wins over the
                    # classifier's guess, the same precedence every other page
                    # applies -- and neither is allowed to affect the arithmetic
                    # above it (§2.1); it is shown so a reader can see what a row
                    # was called, not so anything can filter on it.
                    record_class=self._text(row, "record_class_final", "record_class_auto"),
                    asset_name=self._text(row, "asset_name"),
                    description=self._text(
                        row, "description_raw", "requestor_description", "request_title"
                    ),
                    completion_notes=self._text(row, "completion_notes"),
                )
            )
        return details

    def _fetch_work_order_rows(
        self, asset_numbers: list[str] | None, columns: tuple[str, ...]
    ) -> list[sqlite3.Row]:
        """Raw mapped rows for the given assets, selecting only ``columns``."""

        if not self._table_exists("mapped_cmms_record"):
            return []

        with self.connect() as conn:
            present = self._mapped_columns(conn)
            if "asset_number" not in present:
                return []
            select = ", ".join(
                ["TRIM(asset_number) AS asset_number"] + self._select_expressions(present, columns)
            )
            sql = [
                f"SELECT {select}",
                "FROM mapped_cmms_record",
                "WHERE asset_number IS NOT NULL AND TRIM(asset_number) <> ''",
            ]
            params: list[str] = []
            wanted = [str(a).strip() for a in (asset_numbers or []) if str(a).strip()]
            if wanted:
                sql.append(f"AND TRIM(asset_number) IN ({','.join('?' for _ in wanted)})")
                params.extend(wanted)
            return conn.execute("\n".join(sql), params).fetchall()

    def _build_order(self, row: sqlite3.Row, zone: ZoneInfo | timezone) -> WorkOrder | None:
        """One mapped row as a localized ``WorkOrder``, or ``None`` if undated."""

        created = self._parse_utc(
            row["created_date_final"] or row["created_datetime_raw"] or row["created_date_raw"]
        )
        if created is None:
            return None
        completed = self._parse_utc(
            row["completed_date_final"] or row["completed_datetime_raw"] or row["completed_date_raw"]
        )
        return WorkOrder(
            asset_number=str(row["asset_number"]).strip(),
            created_local=created.astimezone(zone).replace(tzinfo=None),
            downtime_hours=self._downtime_hours(row),
            completed_local=(completed.astimezone(zone).replace(tzinfo=None) if completed else None),
            task_id=str(row["task_id"] or ""),
        )

    @staticmethod
    def _text(row: sqlite3.Row, *columns: str) -> str:
        """The first of ``columns`` this row actually filled in.

        Limble populates a different description field depending on whether a
        work order began life as a request, so the useful text is wherever it
        landed rather than in one fixed column.
        """

        for column in columns:
            value = row[column]
            if value is None:
                continue
            text = str(value).strip()
            if text:
                return text
        return ""

    @staticmethod
    def _downtime_hours(row: sqlite3.Row) -> float:
        """Prefer the stored hours, falling back to minutes.

        ``downtime_minutes`` is the column the mapper always writes;
        ``downtime_hours`` is derived from it. Reading hours first and minutes
        second keeps a row usable when either is missing.
        """

        hours = row["downtime_hours"]
        if hours is not None:
            try:
                return float(hours)
            except (TypeError, ValueError):
                pass
        minutes = row["downtime_minutes"]
        try:
            return float(minutes) / 60.0
        except (TypeError, ValueError):
            return 0.0

    # Columns this module reads from mapped_cmms_record. A long-lived GREMLIN.db
    # only ever gains columns, so one that predates a migration is missing some
    # of these -- and the availability card is now the Metrics page's first
    # request, so it can be the one that meets an un-migrated database. Selecting
    # them unconditionally turns that into a 500; substituting NULL degrades to
    # the fallbacks these fields already have. Same approach RawRepository takes.
    _OPTIONAL_MAPPED_COLUMNS = (
        "created_date_final", "created_datetime_raw", "created_date_raw",
        "completed_date_final", "completed_datetime_raw", "completed_date_raw",
        "downtime_minutes", "downtime_hours", "task_id",
    )

    # Read only by the per-bar drill-down, which loads one asset-month at a
    # time. Kept out of the list above so the nine-chart request kept on the
    # hot path never carries this much text.
    _DETAIL_MAPPED_COLUMNS = (
        "task_name", "status_raw", "type_raw", "asset_name",
        "description_raw", "requestor_description", "request_title",
        "completion_notes", "record_class_final", "record_class_auto",
    )

    def _mapped_columns(self, conn: sqlite3.Connection) -> set[str]:
        return {row[1] for row in conn.execute("PRAGMA table_info(mapped_cmms_record)")}

    @staticmethod
    def _select_expressions(present: set[str], columns: tuple[str, ...]) -> list[str]:
        return [column if column in present else f"NULL AS {column}" for column in columns]

    @staticmethod
    def _created_expression(present: set[str]) -> str | None:
        """COALESCE over whichever created-date columns this database has."""

        candidates = [
            f"NULLIF(TRIM({column}), '')"
            for column in ("created_date_final", "created_datetime_raw", "created_date_raw")
            if column in present
        ]
        if not candidates:
            return None
        return f"COALESCE({', '.join(candidates)})"

    def has_any_work_orders(self) -> bool:
        """Whether the database holds any dated work order at all.

        Separates "nothing has been imported" from "nothing for the assets this
        dashboard is configured to chart" -- two empty screens that need
        different explanations. Deliberately an existence check rather than a
        count or a load, so it stays cheap enough to run on every request.
        """

        if not self._table_exists("mapped_cmms_record"):
            return False
        with self.connect() as conn:
            present = self._mapped_columns(conn)
            created = self._created_expression(present)
            if "asset_number" not in present or created is None:
                return False
            row = conn.execute(
                f"""
                SELECT 1 FROM mapped_cmms_record
                WHERE asset_number IS NOT NULL AND TRIM(asset_number) <> ''
                  AND {created} IS NOT NULL
                LIMIT 1
                """
            ).fetchone()
        return row is not None

    def data_extent(self, asset_numbers: list[str] | None = None) -> tuple[date | None, date | None]:
        """Earliest and latest month that holds a work order, in plant time."""

        orders = self.load_work_orders(asset_numbers)
        if not orders:
            return None, None
        months = [date(o.created_local.year, o.created_local.month, 1) for o in orders]
        return min(months), max(months)

    def _table_exists(self, table: str) -> bool:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()
        return row is not None

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------
    def save_group_schedule(
        self,
        asset_group: str,
        *,
        schedule_hours_per_day: float,
        break_hours_per_day: float,
        lunch_hours_per_day: float,
        setup_hours_per_day: float,
        include: bool,
        asset_numbers: list[str] | None = None,
    ) -> None:
        """Update one group's shift schedule, and optionally its membership.

        Net hours per day is never written -- it is derived from these parts
        every time it is read, so a stored net cannot drift from them.
        """

        parts = {
            "schedule hours": schedule_hours_per_day,
            "break hours": break_hours_per_day,
            "lunch hours": lunch_hours_per_day,
            "setup hours": setup_hours_per_day,
        }
        for label, value in parts.items():
            if value < 0:
                raise AvailabilityConfigError(f"{label.capitalize()} cannot be negative.")
            if value > 24:
                raise AvailabilityConfigError(f"{label.capitalize()} cannot exceed 24 per day.")
        deductions = break_hours_per_day + lunch_hours_per_day + setup_hours_per_day
        if deductions > schedule_hours_per_day:
            raise AvailabilityConfigError(
                f"Break, lunch and setup total {deductions:g} h, which is more than the "
                f"{schedule_hours_per_day:g} h scheduled — net hours would be negative."
            )

        with self.write_connection() as conn:
            row = conn.execute(
                "SELECT id FROM availability_asset_groups WHERE asset_group = ?", (asset_group,)
            ).fetchone()
            if row is None:
                raise AvailabilityConfigError(f"Unknown asset group: {asset_group}")
            conn.execute(
                """
                UPDATE availability_asset_groups
                   SET schedule_hours_per_day = ?, break_hours_per_day = ?, lunch_hours_per_day = ?,
                       setup_hours_per_day = ?, include_flag = ?, updated_at = ?
                 WHERE id = ?
                """,
                (schedule_hours_per_day, break_hours_per_day, lunch_hours_per_day,
                 setup_hours_per_day, int(include), _now(), row["id"]),
            )
            if asset_numbers is not None:
                conn.execute(
                    "DELETE FROM availability_asset_group_assets WHERE asset_group_id = ?", (row["id"],)
                )
                seen: set[str] = set()
                order = 0
                for asset in asset_numbers:
                    asset = str(asset).strip()
                    if not asset or asset in seen:
                        continue
                    seen.add(asset)
                    order += 1
                    conn.execute(
                        "INSERT INTO availability_asset_group_assets"
                        "(asset_group_id, asset_number, sort_order) VALUES (?, ?, ?)",
                        (row["id"], asset, order),
                    )

    def save_goal(self, asset_group: str, month: date | str, goal_percent: float) -> None:
        if not 0 <= goal_percent <= 1:
            raise AvailabilityConfigError("Goal must be between 0% and 100%.")
        with self.write_connection() as conn:
            conn.execute(
                """
                INSERT INTO availability_goal_percent(asset_group, month_date, goal_percent, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(asset_group, month_date)
                DO UPDATE SET goal_percent = excluded.goal_percent, updated_at = excluded.updated_at
                """,
                (asset_group, _month_text(month), goal_percent, _now()),
            )

    def save_manual_ot(self, asset_number: str, month: date | str, hours: float) -> None:
        if hours < 0:
            raise AvailabilityConfigError("Overtime hours cannot be negative.")
        with self.write_connection() as conn:
            conn.execute(
                """
                INSERT INTO availability_manual_ot(asset_number, month_date, manual_ot_hours, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(asset_number, month_date)
                DO UPDATE SET manual_ot_hours = excluded.manual_ot_hours, updated_at = excluded.updated_at
                """,
                (str(asset_number).strip(), _month_text(month), hours, _now()),
            )

    def save_display_name(self, asset_number: str, display_name: str) -> None:
        name = str(display_name).strip()
        if not name:
            raise AvailabilityConfigError("Display name cannot be blank.")
        with self.write_connection() as conn:
            conn.execute(
                """
                INSERT INTO availability_asset_display_names(asset_number, display_name, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(asset_number)
                DO UPDATE SET display_name = excluded.display_name, updated_at = excluded.updated_at
                """,
                (str(asset_number).strip(), name, _now()),
            )

    def replace_linked_rules(self, rules: list[dict]) -> None:
        """Replace the whole linked-downtime rule set in one transaction."""

        cleaned: list[tuple[str, str, str, float]] = []
        for rule in rules:
            parent = str(rule.get("parent_asset_number", "")).strip()
            linked = str(rule.get("linked_asset_number", "")).strip()
            if not parent or not linked:
                raise AvailabilityConfigError("Every rule needs a parent and a linked asset number.")
            if parent == linked:
                raise AvailabilityConfigError(f"Asset {parent} cannot be linked to itself.")
            try:
                factor = float(rule.get("impact_factor", 0.5))
            except (TypeError, ValueError) as exc:
                raise AvailabilityConfigError("Impact factor must be a number.") from exc
            if not 0 <= factor <= 1:
                raise AvailabilityConfigError("Impact factor must be between 0 and 1.")
            cleaned.append((str(rule.get("rule_group", "") or ""), parent, linked, factor))

        with self.write_connection() as conn:
            conn.execute("DELETE FROM availability_linked_downtime_rules")
            for rule_group, parent, linked, factor in cleaned:
                conn.execute(
                    "INSERT OR REPLACE INTO availability_linked_downtime_rules"
                    "(rule_group, parent_asset_number, linked_asset_number, impact_factor, updated_at)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (rule_group, parent, linked, factor, _now()),
                )

    def reset_group_to_defaults(self, asset_group: str) -> None:
        """Restore one group's seeded schedule and membership."""

        seed = next((g for g in DEFAULT_ASSET_GROUPS if g[0] == asset_group), None)
        if seed is None:
            raise AvailabilityConfigError(f"No default configuration for: {asset_group}")
        _, assets, sched, brk, lunch, setup, include, _notes = seed
        self.save_group_schedule(
            asset_group,
            schedule_hours_per_day=float(sched),
            break_hours_per_day=float(brk),
            lunch_hours_per_day=float(lunch),
            setup_hours_per_day=float(setup),
            include=bool(include),
            asset_numbers=list(assets),
        )
