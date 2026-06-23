"""SQLite-backed Life Data Analysis workflow services for GREMLIN.

This module extends the existing ``GREMLIN.db`` raw import database in-place.
It never modifies ``raw_cmms_record.raw_json``; instead, raw JSON is parsed into
REL-style mapped, disposition, event-processing, and Weibull analysis tables.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import string
import threading
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from contextlib import contextmanager
from typing import Any, Iterable, Iterator
from xml.sax.saxutils import escape
import xml.etree.ElementTree as ET

ROOT_DIR = Path(__file__).resolve().parents[1]

_DEFAULT_DB_RELATIVE_PATHS = (
    # Drives mapped at the \\sandc.ws\depts share root reach the database through
    # the Facilities\FACIL\... folder chain. Probe this first so a mapped drive
    # resolves to the real shared location instead of an empty stub path.
    r"Facilities\FACIL\MAIN-ENG\901 Reliability Projects\901 Reliability Projects\Weibull Data\Database\GREMLIN.db",
    # Drives mapped directly at the "901 Reliability Projects" project folder.
    r"Weibull Data\\Database\\GREMLIN.db",
)

_DEFAULT_DB_UNC_PATHS = (
    r"\\sandc.ws\depts\Facilities\FACIL\MAIN-ENG\901 Reliability Projects\901 Reliability Projects\Weibull Data\Database\GREMLIN.db",
    r"Weibull Data\\Database\\GREMLIN.db"
)


def _drive_letters_in_lookup_order() -> tuple[str, ...]:
    """Return mapped-drive letters with Z first, then the remaining alphabet."""

    return ("Z",) + tuple(letter for letter in string.ascii_uppercase if letter != "Z")


def _build_default_db_path_candidates() -> tuple[Path, ...]:
        """Build every supported shared database location."""

        candidates: list[Path] = []
        seen: set[str] = set()

        def append_candidate(candidate: Path) -> None:
            candidate_key = str(candidate).casefold()
            if candidate_key not in seen:
                candidates.append(candidate)
                seen.add(candidate_key)

        # ✅ ADD YOUR PATH FIRST (highest priority)
        append_candidate(Path(
            r"\\sandc.ws\depts\Facilities\FACIL\MAIN-ENG\901 Reliability Projects\901 Reliability Projects\Weibull Data\Database\GREMLIN.db"
        ))

        # Existing logic (unchanged)
        for drive_letter in _drive_letters_in_lookup_order():
            for relative_path in _DEFAULT_DB_RELATIVE_PATHS:
                append_candidate(Path(f"{drive_letter}:\\{relative_path}"))

        for unc_path in _DEFAULT_DB_UNC_PATHS:
            append_candidate(Path(unc_path))

        return tuple(candidates)



DEFAULT_DB_PATH_CANDIDATES = _build_default_db_path_candidates()
DEFAULT_DB_PATH = DEFAULT_DB_PATH_CANDIDATES[1]
DB_WRITE_TIMEOUT_SECONDS = 30
_DEFAULT_DB_PATH_SENTINEL = object()
_LOCK_WAIT_CONTEXT = threading.local()


def resolve_default_db_path(requested_path: Path | str = DEFAULT_DB_PATH) -> Path:
    """Return the first reachable GREMLIN.db path among supported shared locations.

    GREMLIN historically used the Z: mapped drive, but some Windows profiles map
    the same share under a different drive letter, some include an additional
    ``Facilities`` folder segment, and some reach the database through the
    ``\\\\sandc.ws\\depts`` UNC share.  When the requested path is one of the known
    shared GREMLIN defaults, use the first candidate that already contains the
    database file so SQLite does not create a new empty database on the wrong
    mapped drive.  If none are reachable, fall back to the requested/default
    path so the normal startup error can explain that the share is unavailable.
    Probe errors from inaccessible mapped drives are treated as unavailable
    candidates so later mapped drives can still be discovered.
    """

    requested = Path(requested_path)
    default_candidate_keys = {str(candidate).casefold() for candidate in DEFAULT_DB_PATH_CANDIDATES}
    if str(requested).casefold() not in default_candidate_keys:
        return requested

    for candidate in DEFAULT_DB_PATH_CANDIDATES:
        try:
            if candidate.is_file():
                return candidate
        except OSError:
            continue
    return requested


@contextmanager
def database_lock_wait_callback(callback: Any) -> Iterator[None]:
    """Temporarily notify a caller when a write waits on SQLite's busy timeout."""

    previous = getattr(_LOCK_WAIT_CONTEXT, "callback", None)
    _LOCK_WAIT_CONTEXT.callback = callback
    try:
        yield
    finally:
        _LOCK_WAIT_CONTEXT.callback = previous


class ClosingSqliteConnection(sqlite3.Connection):
    """SQLite connection that closes when used as a context manager.

    The standard sqlite3.Connection context manager commits or rolls back but
    leaves the database handle open. GREMLIN opens many short-lived read handles
    from GUI actions, so closing on context exit prevents stale handles from
    lingering after large Excel disposition imports.
    """

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> bool:
        try:
            return bool(super().__exit__(exc_type, exc_value, traceback))
        finally:
            self.close()


class DatabaseWriteError(RuntimeError):
    """User-facing error raised when GREMLIN cannot safely write to SQLite."""


RECORD_CLASSES = (
    "CORRECTIVE_WO",
    "PM",
    "PM_RESET_CANDIDATE",
    "INSPECTION",
    "PARTS_ORDER",
    "ADMINISTRATIVE",
    "PROJECT_WORK",
    "UNKNOWN",
)

WO_DISPOSITION_CATEGORIES = (
    "INCLUDED_FAILURE",
    "INCLUDED_CENSORED_ASSET_EVENT",
    "EXCLUDED_NON_FAILURE",
    "HELD_AMBIGUOUS",
    "EXCLUDED_MIXED_CONTAMINATING",
    "UNKNOWN",
)

PM_DISPOSITION_CATEGORIES = (
    "INCLUDED_PM_RESET_EVENT",
    "PM_CONTEXT_ONLY",
    "REJECTED_PM_RESET",
    "HELD_AMBIGUOUS",
    "EXCLUDED_NON_FAILURE",
    "UNKNOWN",
)

PM_RESET_DECISIONS = ("APPROVED_RESET", "REJECTED_RESET", "CONTEXT_ONLY", "NEEDS_REVIEW")


@dataclass(frozen=True)
class ExcelValidation:
    """A simple Excel data-validation rule for one exported worksheet column."""

    column_name: str
    validation_type: str
    formula1: str
    operator: str | None = None
    allow_blank: bool = True
    show_error: bool = True
    error_title: str = "Invalid value"
    error: str = "Choose a value from the dropdown or enter a valid value."


DISPLAY_COLUMNS = (
    "name",
    "taskID",
    "createdDate_Final",
    "completedDate_Final",
    "weibullEventDate_Final",
    "weibullEventDate_Source",
    "downtime",
    "completionNotes",
    "requestTitle",
    "requestorDescription",
)

EXCEL_BASE_COLUMNS = ("mapped_record_id",) + DISPLAY_COLUMNS
EXCEL_COMMON_DISPOSITION_COLUMNS = (
    "disposition_notes",
    "disposition_category",
    "record_class",
    "include_in_weibull_candidate",
)
EXCEL_WO_DISPOSITION_COLUMNS = EXCEL_BASE_COLUMNS + EXCEL_COMMON_DISPOSITION_COLUMNS + (
    "failure_mode_id",
    "failure_mode",
    "failure_mechanism_id",
    "failure_mechanism",
)
EXCEL_PM_DISPOSITION_COLUMNS = EXCEL_BASE_COLUMNS + EXCEL_COMMON_DISPOSITION_COLUMNS + (
    "pm_reset_decision",
    "reset_target_failure_mode_id",
    "reset_target_failure_mode",
    "reset_target_failure_mechanism_id",
    "reset_target_failure_mechanism",
    "pm_reset_renewal_rationale",
)


@dataclass(frozen=True)
class SummaryMetrics:
    total_entries: int = 0
    usable_wos_for_weibull: int = 0
    usable_pms_for_weibull: int = 0
    wos_dispositioned: int = 0
    wos_not_dispositioned: int = 0
    pms_dispositioned: int = 0
    pms_not_dispositioned: int = 0


@dataclass(frozen=True)
class AnalysisResultView:
    run_id: int
    result_id: int
    beta_mle: float
    eta_mle: float
    failure_count: int
    censored_count: int
    total_observation_count: int
    km_points: list[dict[str, float | int | None]]
    curve_points: list[dict[str, float | None]]
    observations: list[dict[str, float | int | str | None]]
    analysis_label: str = ""
    grouping_level: str = ""
    beta_lower_ci: float | None = None
    beta_upper_ci: float | None = None
    eta_lower_ci: float | None = None
    eta_upper_ci: float | None = None
    mean_time_to_failure: float | None = None
    interpretation_summary: list[dict[str, str]] | None = None


class LifeDataService:
    """Owns GREMLIN.db schema, CMMS mapping, disposition, and Weibull analysis."""

    def __init__(self, db_path: Path | str | object = _DEFAULT_DB_PATH_SENTINEL, *, refresh_on_startup: bool = True) -> None:
        if db_path is _DEFAULT_DB_PATH_SENTINEL:
            # Pick the first reachable shared candidate (UNC or any mapped drive)
            # instead of opening only the UNC path, so default/session reuse works
            # on profiles where the share is mapped to a drive letter.
            self.db_path = resolve_default_db_path(DEFAULT_DB_PATH)
        else:
            self.db_path = Path(db_path)
        self._asset_number_options_cache: list[dict[str, str]] | None = None
        self.ensure_schema()
        if refresh_on_startup:
            self.refresh_mapped_cmms_records()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=DB_WRITE_TIMEOUT_SECONDS, factory=ClosingSqliteConnection)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(f"PRAGMA busy_timeout = {DB_WRITE_TIMEOUT_SECONDS * 1000}")
        conn.execute("PRAGMA locking_mode = NORMAL")
        conn.execute("PRAGMA synchronous = FULL")
        conn.execute("PRAGMA temp_store = MEMORY")
        conn.execute("PRAGMA cache_size = -65536")
        return conn

    @contextmanager
    def write_connection(self) -> Iterator[sqlite3.Connection]:
        """Open a serialized write transaction suitable for a shared SQLite file.

        SQLite only permits one writer at a time. ``BEGIN IMMEDIATE`` reserves
        the writer slot before GREMLIN computes and updates rows, while the
        busy timeout gives another user's write time to finish instead of
        failing immediately. Rollback-journal mode is used because the database
        is expected to live on a shared drive where WAL files are unsafe across
        many network filesystems.
        """

        conn: sqlite3.Connection | None = None
        try:
            conn = self.connect()
            conn.execute("PRAGMA journal_mode = DELETE")
            self._begin_write_transaction(conn)
            yield conn
            conn.commit()
        except Exception as exc:
            if conn is not None:
                conn.rollback()
            if isinstance(exc, (sqlite3.Error, OSError)):
                raise self._database_write_error(exc) from exc
            raise
        finally:
            if conn is not None:
                conn.close()


    def _begin_write_transaction(self, conn: sqlite3.Connection) -> None:
        """Start a write transaction and report when SQLite enters busy-timeout waiting."""

        try:
            conn.execute("PRAGMA busy_timeout = 0")
            conn.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as exc:
            reason = str(exc).lower()
            if "locked" not in reason and "busy" not in reason:
                raise
            conn.rollback()
            self._notify_database_lock_wait()
            conn.execute(f"PRAGMA busy_timeout = {DB_WRITE_TIMEOUT_SECONDS * 1000}")
            conn.execute("BEGIN IMMEDIATE")
        else:
            conn.execute(f"PRAGMA busy_timeout = {DB_WRITE_TIMEOUT_SECONDS * 1000}")

    def _notify_database_lock_wait(self) -> None:
        callback = getattr(_LOCK_WAIT_CONTEXT, "callback", None)
        if callback is None:
            return
        try:
            callback()
        except Exception:
            return

    def _database_write_error(self, exc: sqlite3.Error | OSError) -> DatabaseWriteError:
        raw_reason = str(exc).strip() or exc.__class__.__name__
        reason_lower = raw_reason.lower()
        if "locked" in reason_lower or "busy" in reason_lower:
            reason = (
                f"another GREMLIN user or program is writing to the shared database and it stayed locked "
                f"for more than {DB_WRITE_TIMEOUT_SECONDS} seconds"
            )
            action = "Wait a moment, then try saving again. If this keeps happening, ask other users to finish their save first."
        elif "readonly" in reason_lower or "permission" in reason_lower or "access" in reason_lower:
            reason = "your Windows account does not have permission to write to the shared database or its folder"
            action = "Confirm you can edit files in the shared folder, then reopen GREMLIN."
        elif "unable to open" in reason_lower or "no such file" in reason_lower or "path" in reason_lower:
            reason = "GREMLIN could not open the shared database path"
            action = "Confirm one of the mapped drive letters can reach the shared database folder."
        elif "disk" in reason_lower or "space" in reason_lower or "full" in reason_lower:
            reason = "the shared drive may be out of space or unavailable"
            action = "Check the shared drive status and free space, then try again."
        else:
            reason = raw_reason
            action = "Try again. If it repeats, send this message to the GREMLIN maintainer."
        return DatabaseWriteError(
            "GREMLIN could not write to the shared database.\n\n"
            f"Database: {self.db_path}\n"
            f"Reason: {reason}.\n"
            f"What to do: {action}"
        )

    def ensure_schema(self) -> None:
        """Create all downstream REL-compliant tables in the existing GREMLIN.db."""

        with self.write_connection() as conn:
            self._migrate_rel_disposition_schema(conn)
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS mapped_cmms_record (
                    mapped_record_id INTEGER PRIMARY KEY,
                    raw_record_id INTEGER NOT NULL,
                    raw_content_hash TEXT,
                    import_batch_id INTEGER NOT NULL,
                    source_system TEXT NOT NULL DEFAULT 'Limble',
                    task_id TEXT,
                    task_name TEXT,
                    template_raw TEXT,
                    type_raw TEXT,
                    associated_task_id TEXT,
                    status_raw TEXT,
                    status_id_raw TEXT,
                    asset_id_raw TEXT,
                    asset_name TEXT,
                    asset_number TEXT,
                    immediate_parent_asset_id TEXT,
                    immediate_parent_asset_name TEXT,
                    root_asset_id TEXT,
                    root_asset_name TEXT,
                    wo_asset_level TEXT,
                    asset_has_children_raw TEXT,
                    created_date_raw TEXT,
                    created_datetime_raw TEXT,
                    created_date_final TEXT,
                    start_date_raw TEXT,
                    start_datetime_raw TEXT,
                    start_date_final TEXT,
                    due_date_raw TEXT,
                    due_datetime_raw TEXT,
                    due_date_final TEXT,
                    completed_date_raw TEXT,
                    completed_datetime_raw TEXT,
                    completed_date_final TEXT,
                    completion_notes TEXT,
                    requestor_description TEXT,
                    request_title TEXT,
                    description_raw TEXT,
                    custom_tags_json TEXT,
                    po_ids_json TEXT,
                    downtime_raw TEXT,
                    downtime_minutes REAL,
                    downtime_hours REAL,
                    downtime_backfill_attempted INTEGER NOT NULL DEFAULT 0,
                    record_class_auto TEXT NOT NULL DEFAULT 'UNKNOWN',
                    record_class_final TEXT,
                    classification_reason TEXT,
                    is_pm_candidate INTEGER NOT NULL DEFAULT 0,
                    is_corrective_wo_candidate INTEGER NOT NULL DEFAULT 0,
                    is_purchase_order_related INTEGER NOT NULL DEFAULT 0,
                    is_completed INTEGER NOT NULL DEFAULT 0,
                    mapped_at TEXT NOT NULL DEFAULT (datetime('now')),
                    mapping_version TEXT NOT NULL DEFAULT 'v1',
                    FOREIGN KEY (raw_record_id) REFERENCES raw_cmms_record(raw_record_id) ON UPDATE CASCADE ON DELETE RESTRICT,
                    FOREIGN KEY (import_batch_id) REFERENCES import_batch(import_batch_id) ON UPDATE CASCADE ON DELETE RESTRICT,
                    UNIQUE (raw_record_id)
                );
                CREATE INDEX IF NOT EXISTS idx_mapped_cmms_raw_record ON mapped_cmms_record(raw_record_id);
                CREATE INDEX IF NOT EXISTS idx_mapped_cmms_raw_hash ON mapped_cmms_record(raw_content_hash);
                CREATE INDEX IF NOT EXISTS idx_mapped_cmms_import_batch ON mapped_cmms_record(import_batch_id);
                CREATE INDEX IF NOT EXISTS idx_mapped_cmms_asset_number ON mapped_cmms_record(asset_number);
                CREATE INDEX IF NOT EXISTS idx_mapped_cmms_task_id ON mapped_cmms_record(task_id);
                CREATE INDEX IF NOT EXISTS idx_mapped_cmms_record_class_auto ON mapped_cmms_record(record_class_auto);
                CREATE INDEX IF NOT EXISTS idx_mapped_cmms_record_class_final ON mapped_cmms_record(record_class_final);
                CREATE INDEX IF NOT EXISTS idx_mapped_cmms_pm_candidate ON mapped_cmms_record(is_pm_candidate);
                CREATE INDEX IF NOT EXISTS idx_mapped_cmms_corrective_candidate ON mapped_cmms_record(is_corrective_wo_candidate);
                CREATE INDEX IF NOT EXISTS idx_mapped_cmms_completed_date ON mapped_cmms_record(completed_date_final);
                CREATE INDEX IF NOT EXISTS idx_mapped_cmms_asset_class_final ON mapped_cmms_record(asset_number, record_class_final);
                CREATE INDEX IF NOT EXISTS idx_mapped_cmms_asset_class_auto ON mapped_cmms_record(asset_number, record_class_auto);
                CREATE INDEX IF NOT EXISTS idx_mapped_cmms_asset_pm_candidate ON mapped_cmms_record(asset_number, is_pm_candidate);
                CREATE INDEX IF NOT EXISTS idx_mapped_cmms_asset_corrective_candidate ON mapped_cmms_record(asset_number, is_corrective_wo_candidate);
                CREATE INDEX IF NOT EXISTS idx_mapped_cmms_asset_dates ON mapped_cmms_record(asset_number, completed_date_final, start_date_final, created_date_final, task_id);

                CREATE TABLE IF NOT EXISTS failure_mode (
                    failure_mode_id INTEGER PRIMARY KEY,
                    failure_mode_name TEXT NOT NULL UNIQUE,
                    description TEXT,
                    is_active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL DEFAULT (datetime('now'))
                );
                CREATE INDEX IF NOT EXISTS idx_failure_mode_active ON failure_mode(is_active);

                CREATE TABLE IF NOT EXISTS failure_mechanism (
                    failure_mechanism_id INTEGER PRIMARY KEY,
                    failure_mechanism_name TEXT NOT NULL,
                    failure_mode_id INTEGER,
                    description TEXT,
                    is_active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL DEFAULT (datetime('now')),
                    FOREIGN KEY (failure_mode_id) REFERENCES failure_mode(failure_mode_id)
                );
                CREATE INDEX IF NOT EXISTS idx_failure_mechanism_active ON failure_mechanism(is_active);

                CREATE TABLE IF NOT EXISTS modeled_population (
                    modeled_population_id INTEGER PRIMARY KEY,
                    population_name TEXT NOT NULL,
                    asset_number TEXT,
                    asset_name TEXT,
                    failure_mode_id INTEGER,
                    failure_mechanism_id INTEGER,
                    grouping_level_used TEXT NOT NULL DEFAULT 'UNKNOWN' CHECK (grouping_level_used IN ('FAILURE_MODE','FAILURE_MECHANISM','ASSET_ONLY','UNKNOWN')),
                    population_definition TEXT,
                    fallback_rationale TEXT,
                    consistency_notes TEXT,
                    is_approved INTEGER NOT NULL DEFAULT 0,
                    approved_by_user_id INTEGER,
                    approved_at TEXT,
                    created_at TEXT NOT NULL DEFAULT (datetime('now')),
                    FOREIGN KEY (failure_mode_id) REFERENCES failure_mode(failure_mode_id),
                    FOREIGN KEY (failure_mechanism_id) REFERENCES failure_mechanism(failure_mechanism_id)
                );
                CREATE INDEX IF NOT EXISTS idx_modeled_population_asset_number ON modeled_population(asset_number);
                CREATE INDEX IF NOT EXISTS idx_modeled_population_failure_mode ON modeled_population(failure_mode_id);
                CREATE INDEX IF NOT EXISTS idx_modeled_population_failure_mechanism ON modeled_population(failure_mechanism_id);

                CREATE TABLE IF NOT EXISTS asset_failure_mode_option (
                    asset_failure_mode_option_id INTEGER PRIMARY KEY,
                    asset_number TEXT NOT NULL,
                    failure_mode_id INTEGER NOT NULL,
                    first_source_event_disposition_id INTEGER,
                    last_used_event_disposition_id INTEGER,
                    use_count INTEGER NOT NULL DEFAULT 1,
                    is_active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL DEFAULT (datetime('now')),
                    last_used_at TEXT NOT NULL DEFAULT (datetime('now')),
                    FOREIGN KEY (failure_mode_id) REFERENCES failure_mode(failure_mode_id),
                    FOREIGN KEY (first_source_event_disposition_id) REFERENCES event_disposition(event_disposition_id),
                    FOREIGN KEY (last_used_event_disposition_id) REFERENCES event_disposition(event_disposition_id),
                    UNIQUE (asset_number, failure_mode_id)
                );
                CREATE INDEX IF NOT EXISTS idx_asset_failure_mode_option_asset ON asset_failure_mode_option(asset_number);
                CREATE INDEX IF NOT EXISTS idx_asset_failure_mode_option_mode ON asset_failure_mode_option(failure_mode_id);

                CREATE TABLE IF NOT EXISTS asset_failure_mechanism_option (
                    asset_failure_mechanism_option_id INTEGER PRIMARY KEY,
                    asset_number TEXT NOT NULL,
                    failure_mechanism_id INTEGER NOT NULL,
                    failure_mode_id INTEGER,
                    first_source_event_disposition_id INTEGER,
                    last_used_event_disposition_id INTEGER,
                    use_count INTEGER NOT NULL DEFAULT 1,
                    is_active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL DEFAULT (datetime('now')),
                    last_used_at TEXT NOT NULL DEFAULT (datetime('now')),
                    FOREIGN KEY (failure_mechanism_id) REFERENCES failure_mechanism(failure_mechanism_id),
                    FOREIGN KEY (failure_mode_id) REFERENCES failure_mode(failure_mode_id),
                    FOREIGN KEY (first_source_event_disposition_id) REFERENCES event_disposition(event_disposition_id),
                    FOREIGN KEY (last_used_event_disposition_id) REFERENCES event_disposition(event_disposition_id),
                    UNIQUE (asset_number, failure_mechanism_id)
                );
                CREATE INDEX IF NOT EXISTS idx_asset_failure_mechanism_option_asset ON asset_failure_mechanism_option(asset_number);
                CREATE INDEX IF NOT EXISTS idx_asset_failure_mechanism_option_mechanism ON asset_failure_mechanism_option(failure_mechanism_id);
                CREATE INDEX IF NOT EXISTS idx_asset_failure_mechanism_option_mode ON asset_failure_mechanism_option(failure_mode_id);

                CREATE TABLE IF NOT EXISTS event_disposition (
                    event_disposition_id INTEGER PRIMARY KEY,
                    mapped_record_id INTEGER NOT NULL,
                    modeled_population_id INTEGER,
                    record_class_final TEXT CHECK (record_class_final IS NULL OR record_class_final IN ('CORRECTIVE_WO','PM','PM_RESET_CANDIDATE','INSPECTION','PARTS_ORDER','ADMINISTRATIVE','PROJECT_WORK','UNKNOWN')),
                    disposition_category TEXT NOT NULL DEFAULT 'UNKNOWN' CHECK (disposition_category IN ('INCLUDED_FAILURE','INCLUDED_CENSORED_ASSET_EVENT','EXCLUDED_NON_FAILURE','HELD_AMBIGUOUS','EXCLUDED_MIXED_CONTAMINATING','INCLUDED_PM_RESET_EVENT','PM_CONTEXT_ONLY','REJECTED_PM_RESET','UNKNOWN')),
                    include_in_event_processing INTEGER NOT NULL DEFAULT 0,
                    include_in_weibull_candidate INTEGER NOT NULL DEFAULT 0,
                    failure_mode_id INTEGER,
                    failure_mechanism_id INTEGER,
                    reset_target_failure_mode_id INTEGER,
                    reset_target_failure_mechanism_id INTEGER,
                    pm_reset_inclusion_decision TEXT CHECK (pm_reset_inclusion_decision IS NULL OR pm_reset_inclusion_decision IN ('APPROVED_RESET','REJECTED_RESET','CONTEXT_ONLY','NEEDS_REVIEW')),
                    pm_reset_renewal_rationale TEXT,
                    disposition_text TEXT,
                    disposition_notes TEXT,
                    decided_by_user_id INTEGER,
                    decided_at TEXT NOT NULL DEFAULT (datetime('now')),
                    is_current INTEGER NOT NULL DEFAULT 1,
                    FOREIGN KEY (mapped_record_id) REFERENCES mapped_cmms_record(mapped_record_id) ON UPDATE CASCADE ON DELETE RESTRICT,
                    FOREIGN KEY (modeled_population_id) REFERENCES modeled_population(modeled_population_id),
                    FOREIGN KEY (failure_mode_id) REFERENCES failure_mode(failure_mode_id),
                    FOREIGN KEY (failure_mechanism_id) REFERENCES failure_mechanism(failure_mechanism_id),
                    FOREIGN KEY (reset_target_failure_mode_id) REFERENCES failure_mode(failure_mode_id),
                    FOREIGN KEY (reset_target_failure_mechanism_id) REFERENCES failure_mechanism(failure_mechanism_id)
                );
                CREATE INDEX IF NOT EXISTS idx_event_disposition_mapped_record ON event_disposition(mapped_record_id);
                CREATE INDEX IF NOT EXISTS idx_event_disposition_modeled_population ON event_disposition(modeled_population_id);
                CREATE INDEX IF NOT EXISTS idx_event_disposition_current ON event_disposition(is_current);
                CREATE INDEX IF NOT EXISTS idx_event_disposition_category ON event_disposition(disposition_category);
                CREATE INDEX IF NOT EXISTS idx_event_disposition_failure_mode ON event_disposition(failure_mode_id);
                CREATE INDEX IF NOT EXISTS idx_event_disposition_failure_mechanism ON event_disposition(failure_mechanism_id);
                CREATE INDEX IF NOT EXISTS idx_event_disposition_pm_reset_decision ON event_disposition(pm_reset_inclusion_decision);
                CREATE INDEX IF NOT EXISTS idx_event_disposition_current_mapped ON event_disposition(is_current, mapped_record_id);
                CREATE INDEX IF NOT EXISTS idx_event_disposition_current_wo_missing ON event_disposition(is_current, mapped_record_id, failure_mode_id, failure_mechanism_id);
                CREATE INDEX IF NOT EXISTS idx_event_disposition_current_pm_missing ON event_disposition(is_current, mapped_record_id, reset_target_failure_mode_id, reset_target_failure_mechanism_id);
                CREATE UNIQUE INDEX IF NOT EXISTS ux_event_disposition_one_current ON event_disposition(mapped_record_id) WHERE is_current = 1;

                CREATE TABLE IF NOT EXISTS life_basis (
                    life_basis_id INTEGER PRIMARY KEY,
                    life_basis_code TEXT NOT NULL UNIQUE,
                    life_basis_name TEXT NOT NULL,
                    description TEXT,
                    is_active INTEGER NOT NULL DEFAULT 1
                );
                CREATE TABLE IF NOT EXISTS asset_schedule_class (
                    schedule_class_id INTEGER PRIMARY KEY,
                    schedule_class_code TEXT NOT NULL UNIQUE,
                    schedule_class_name TEXT NOT NULL,
                    hours_per_day REAL,
                    days_per_week REAL,
                    exclude_weekends INTEGER NOT NULL DEFAULT 1,
                    description TEXT,
                    is_active INTEGER NOT NULL DEFAULT 1
                );
                CREATE TABLE IF NOT EXISTS schedule_exception (
                    schedule_exception_id INTEGER PRIMARY KEY,
                    asset_number TEXT,
                    exception_start_datetime TEXT NOT NULL,
                    exception_end_datetime TEXT NOT NULL,
                    exception_type TEXT NOT NULL,
                    approved_by_user_id INTEGER,
                    approval_notes TEXT,
                    created_at TEXT NOT NULL DEFAULT (datetime('now'))
                );
                CREATE INDEX IF NOT EXISTS idx_schedule_exception_asset_number ON schedule_exception(asset_number);

                CREATE TABLE IF NOT EXISTS event_processing_record (
                    event_processing_id INTEGER PRIMARY KEY,
                    mapped_record_id INTEGER,
                    event_disposition_id INTEGER,
                    modeled_population_id INTEGER,
                    asset_number TEXT,
                    asset_name TEXT,
                    event_role TEXT NOT NULL CHECK (event_role IN ('FAILURE_EVENT','PM_RESET_EVENT','INSTALLATION_EVENT','REPLACEMENT_EVENT','CENSOR_CUTOFF_EVENT','TRACEABILITY_ONLY','EXCLUDED_EVENT')),
                    completed_date_raw TEXT,
                    completed_date_parsed TEXT,
                    date_parse_status TEXT,
                    failure_mode_id INTEGER,
                    failure_mechanism_id INTEGER,
                    grouping_level_used TEXT,
                    modeled_population_used TEXT,
                    weibull_sequence_number INTEGER,
                    previous_same_population_event_id INTEGER,
                    previous_same_population_date TEXT,
                    is_failure_event INTEGER NOT NULL DEFAULT 0,
                    is_pm_reset_event INTEGER NOT NULL DEFAULT 0,
                    is_valid_life_start INTEGER NOT NULL DEFAULT 0,
                    is_valid_life_end INTEGER NOT NULL DEFAULT 0,
                    weibull_life_note TEXT,
                    data_quality_assumption_flag TEXT,
                    processing_notes TEXT,
                    created_at TEXT NOT NULL DEFAULT (datetime('now')),
                    created_by_user_id INTEGER,
                    FOREIGN KEY (mapped_record_id) REFERENCES mapped_cmms_record(mapped_record_id),
                    FOREIGN KEY (event_disposition_id) REFERENCES event_disposition(event_disposition_id),
                    FOREIGN KEY (modeled_population_id) REFERENCES modeled_population(modeled_population_id),
                    FOREIGN KEY (failure_mode_id) REFERENCES failure_mode(failure_mode_id),
                    FOREIGN KEY (failure_mechanism_id) REFERENCES failure_mechanism(failure_mechanism_id)
                );
                CREATE INDEX IF NOT EXISTS idx_event_processing_population ON event_processing_record(modeled_population_id);
                CREATE INDEX IF NOT EXISTS idx_event_processing_asset ON event_processing_record(asset_number);
                CREATE INDEX IF NOT EXISTS idx_event_processing_completed_date ON event_processing_record(completed_date_parsed);
                CREATE INDEX IF NOT EXISTS idx_event_processing_role ON event_processing_record(event_role);

                CREATE TABLE IF NOT EXISTS weibull_observation (
                    weibull_observation_id INTEGER PRIMARY KEY,
                    modeled_population_id INTEGER,
                    asset_number TEXT,
                    start_event_processing_id INTEGER,
                    end_event_processing_id INTEGER,
                    observation_type TEXT NOT NULL CHECK (observation_type IN ('COMPLETED_FAILURE_LIFE','RIGHT_CENSORED_LIFE','PM_RESET_COMPLETED_LIFE','PM_RESET_CENSORED_LIFE')),
                    censoring_type TEXT,
                    start_datetime TEXT,
                    end_datetime TEXT,
                    analysis_cutoff_datetime TEXT,
                    life_basis_id INTEGER,
                    schedule_class_id INTEGER,
                    life_hours_raw_elapsed REAL,
                    excluded_night_hours REAL DEFAULT 0,
                    excluded_weekend_hours REAL DEFAULT 0,
                    excluded_holiday_hours REAL DEFAULT 0,
                    excluded_shutdown_hours REAL DEFAULT 0,
                    excluded_schedule_non_run_hours REAL DEFAULT 0,
                    life_hours_for_weibull REAL,
                    failure_indicator INTEGER NOT NULL DEFAULT 0,
                    is_right_censored INTEGER NOT NULL DEFAULT 0,
                    is_usable INTEGER NOT NULL DEFAULT 1,
                    weibull_life_note TEXT,
                    data_quality_assumption_flag TEXT,
                    created_at TEXT NOT NULL DEFAULT (datetime('now')),
                    created_by_user_id INTEGER,
                    FOREIGN KEY (modeled_population_id) REFERENCES modeled_population(modeled_population_id),
                    FOREIGN KEY (start_event_processing_id) REFERENCES event_processing_record(event_processing_id),
                    FOREIGN KEY (end_event_processing_id) REFERENCES event_processing_record(event_processing_id),
                    FOREIGN KEY (life_basis_id) REFERENCES life_basis(life_basis_id),
                    FOREIGN KEY (schedule_class_id) REFERENCES asset_schedule_class(schedule_class_id)
                );
                CREATE INDEX IF NOT EXISTS idx_weibull_observation_population ON weibull_observation(modeled_population_id);
                CREATE INDEX IF NOT EXISTS idx_weibull_observation_asset ON weibull_observation(asset_number);
                CREATE INDEX IF NOT EXISTS idx_weibull_observation_usable ON weibull_observation(is_usable);
                CREATE INDEX IF NOT EXISTS idx_weibull_observation_type ON weibull_observation(observation_type);

                CREATE TABLE IF NOT EXISTS analysis_dataset (
                    analysis_dataset_id INTEGER PRIMARY KEY,
                    modeled_population_id INTEGER,
                    asset_number TEXT,
                    analysis_name TEXT,
                    analysis_cutoff_datetime TEXT,
                    life_basis_id INTEGER,
                    created_by_user_id INTEGER,
                    created_at TEXT NOT NULL DEFAULT (datetime('now')),
                    dataset_status TEXT NOT NULL DEFAULT 'ACTIVE',
                    notes TEXT,
                    FOREIGN KEY (modeled_population_id) REFERENCES modeled_population(modeled_population_id),
                    FOREIGN KEY (life_basis_id) REFERENCES life_basis(life_basis_id)
                );
                CREATE INDEX IF NOT EXISTS idx_analysis_dataset_population ON analysis_dataset(modeled_population_id);
                CREATE INDEX IF NOT EXISTS idx_analysis_dataset_asset ON analysis_dataset(asset_number);

                CREATE TABLE IF NOT EXISTS analysis_dataset_member (
                    analysis_dataset_member_id INTEGER PRIMARY KEY,
                    analysis_dataset_id INTEGER NOT NULL,
                    weibull_observation_id INTEGER NOT NULL,
                    included_in_fit INTEGER NOT NULL DEFAULT 1,
                    member_notes TEXT,
                    FOREIGN KEY (analysis_dataset_id) REFERENCES analysis_dataset(analysis_dataset_id) ON DELETE CASCADE,
                    FOREIGN KEY (weibull_observation_id) REFERENCES weibull_observation(weibull_observation_id)
                );
                CREATE INDEX IF NOT EXISTS idx_analysis_dataset_member_dataset ON analysis_dataset_member(analysis_dataset_id);
                CREATE INDEX IF NOT EXISTS idx_analysis_dataset_member_observation ON analysis_dataset_member(weibull_observation_id);

                CREATE TABLE IF NOT EXISTS weibull_analysis_run (
                    weibull_analysis_run_id INTEGER PRIMARY KEY,
                    analysis_dataset_id INTEGER NOT NULL,
                    run_datetime TEXT NOT NULL DEFAULT (datetime('now')),
                    run_by_user_id INTEGER,
                    fit_method TEXT NOT NULL DEFAULT '2P_WEIBULL_MLE',
                    empirical_method TEXT NOT NULL DEFAULT 'KAPLAN_MEIER',
                    distribution_type TEXT NOT NULL DEFAULT 'WEIBULL_2P',
                    status TEXT NOT NULL DEFAULT 'COMPLETED',
                    software_version TEXT,
                    code_version TEXT,
                    notes TEXT,
                    FOREIGN KEY (analysis_dataset_id) REFERENCES analysis_dataset(analysis_dataset_id)
                );
                CREATE INDEX IF NOT EXISTS idx_weibull_analysis_run_dataset ON weibull_analysis_run(analysis_dataset_id);

                CREATE TABLE IF NOT EXISTS kaplan_meier_point (
                    kaplan_meier_point_id INTEGER PRIMARY KEY,
                    weibull_analysis_run_id INTEGER NOT NULL,
                    ordered_index INTEGER,
                    life_hours REAL,
                    at_risk_count INTEGER,
                    failure_count_at_time INTEGER,
                    censored_count_at_time INTEGER,
                    survival_estimate REAL,
                    cdf_estimate REAL,
                    reliability_estimate REAL,
                    weibull_plot_x REAL,
                    weibull_plot_y REAL,
                    FOREIGN KEY (weibull_analysis_run_id) REFERENCES weibull_analysis_run(weibull_analysis_run_id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_kaplan_meier_point_run ON kaplan_meier_point(weibull_analysis_run_id);

                CREATE TABLE IF NOT EXISTS weibull_result (
                    weibull_result_id INTEGER PRIMARY KEY,
                    weibull_analysis_run_id INTEGER NOT NULL,
                    beta_mle REAL,
                    eta_mle REAL,
                    beta_lower_ci REAL,
                    beta_upper_ci REAL,
                    eta_lower_ci REAL,
                    eta_upper_ci REAL,
                    log_likelihood REAL,
                    aic REAL,
                    bic REAL,
                    failure_count INTEGER,
                    censored_count INTEGER,
                    total_observation_count INTEGER,
                    mean_time_to_failure REAL,
                    b10_life REAL,
                    b50_life REAL,
                    fit_quality_notes TEXT,
                    engineering_interpretation TEXT,
                    recommended_action TEXT,
                    limitations TEXT,
                    created_at TEXT NOT NULL DEFAULT (datetime('now')),
                    FOREIGN KEY (weibull_analysis_run_id) REFERENCES weibull_analysis_run(weibull_analysis_run_id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_weibull_result_run ON weibull_result(weibull_analysis_run_id);

                CREATE TABLE IF NOT EXISTS weibull_curve_point (
                    weibull_curve_point_id INTEGER PRIMARY KEY,
                    weibull_analysis_run_id INTEGER NOT NULL,
                    life_hours REAL NOT NULL,
                    cdf REAL,
                    reliability REAL,
                    pdf REAL,
                    hazard_rate REAL,
                    FOREIGN KEY (weibull_analysis_run_id) REFERENCES weibull_analysis_run(weibull_analysis_run_id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_weibull_curve_point_run ON weibull_curve_point(weibull_analysis_run_id);

                CREATE TABLE IF NOT EXISTS weibull_parameter_adjustment (
                    parameter_adjustment_id INTEGER PRIMARY KEY,
                    weibull_result_id INTEGER NOT NULL,
                    adjusted_beta REAL NOT NULL,
                    adjusted_eta REAL NOT NULL,
                    adjustment_reason TEXT,
                    adjusted_by_user_id INTEGER,
                    adjusted_at TEXT NOT NULL DEFAULT (datetime('now')),
                    is_current INTEGER NOT NULL DEFAULT 1,
                    FOREIGN KEY (weibull_result_id) REFERENCES weibull_result(weibull_result_id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_weibull_parameter_adjustment_result ON weibull_parameter_adjustment(weibull_result_id);
                CREATE UNIQUE INDEX IF NOT EXISTS ux_weibull_parameter_adjustment_one_current ON weibull_parameter_adjustment(weibull_result_id) WHERE is_current = 1;

                CREATE TABLE IF NOT EXISTS approved_weibull_parameter (
                    approved_parameter_id INTEGER PRIMARY KEY,
                    weibull_result_id INTEGER NOT NULL,
                    parameter_adjustment_id INTEGER,
                    approved_beta REAL NOT NULL,
                    approved_eta REAL NOT NULL,
                    approved_life_basis_id INTEGER,
                    approved_modeled_population_id INTEGER,
                    approval_notes TEXT,
                    approved_by_user_id INTEGER,
                    approved_at TEXT NOT NULL DEFAULT (datetime('now')),
                    is_current INTEGER NOT NULL DEFAULT 1,
                    FOREIGN KEY (weibull_result_id) REFERENCES weibull_result(weibull_result_id),
                    FOREIGN KEY (parameter_adjustment_id) REFERENCES weibull_parameter_adjustment(parameter_adjustment_id),
                    FOREIGN KEY (approved_life_basis_id) REFERENCES life_basis(life_basis_id),
                    FOREIGN KEY (approved_modeled_population_id) REFERENCES modeled_population(modeled_population_id)
                );
                CREATE INDEX IF NOT EXISTS idx_approved_weibull_parameter_result ON approved_weibull_parameter(weibull_result_id);
                CREATE INDEX IF NOT EXISTS idx_approved_weibull_parameter_population ON approved_weibull_parameter(approved_modeled_population_id);
                """
            )
            conn.executemany(
                "INSERT OR IGNORE INTO life_basis(life_basis_code, life_basis_name, description) VALUES (?, ?, ?)",
                [
                    ("RAW_ELAPSED_HOURS", "Raw elapsed hours", "Calendar elapsed hours between start and end events."),
                    ("SCHEDULE_ADJUSTED_ELAPSED_HOURS", "Schedule-adjusted elapsed hours", "Elapsed hours after schedule exclusions."),
                    ("TRUE_OPERATING_HOURS", "True operating hours", "Runtime meter or telemetry-based operating hours."),
                    ("CYCLES", "Cycles", "Cycle count life basis."),
                    ("STARTS", "Starts", "Start count life basis."),
                ],
            )
            conn.executemany(
                """
                INSERT OR IGNORE INTO asset_schedule_class(
                    schedule_class_code, schedule_class_name, hours_per_day, days_per_week, exclude_weekends, description
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                [
                    ("24H_MON_FRI", "24 hours Monday-Friday", 24.0, 5.0, 1, "Continuous weekday operation."),
                    ("20H_MON_FRI", "20 hours Monday-Friday", 20.0, 5.0, 1, "Twenty-hour weekday operation."),
                    ("RAW_ELAPSED_ONLY", "Raw elapsed only", None, None, 0, "No schedule adjustment."),
                ],
            )
            self._migrate_rel_disposition_schema(conn)

    def _migrate_rel_disposition_schema(self, conn: sqlite3.Connection) -> None:
        """Safely add REL disposition columns/tables to existing GREMLIN.db files."""

        if self._table_exists(conn, "mapped_cmms_record"):
            required_mapped_columns = {
                "raw_content_hash": "TEXT",
                "downtime_raw": "TEXT",
                "downtime_minutes": "REAL",
                "downtime_hours": "REAL",
                "downtime_backfill_attempted": "INTEGER NOT NULL DEFAULT 0",
            }
            for column, ddl in required_mapped_columns.items():
                if not self._column_exists(conn, "mapped_cmms_record", column):
                    conn.execute(f"ALTER TABLE mapped_cmms_record ADD COLUMN {column} {ddl}")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_mapped_cmms_raw_hash ON mapped_cmms_record(raw_content_hash)")
            self._backfill_mapped_downtime_from_raw(conn)
        if self._table_exists(conn, "failure_mechanism") and not self._column_exists(conn, "failure_mechanism", "failure_mode_id"):
            conn.execute("ALTER TABLE failure_mechanism ADD COLUMN failure_mode_id INTEGER REFERENCES failure_mode(failure_mode_id)")
        if self._table_exists(conn, "modeled_population"):
            for column, ddl in {
                "asset_number": "TEXT",
                "failure_mode_id": "INTEGER REFERENCES failure_mode(failure_mode_id)",
                "failure_mechanism_id": "INTEGER REFERENCES failure_mechanism(failure_mechanism_id)",
                "grouping_level_used": "TEXT NOT NULL DEFAULT 'UNKNOWN'",
            }.items():
                if not self._column_exists(conn, "modeled_population", column):
                    conn.execute(f"ALTER TABLE modeled_population ADD COLUMN {column} {ddl}")
        if self._table_exists(conn, "event_disposition"):
            required_columns = {
                "modeled_population_id": "INTEGER REFERENCES modeled_population(modeled_population_id)",
                "record_class_final": "TEXT",
                "disposition_category": "TEXT NOT NULL DEFAULT 'UNKNOWN'",
                "include_in_event_processing": "INTEGER NOT NULL DEFAULT 0",
                "include_in_weibull_candidate": "INTEGER NOT NULL DEFAULT 0",
                "failure_mode_id": "INTEGER REFERENCES failure_mode(failure_mode_id)",
                "failure_mechanism_id": "INTEGER REFERENCES failure_mechanism(failure_mechanism_id)",
                "reset_target_failure_mode_id": "INTEGER REFERENCES failure_mode(failure_mode_id)",
                "reset_target_failure_mechanism_id": "INTEGER REFERENCES failure_mechanism(failure_mechanism_id)",
                "pm_reset_inclusion_decision": "TEXT",
                "pm_reset_renewal_rationale": "TEXT",
                "disposition_text": "TEXT",
                "disposition_notes": "TEXT",
                "decided_by_user_id": "INTEGER",
                "decided_at": "TEXT",
                "is_current": "INTEGER NOT NULL DEFAULT 1",
            }
            for column, ddl in required_columns.items():
                if not self._column_exists(conn, "event_disposition", column):
                    conn.execute(f"ALTER TABLE event_disposition ADD COLUMN {column} {ddl}")
        if self._table_exists(conn, "failure_mechanism") and self._column_exists(conn, "failure_mechanism", "failure_mode_id"):
            conn.execute("CREATE INDEX IF NOT EXISTS idx_failure_mechanism_failure_mode ON failure_mechanism(failure_mode_id)")
            conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_failure_mechanism_name_mode ON failure_mechanism(failure_mechanism_name, failure_mode_id)")

    def _backfill_mapped_downtime_from_raw(self, conn: sqlite3.Connection) -> int:
        """Populate newly migrated mapped downtime fields from stored raw CMMS JSON."""

        if not self._table_exists(conn, "raw_cmms_record"):
            return 0
        raw_columns = {row[1] for row in conn.execute("PRAGMA table_info(raw_cmms_record)")}
        if "raw_json" not in raw_columns:
            return 0
        required_columns = {"downtime_raw", "downtime_minutes", "downtime_hours", "downtime_backfill_attempted"}
        if not all(self._column_exists(conn, "mapped_cmms_record", column) for column in required_columns):
            return 0

        rows = conn.execute(
            """
            SELECT m.mapped_record_id, r.raw_json
            FROM mapped_cmms_record m
            JOIN raw_cmms_record r ON r.raw_record_id = m.raw_record_id
            WHERE m.downtime_hours IS NULL
              AND m.downtime_backfill_attempted = 0
            """
        ).fetchall()
        updates = []
        for row in rows:
            try:
                raw = json.loads(row["raw_json"] or "{}")
            except json.JSONDecodeError:
                raw = {}
            downtime_raw = self._get_alias(raw, "downtime")
            downtime_minutes = self._parse_downtime_minutes(downtime_raw)
            updates.append({
                "mapped_record_id": row["mapped_record_id"],
                "downtime_raw": downtime_raw,
                "downtime_minutes": downtime_minutes,
                "downtime_hours": downtime_minutes / 60.0 if downtime_minutes is not None else None,
            })
        if updates:
            conn.executemany(
                """
                UPDATE mapped_cmms_record
                SET downtime_raw = :downtime_raw,
                    downtime_minutes = :downtime_minutes,
                    downtime_hours = :downtime_hours,
                    downtime_backfill_attempted = 1
                WHERE mapped_record_id = :mapped_record_id
                """,
                updates,
            )
        return len(updates)

    def _table_exists(self, conn: sqlite3.Connection, table: str) -> bool:
        row = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
        return row is not None

    def _column_exists(self, conn: sqlite3.Connection, table: str, column: str) -> bool:
        return column in {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


    def mapped_record_count(self) -> int:
        """Return the number of mapped CMMS rows currently available."""

        with self.connect() as conn:
            if not self._table_exists(conn, "mapped_cmms_record"):
                return 0
            return int(conn.execute("SELECT COUNT(*) AS count FROM mapped_cmms_record").fetchone()["count"] or 0)

    def raw_record_count(self) -> int:
        """Return the number of raw CMMS rows currently available."""

        with self.connect() as conn:
            if not self._table_exists(conn, "raw_cmms_record"):
                return 0
            return int(conn.execute("SELECT COUNT(*) AS count FROM raw_cmms_record").fetchone()["count"] or 0)

    def ensure_mapped_records_available(self) -> int:
        """Create mapped rows on demand when raw data exists but no mapped layer exists yet."""

        if self.mapped_record_count() == 0 and self.raw_record_count() > 0:
            return self.refresh_mapped_cmms_records()
        return 0

    def refresh_mapped_cmms_records(self) -> int:
        """Map only new or changed raw JSON records into ``mapped_cmms_record``."""

        # An explicit mapping refresh may be intended to pick up rows already
        # imported or mapped by another GREMLIN process. Drop local asset-list
        # state before any early return so the next dropdown population reads
        # ``mapped_cmms_record`` even when this process has no upserts to make.
        self._asset_number_options_cache = None
        mapping_version = "v1"
        with self.write_connection() as conn:
            if not self._table_exists(conn, "raw_cmms_record"):
                return 0
            if not self._column_exists(conn, "mapped_cmms_record", "raw_content_hash"):
                conn.execute("ALTER TABLE mapped_cmms_record ADD COLUMN raw_content_hash TEXT")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_mapped_cmms_raw_hash ON mapped_cmms_record(raw_content_hash)")
            columns = {row[1] for row in conn.execute("PRAGMA table_info(raw_cmms_record)")}
            raw_id_expr = "raw_record_id" if "raw_record_id" in columns else "rowid AS raw_record_id"
            batch_expr = "import_batch_id" if "import_batch_id" in columns else "0 AS import_batch_id"
            existing_by_raw_id = {
                int(row["raw_record_id"]): {
                    "record_class_final": row["record_class_final"],
                    "raw_content_hash": row["raw_content_hash"],
                    "mapping_version": row["mapping_version"],
                }
                for row in conn.execute(
                    "SELECT raw_record_id, record_class_final, raw_content_hash, mapping_version FROM mapped_cmms_record"
                ).fetchall()
            }
            upsert_values: list[dict[str, Any]] = []
            for row in conn.execute(f"SELECT {raw_id_expr}, {batch_expr}, raw_json FROM raw_cmms_record"):
                raw_text = row["raw_json"] or "{}"
                raw_hash = hashlib.sha256(raw_text.encode("utf-8", errors="replace")).hexdigest()
                existing = existing_by_raw_id.get(int(row["raw_record_id"]))
                if existing and existing.get("raw_content_hash") == raw_hash and existing.get("mapping_version") == mapping_version:
                    continue
                try:
                    raw = json.loads(raw_text)
                except json.JSONDecodeError:
                    raw = {}
                mapped = self._map_raw_record(raw)
                mapped["record_class_final"] = existing.get("record_class_final") if existing else None
                upsert_values.append({
                    "raw_record_id": row["raw_record_id"],
                    "raw_content_hash": raw_hash,
                    "import_batch_id": row["import_batch_id"] or 0,
                    **mapped,
                    "mapping_version": mapping_version,
                })
            if not upsert_values:
                return 0
            cols = ", ".join(upsert_values[0])
            placeholders = ", ".join(f":{key}" for key in upsert_values[0])
            update_cols = [key for key in upsert_values[0] if key not in {"raw_record_id", "record_class_final"}]
            updates = ", ".join(f"{key}=excluded.{key}" for key in update_cols)
            conn.executemany(
                f"""
                INSERT INTO mapped_cmms_record ({cols}) VALUES ({placeholders})
                ON CONFLICT(raw_record_id) DO UPDATE SET
                    {updates},
                    record_class_final = mapped_cmms_record.record_class_final,
                    mapped_at = datetime('now')
                """,
                upsert_values,
            )
            return len(upsert_values)

    def _get_alias(self, raw: dict[str, Any], *keys: str) -> Any:
        for key in keys:
            if key in raw and raw[key] not in (None, ""):
                return raw[key]
        return None

    def _json_text(self, value: Any) -> str | None:
        if value in (None, ""):
            return None
        if isinstance(value, str):
            return value
        return json.dumps(value, ensure_ascii=False)

    def _map_raw_record(self, raw: dict[str, Any]) -> dict[str, Any]:
        completion_notes = self._get_alias(raw, "completionNotes", "CompletionNotes")
        requestor_description = self._get_alias(raw, "requestorDescription", "requestordescription")
        request_title = self._get_alias(raw, "requestTitle")
        task_name = self._get_alias(raw, "name")
        completed_date_final = self._get_alias(raw, "completedDate_Final", "dateCompletedfinal", "dateCompleted_Final")
        created_date_final = self._get_alias(raw, "createdDate_Final", "createdDateFinal")
        start_date_final = self._get_alias(raw, "startDate_Final", "startDateFinal")
        type_raw = self._get_alias(raw, "type")
        downtime_raw = self._get_alias(raw, "downtime")
        downtime_minutes = self._parse_downtime_minutes(downtime_raw)
        auto_class, is_pm, is_wo, reason = self._classify_record(type_raw, task_name, request_title, requestor_description, completion_notes, raw)
        status_text = str(self._get_alias(raw, "status", "statusID") or "").lower()
        return {
            "task_id": self._get_alias(raw, "taskID"),
            "task_name": task_name,
            "template_raw": self._get_alias(raw, "template"),
            "type_raw": type_raw,
            "associated_task_id": self._get_alias(raw, "associatedTaskID"),
            "status_raw": self._get_alias(raw, "status"),
            "status_id_raw": self._get_alias(raw, "statusID"),
            "asset_id_raw": self._get_alias(raw, "assetID"),
            "asset_name": self._get_alias(raw, "Asset Name"),
            "asset_number": self._get_alias(raw, "Asset Number"),
            "immediate_parent_asset_id": self._get_alias(raw, "Immediate Parent Asset ID"),
            "immediate_parent_asset_name": self._get_alias(raw, "Immediate Parent Asset Name"),
            "root_asset_id": self._get_alias(raw, "Root Asset ID"),
            "root_asset_name": self._get_alias(raw, "Root Asset Name"),
            "wo_asset_level": self._get_alias(raw, "WO Asset Level"),
            "asset_has_children_raw": self._get_alias(raw, "Asset Has Children"),
            "created_date_raw": self._get_alias(raw, "createdDate"),
            "created_datetime_raw": self._get_alias(raw, "createdDateTime"),
            "created_date_final": created_date_final,
            "start_date_raw": self._get_alias(raw, "startDate"),
            "start_datetime_raw": self._get_alias(raw, "startDateTime"),
            "start_date_final": start_date_final,
            "due_date_raw": self._get_alias(raw, "due"),
            "due_datetime_raw": self._get_alias(raw, "dueDate"),
            "due_date_final": self._get_alias(raw, "dueDate_Final"),
            "completed_date_raw": self._get_alias(raw, "dateCompleted"),
            "completed_datetime_raw": self._get_alias(raw, "completedDateTime"),
            "completed_date_final": completed_date_final,
            "completion_notes": completion_notes,
            "requestor_description": requestor_description,
            "request_title": request_title,
            "description_raw": self._get_alias(raw, "description"),
            "custom_tags_json": self._json_text(self._get_alias(raw, "customTags")),
            "po_ids_json": self._json_text(self._get_alias(raw, "poIDs")),
            "downtime_raw": downtime_raw,
            "downtime_minutes": downtime_minutes,
            "downtime_hours": downtime_minutes / 60.0 if downtime_minutes is not None else None,
            "record_class_auto": auto_class,
            "record_class_final": None,
            "classification_reason": reason,
            "is_pm_candidate": int(is_pm),
            "is_corrective_wo_candidate": int(is_wo),
            "is_purchase_order_related": int(bool(self._get_alias(raw, "poIDs"))),
            "is_completed": int("complete" in status_text or bool(completed_date_final)),
            "mapping_version": "v1",
        }

    def _parse_downtime_minutes(self, value: Any) -> float | None:
        if value in (None, ""):
            return None
        if isinstance(value, (int, float)):
            return float(value)
        text = str(value).strip().lower()
        match = re.search(r"[-+]?\d*\.?\d+", text)
        if not match:
            return None
        number = float(match.group())
        if "hour" in text or re.search(r"\bhr\b", text):
            return number * 60.0
        return number

    def _classify_record(self, type_raw: Any, task_name: Any, request_title: Any, requestor_description: Any, completion_notes: Any, raw: dict[str, Any]) -> tuple[str, bool, bool, str]:
        type_text = str(type_raw or "").strip()
        text = " ".join(str(part or "") for part in (task_name, request_title, requestor_description, completion_notes, raw.get("description"), raw.get("customTags"))).lower()
        task_text = str(task_name or "")
        pm_patterns = [" - M - ", " - Q - ", " - W - ", " - SA - ", " - A - "]
        is_pm = type_text == "1" or any(pattern.lower() in task_text.lower() for pattern in pm_patterns) or bool(re.search(r"\bpm\b", text)) or any(
            phrase in text for phrase in ("pm completed", "completed pm", "performed pm", "pm service", "pm was completed")
        )
        corrective_terms = (
            "leaking", "broken", "not working", "fault", "alarm", "jammed", "no power", "overheating", "making noise",
            "failed", "repair", "replace", "troubleshoot", "investigate", "stopped", "stuck", "issue", "faulted",
        )
        is_wo = type_text == "6" or any(term in text for term in corrective_terms)
        parts_terms = ("order parts", "spare parts", "identify and order spares", "parts required", "deliver parts", "picked up parts", "put into stock", "all parts accounted for")
        project_terms = ("project", "upgrade", "install", "scheduled project")
        inspection_terms = ("inspection", "inspect", "checked", "audit")
        if any(term in text for term in parts_terms):
            return "PARTS_ORDER", is_pm, is_wo, "parts/order text rule"
        if any(term in text for term in project_terms):
            return "PROJECT_WORK", is_pm, is_wo, "project text rule"
        if is_pm:
            return "PM", is_pm, is_wo, "PM candidate rule"
        if is_wo:
            return "CORRECTIVE_WO", is_pm, is_wo, "corrective WO candidate rule"
        if any(term in text for term in inspection_terms) and not is_wo:
            return "INSPECTION", is_pm, is_wo, "inspection-only text rule"
        return "UNKNOWN", is_pm, is_wo, "default unknown"

    def asset_numbers(self, *, refresh: bool = False) -> list[str]:
        return [row["asset_number"] for row in self.asset_number_options(refresh=refresh)]

    def asset_number_options(self, *, refresh: bool = False) -> list[dict[str, str]]:
        if refresh:
            mapped_count = self.refresh_mapped_cmms_records()
            if mapped_count:
                self._asset_number_options_cache = None
        elif self._asset_number_options_cache is not None:
            return [dict(option) for option in self._asset_number_options_cache]
        else:
            mapped_count = self.ensure_mapped_records_available()
            if mapped_count:
                self._asset_number_options_cache = None
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT TRIM(asset_number) AS asset_number,
                       COALESCE(NULLIF(TRIM(asset_name), ''), '') AS asset_name,
                       COUNT(*) AS record_count
                FROM mapped_cmms_record
                WHERE asset_number IS NOT NULL AND TRIM(asset_number) <> ''
                GROUP BY TRIM(asset_number), COALESCE(NULLIF(TRIM(asset_name), ''), '')
                ORDER BY TRIM(asset_number), record_count DESC
                """
            ).fetchall()
        best_by_number: dict[str, dict[str, str | int]] = {}
        for row in rows:
            asset_number = row["asset_number"]
            current = best_by_number.get(asset_number)
            if current is None or int(row["record_count"] or 0) > int(current["record_count"] or 0):
                best_by_number[asset_number] = {
                    "asset_number": asset_number,
                    "asset_name": row["asset_name"] or "",
                    "record_count": int(row["record_count"] or 0),
                }
        options = [
            {"asset_number": str(row["asset_number"]), "asset_name": str(row["asset_name"])}
            for row in sorted(best_by_number.values(), key=lambda item: self._natural_key(str(item["asset_number"])))
        ]
        self._asset_number_options_cache = [dict(option) for option in options]
        return options

    def _natural_key(self, value: str) -> list[Any]:
        return [int(part) if part.isdigit() else part.lower() for part in re.split(r"(\d+)", value)]

    def summary_for_asset(self, asset_number: str) -> SummaryMetrics:
        with self.connect() as conn:
            row = conn.execute(
                """
                WITH current_disp AS (SELECT * FROM event_disposition WHERE is_current = 1),
                asset_records AS (
                    SELECT m.*, d.event_disposition_id, d.disposition_category, d.pm_reset_inclusion_decision,
                           d.include_in_weibull_candidate, d.failure_mode_id, d.modeled_population_id, d.reset_target_failure_mode_id,
                           COALESCE(d.record_class_final, m.record_class_final, m.record_class_auto) AS effective_record_class
                    FROM mapped_cmms_record m
                    LEFT JOIN current_disp d ON d.mapped_record_id = m.mapped_record_id
                    WHERE m.asset_number = :asset_number
                )
                SELECT
                    COUNT(*) AS total_entries,
                    COALESCE(SUM(CASE WHEN (effective_record_class = 'CORRECTIVE_WO' OR is_corrective_wo_candidate = 1)
                        AND disposition_category = 'INCLUDED_FAILURE' AND include_in_weibull_candidate = 1
                        AND failure_mode_id IS NOT NULL AND modeled_population_id IS NOT NULL THEN 1 ELSE 0 END), 0) AS usable_wos_for_weibull,
                    COALESCE(SUM(CASE WHEN (effective_record_class IN ('PM','PM_RESET_CANDIDATE') OR is_pm_candidate = 1)
                        AND disposition_category = 'INCLUDED_PM_RESET_EVENT' AND pm_reset_inclusion_decision = 'APPROVED_RESET'
                        AND include_in_weibull_candidate = 1 AND reset_target_failure_mode_id IS NOT NULL
                        AND modeled_population_id IS NOT NULL THEN 1 ELSE 0 END), 0) AS usable_pms_for_weibull,
                    COALESCE(SUM(CASE WHEN (effective_record_class = 'CORRECTIVE_WO' OR is_corrective_wo_candidate = 1)
                        AND event_disposition_id IS NOT NULL THEN 1 ELSE 0 END), 0) AS wos_dispositioned,
                    COALESCE(SUM(CASE WHEN (effective_record_class = 'CORRECTIVE_WO' OR is_corrective_wo_candidate = 1)
                        AND event_disposition_id IS NULL THEN 1 ELSE 0 END), 0) AS wos_not_dispositioned,
                    COALESCE(SUM(CASE WHEN (effective_record_class IN ('PM','PM_RESET_CANDIDATE') OR is_pm_candidate = 1)
                        AND event_disposition_id IS NOT NULL THEN 1 ELSE 0 END), 0) AS pms_dispositioned,
                    COALESCE(SUM(CASE WHEN (effective_record_class IN ('PM','PM_RESET_CANDIDATE') OR is_pm_candidate = 1)
                        AND event_disposition_id IS NULL THEN 1 ELSE 0 END), 0) AS pms_not_dispositioned
                FROM asset_records
                """,
                {"asset_number": asset_number},
            ).fetchone()
        return SummaryMetrics(**{field: int(row[field] or 0) for field in SummaryMetrics.__dataclass_fields__})

    def weibull_group_options(self, asset_number: str) -> list[dict[str, Any]]:
        """Return failure-mode and failure-mechanism Weibull populations available for an asset."""

        with self.connect() as conn:
            rows = conn.execute(
                """
                WITH current_disp AS (
                    SELECT * FROM event_disposition WHERE is_current = 1 AND include_in_weibull_candidate = 1
                ),
                included AS (
                    SELECT
                        d.disposition_category,
                        d.pm_reset_inclusion_decision,
                        d.failure_mode_id AS wo_failure_mode_id,
                        d.failure_mechanism_id AS wo_failure_mechanism_id,
                        d.reset_target_failure_mode_id AS pm_failure_mode_id,
                        d.reset_target_failure_mechanism_id AS pm_failure_mechanism_id
                    FROM mapped_cmms_record m
                    JOIN current_disp d ON d.mapped_record_id = m.mapped_record_id
                    WHERE m.asset_number = ?
                      AND (
                        (d.disposition_category = 'INCLUDED_FAILURE' AND d.failure_mode_id IS NOT NULL)
                        OR (d.disposition_category = 'INCLUDED_PM_RESET_EVENT'
                            AND d.pm_reset_inclusion_decision = 'APPROVED_RESET'
                            AND d.reset_target_failure_mode_id IS NOT NULL)
                      )
                ),
                mode_groups AS (
                    SELECT
                        'FAILURE_MODE' AS grouping_level,
                        COALESCE(wo_failure_mode_id, pm_failure_mode_id) AS failure_mode_id,
                        NULL AS failure_mechanism_id,
                        SUM(CASE WHEN disposition_category = 'INCLUDED_FAILURE' THEN 1 ELSE 0 END) AS failure_count,
                        SUM(CASE WHEN disposition_category = 'INCLUDED_PM_RESET_EVENT' THEN 1 ELSE 0 END) AS reset_count
                    FROM included
                    GROUP BY COALESCE(wo_failure_mode_id, pm_failure_mode_id)
                ),
                mechanism_groups AS (
                    SELECT
                        'FAILURE_MECHANISM' AS grouping_level,
                        COALESCE(wo_failure_mode_id, pm_failure_mode_id) AS failure_mode_id,
                        COALESCE(wo_failure_mechanism_id, pm_failure_mechanism_id) AS failure_mechanism_id,
                        SUM(CASE WHEN disposition_category = 'INCLUDED_FAILURE' THEN 1 ELSE 0 END) AS failure_count,
                        SUM(CASE WHEN disposition_category = 'INCLUDED_PM_RESET_EVENT' THEN 1 ELSE 0 END) AS reset_count
                    FROM included
                    WHERE COALESCE(wo_failure_mechanism_id, pm_failure_mechanism_id) IS NOT NULL
                    GROUP BY COALESCE(wo_failure_mode_id, pm_failure_mode_id), COALESCE(wo_failure_mechanism_id, pm_failure_mechanism_id)
                ),
                all_groups AS (
                    SELECT * FROM mode_groups
                    UNION ALL
                    SELECT * FROM mechanism_groups
                )
                SELECT
                    g.grouping_level,
                    g.failure_mode_id,
                    fm.failure_mode_name,
                    g.failure_mechanism_id,
                    fmech.failure_mechanism_name,
                    g.failure_count,
                    g.reset_count
                FROM all_groups g
                JOIN failure_mode fm ON fm.failure_mode_id = g.failure_mode_id
                LEFT JOIN failure_mechanism fmech ON fmech.failure_mechanism_id = g.failure_mechanism_id
                WHERE g.failure_count > 0
                  AND (g.grouping_level = 'FAILURE_MODE' OR fmech.failure_mechanism_id IS NOT NULL)
                ORDER BY g.grouping_level DESC, fm.failure_mode_name, fmech.failure_mechanism_name
                """,
                (asset_number,),
            ).fetchall()
        options = []
        for row in rows:
            label = row["failure_mode_name"]
            if row["grouping_level"] == "FAILURE_MECHANISM":
                label = f"{row['failure_mode_name']} / {row['failure_mechanism_name']}"
            options.append({
                "grouping_level": row["grouping_level"],
                "failure_mode_id": int(row["failure_mode_id"]),
                "failure_mode_name": row["failure_mode_name"],
                "failure_mechanism_id": int(row["failure_mechanism_id"]) if row["failure_mechanism_id"] is not None else None,
                "failure_mechanism_name": row["failure_mechanism_name"],
                "failure_count": int(row["failure_count"] or 0),
                "reset_count": int(row["reset_count"] or 0),
                "label": label,
            })
        return options


    def calculate_all_weibull_results(self, asset_number: str) -> dict[str, Any]:
        """Run and save Weibull MLE results for every available mode/mechanism group on an asset."""

        group_options = self.weibull_group_options(asset_number)
        summary: dict[str, Any] = {
            "asset_number": asset_number,
            "total": len(group_options),
            "completed": 0,
            "failed": 0,
            "results": [],
            "errors": [],
        }
        for option in group_options:
            label = str(option.get("label") or "Unknown failure group")
            try:
                result = self.perform_weibull_analysis(
                    asset_number,
                    grouping_level=str(option["grouping_level"]),
                    failure_mode_id=int(option["failure_mode_id"]),
                    failure_mechanism_id=int(option["failure_mechanism_id"]) if option.get("failure_mechanism_id") is not None else None,
                )
            except Exception as exc:  # Keep processing independent groups even when one fit needs review.
                summary["failed"] += 1
                summary["errors"].append(f"{label}: {exc}")
                continue
            summary["completed"] += 1
            summary["results"].append({
                "label": label,
                "grouping_level": option.get("grouping_level"),
                "failure_mode_id": option.get("failure_mode_id"),
                "failure_mechanism_id": option.get("failure_mechanism_id"),
                "run_id": result.run_id,
                "result_id": result.result_id,
                "beta_mle": result.beta_mle,
                "eta_mle": result.eta_mle,
                "failure_count": result.failure_count,
                "censored_count": result.censored_count,
            })
        return summary


    def latest_failure_mechanism_beta_rankings(self, asset_number: str, *, limit: int = 5) -> list[dict[str, Any]]:
        """Return latest saved Weibull beta values for this asset's failure-mechanism populations."""

        with self.connect() as conn:
            rows = conn.execute(
                """
                WITH latest_result AS (
                    SELECT
                        ad.modeled_population_id,
                        wr.weibull_result_id,
                        wr.beta_mle,
                        wr.eta_mle,
                        wr.failure_count,
                        wr.censored_count,
                        war.run_datetime,
                        ROW_NUMBER() OVER (
                            PARTITION BY ad.modeled_population_id
                            ORDER BY war.run_datetime DESC, wr.weibull_result_id DESC
                        ) AS result_rank
                    FROM weibull_result wr
                    JOIN weibull_analysis_run war ON war.weibull_analysis_run_id = wr.weibull_analysis_run_id
                    JOIN analysis_dataset ad ON ad.analysis_dataset_id = war.analysis_dataset_id
                    WHERE ad.asset_number = :asset_number
                )
                SELECT
                    mp.modeled_population_id,
                    fm.failure_mode_name,
                    fmech.failure_mechanism_name,
                    lr.beta_mle,
                    lr.eta_mle,
                    lr.failure_count,
                    lr.censored_count,
                    lr.run_datetime
                FROM latest_result lr
                JOIN modeled_population mp ON mp.modeled_population_id = lr.modeled_population_id
                JOIN failure_mode fm ON fm.failure_mode_id = mp.failure_mode_id
                JOIN failure_mechanism fmech ON fmech.failure_mechanism_id = mp.failure_mechanism_id
                WHERE lr.result_rank = 1
                  AND mp.asset_number = :asset_number
                  AND mp.grouping_level_used = 'FAILURE_MECHANISM'
                ORDER BY lr.beta_mle DESC, lr.failure_count DESC, fmech.failure_mechanism_name
                LIMIT :limit
                """,
                {"asset_number": asset_number, "limit": limit},
            ).fetchall()
        return [
            {
                "modeled_population_id": int(row["modeled_population_id"]),
                "failure_mode_name": row["failure_mode_name"],
                "failure_mechanism_name": row["failure_mechanism_name"],
                "beta_mle": float(row["beta_mle"]),
                "eta_mle": float(row["eta_mle"]),
                "failure_count": int(row["failure_count"] or 0),
                "censored_count": int(row["censored_count"] or 0),
                "run_datetime": row["run_datetime"],
            }
            for row in rows
        ]

    def failure_mechanism_pareto(self, asset_number: str) -> list[dict[str, Any]]:
        """Return included failure counts and downtime by failure mechanism for the asset summary Pareto chart."""

        with self.connect() as conn:
            rows = conn.execute(
                """
                WITH current_disp AS (
                    SELECT * FROM event_disposition WHERE is_current = 1 AND include_in_weibull_candidate = 1
                )
                SELECT
                    d.failure_mode_id,
                    d.failure_mechanism_id,
                    COALESCE(fmech.failure_mechanism_name, 'Unspecified mechanism') AS failure_mechanism_name,
                    COALESCE(fm.failure_mode_name, 'Unspecified mode') AS failure_mode_name,
                    COUNT(*) AS failure_count,
                    COALESCE(SUM(COALESCE(m.downtime_hours, 0)), 0) AS downtime_hours
                FROM mapped_cmms_record m
                JOIN current_disp d ON d.mapped_record_id = m.mapped_record_id
                LEFT JOIN failure_mode fm ON fm.failure_mode_id = d.failure_mode_id
                LEFT JOIN failure_mechanism fmech ON fmech.failure_mechanism_id = d.failure_mechanism_id
                WHERE m.asset_number = :asset_number
                  AND d.disposition_category = 'INCLUDED_FAILURE'
                  AND d.failure_mechanism_id IS NOT NULL
                GROUP BY d.failure_mode_id, d.failure_mechanism_id, fmech.failure_mechanism_name, fm.failure_mode_name
                ORDER BY downtime_hours DESC, failure_count DESC, failure_mechanism_name
                """,
                {"asset_number": asset_number},
            ).fetchall()
        total = sum(float(row["downtime_hours"] or 0) for row in rows) or 1.0
        cumulative = 0.0
        pareto_rows = []
        for row in rows:
            count = int(row["failure_count"] or 0)
            downtime_hours = float(row["downtime_hours"] or 0.0)
            cumulative += downtime_hours
            pareto_rows.append({
                "failure_mode_id": int(row["failure_mode_id"]),
                "failure_mechanism_id": int(row["failure_mechanism_id"]),
                "failure_mechanism_name": row["failure_mechanism_name"],
                "failure_mode_name": row["failure_mode_name"],
                "failure_count": count,
                "downtime_hours": downtime_hours,
                "cumulative_percent": cumulative / total * 100,
            })
        return pareto_rows

    def _disposition_where(self, kind: str) -> str:
        if kind == "wo":
            return "(COALESCE(m.record_class_final, m.record_class_auto) = 'CORRECTIVE_WO' OR m.is_corrective_wo_candidate = 1)"
        if kind == "pm":
            return "(COALESCE(m.record_class_final, m.record_class_auto) IN ('PM','PM_RESET_CANDIDATE') OR m.is_pm_candidate = 1)"
        raise ValueError("Disposition kind must be 'wo' or 'pm'.")

    def _needs_disposition_where(self, kind: str) -> str:
        # "New/undispositioned" means a row that has no current disposition yet, or
        # one saved as an inclusion that still lacks its required failure
        # mode/mechanism. Reviewed exclusions (EXCLUDED_NON_FAILURE,
        # HELD_AMBIGUOUS, PM_CONTEXT_ONLY, REJECTED_PM_RESET, …) intentionally
        # leave those IDs blank, so they must not keep matching this filter.
        if kind == "wo":
            return (
                "AND (d.event_disposition_id IS NULL OR ("
                "d.disposition_category IN ('INCLUDED_FAILURE','INCLUDED_CENSORED_ASSET_EVENT') "
                "AND (d.failure_mode_id IS NULL OR d.failure_mechanism_id IS NULL)))"
            )
        if kind == "pm":
            return (
                "AND (d.event_disposition_id IS NULL OR ("
                "d.disposition_category = 'INCLUDED_PM_RESET_EVENT' "
                "AND (d.reset_target_failure_mode_id IS NULL OR d.reset_target_failure_mechanism_id IS NULL)))"
            )
        raise ValueError("Disposition kind must be 'wo' or 'pm'.")

    def disposition_row_count(self, asset_number: str, kind: str, *, only_needing_disposition: bool = False) -> int:
        where = self._disposition_where(kind)
        needs_disposition_where = self._needs_disposition_where(kind) if only_needing_disposition else ""
        with self.connect() as conn:
            row = conn.execute(
                f"""
                SELECT COUNT(*) AS count
                FROM mapped_cmms_record m
                LEFT JOIN event_disposition d ON d.mapped_record_id = m.mapped_record_id AND d.is_current = 1
                WHERE m.asset_number = ? AND {where} {needs_disposition_where}
                """,
                (asset_number,),
            ).fetchone()
        return int(row["count"] or 0)

    def disposition_rows(self, asset_number: str, kind: str, *, only_needing_disposition: bool = False, limit: int | None = None, offset: int = 0) -> list[dict[str, Any]]:
        where = self._disposition_where(kind)
        needs_disposition_where = self._needs_disposition_where(kind) if only_needing_disposition else ""
        pagination = ""
        params: list[Any] = [asset_number]
        if limit is not None:
            if limit <= 0:
                raise ValueError("Disposition row limit must be greater than zero.")
            if offset < 0:
                raise ValueError("Disposition row offset cannot be negative.")
            pagination = " LIMIT ? OFFSET ?"
            params.extend([limit, offset])
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT m.mapped_record_id,
                       m.task_name AS name,
                       m.task_id AS taskID,
                       m.created_date_final AS createdDate_Final,
                       m.completed_date_final AS completedDate_Final,
                       COALESCE(m.completed_date_final, m.start_date_final, m.created_date_final) AS weibullEventDate_Final,
                       CASE
                           WHEN m.completed_date_final IS NOT NULL AND trim(m.completed_date_final) != '' THEN 'completedDate_Final'
                           WHEN m.start_date_final IS NOT NULL AND trim(m.start_date_final) != '' THEN 'startDate_Final'
                           WHEN m.created_date_final IS NOT NULL AND trim(m.created_date_final) != '' THEN 'createdDate_Final'
                           ELSE ''
                       END AS weibullEventDate_Source,
                       COALESCE(m.downtime_raw, CAST(m.downtime_minutes AS TEXT)) AS downtime,
                       m.completion_notes AS completionNotes,
                       m.request_title AS requestTitle,
                       m.requestor_description AS requestorDescription,
                       COALESCE(d.record_class_final, m.record_class_final, m.record_class_auto) AS effective_record_class,
                       d.disposition_category,
                       d.pm_reset_inclusion_decision,
                       d.disposition_text,
                       d.disposition_notes,
                       d.pm_reset_renewal_rationale,
                       d.failure_mode_id,
                       fm.failure_mode_name AS failure_mode,
                       d.failure_mechanism_id,
                       fmech.failure_mechanism_name AS failure_mechanism,
                       d.reset_target_failure_mode_id,
                       rtfm.failure_mode_name AS reset_target_failure_mode,
                       d.reset_target_failure_mechanism_id,
                       rtfmech.failure_mechanism_name AS reset_target_failure_mechanism,
                       d.include_in_weibull_candidate,
                       d.modeled_population_id,
                       mp.population_name AS modeled_population_name
                FROM mapped_cmms_record m
                LEFT JOIN event_disposition d ON d.mapped_record_id = m.mapped_record_id AND d.is_current = 1
                LEFT JOIN failure_mode fm ON fm.failure_mode_id = d.failure_mode_id
                LEFT JOIN failure_mechanism fmech ON fmech.failure_mechanism_id = d.failure_mechanism_id
                LEFT JOIN failure_mode rtfm ON rtfm.failure_mode_id = d.reset_target_failure_mode_id
                LEFT JOIN failure_mechanism rtfmech ON rtfmech.failure_mechanism_id = d.reset_target_failure_mechanism_id
                LEFT JOIN modeled_population mp ON mp.modeled_population_id = d.modeled_population_id
                WHERE m.asset_number = ? AND {where} {needs_disposition_where}
                ORDER BY COALESCE(m.completed_date_final, m.start_date_final, m.created_date_final), m.task_id
                {pagination}
                """,
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def disposition_excel_headers(self, kind: str) -> tuple[str, ...]:
        """Return the Excel template columns for a disposition screen."""

        if kind == "wo":
            return EXCEL_WO_DISPOSITION_COLUMNS
        if kind == "pm":
            return EXCEL_PM_DISPOSITION_COLUMNS
        raise ValueError("Disposition kind must be 'wo' or 'pm'.")

    def export_disposition_excel(self, asset_number: str, kind: str, output_path: str | Path) -> int:
        """Write the selected asset's disposition table to an Excel workbook."""

        headers = self.disposition_excel_headers(kind)
        rows = self.disposition_rows(asset_number, kind)
        sheet_rows: list[list[Any]] = [list(headers)]
        for row in rows:
            record: dict[str, Any] = {
                "mapped_record_id": row.get("mapped_record_id"),
                "disposition_notes": row.get("disposition_notes") or row.get("disposition_text"),
                "disposition_category": row.get("disposition_category") or "UNKNOWN",
                "record_class": row.get("effective_record_class") or ("CORRECTIVE_WO" if kind == "wo" else "PM"),
                "include_in_weibull_candidate": bool(row.get("include_in_weibull_candidate")),
                "failure_mode": row.get("failure_mode"),
                "failure_mechanism": row.get("failure_mechanism"),
                "reset_target_failure_mode": row.get("reset_target_failure_mode"),
                "reset_target_failure_mechanism": row.get("reset_target_failure_mechanism"),
                "pm_reset_decision": row.get("pm_reset_inclusion_decision") or "NEEDS_REVIEW",
                "pm_reset_renewal_rationale": row.get("pm_reset_renewal_rationale"),
            }
            record.update({key: row.get(key) for key in DISPLAY_COLUMNS})
            record.update({
                "failure_mode_id": row.get("failure_mode_id"),
                "failure_mechanism_id": row.get("failure_mechanism_id"),
                "reset_target_failure_mode_id": row.get("reset_target_failure_mode_id"),
                "reset_target_failure_mechanism_id": row.get("reset_target_failure_mechanism_id"),
            })
            sheet_rows.append([record.get(header) for header in headers])
        validations, lookup_rows = self._disposition_excel_validation_data(asset_number, kind, headers)
        self._write_xlsx(
            output_path,
            sheet_rows,
            "WO Dispositions" if kind == "wo" else "PM Dispositions",
            validations=validations,
            lookup_rows=lookup_rows,
        )
        return len(rows)

    def import_disposition_excel(self, asset_number: str, kind: str, input_path: str | Path) -> int:
        """Read disposition rows from Excel and save them as current dispositions."""

        current_rows = {int(row["mapped_record_id"]): row for row in self.disposition_rows(asset_number, kind)}
        valid_ids = set(current_rows)
        rows = self._read_xlsx(input_path)
        if not rows:
            raise ValueError("The Excel file does not contain a header row.")
        headers = [self._normalize_excel_header(value) for value in rows[0]]
        if "mapped_record_id" not in headers:
            raise ValueError("The Excel file must include a mapped_record_id column from the downloaded template.")

        mode_name_to_id = {
            self._normalize_taxonomy_text(row.get("failure_mode_name")): int(row["failure_mode_id"])
            for row in self.get_asset_failure_mode_options(asset_number)
            if self._normalize_taxonomy_text(row.get("failure_mode_name"))
        }
        mechanism_ids_by_mode_and_name: dict[tuple[int, str], int] = {}
        mechanism_ids_by_name: dict[str, set[int]] = {}
        for row in self.get_asset_failure_mechanism_options(asset_number):
            normalized_name = self._normalize_taxonomy_text(row.get("failure_mechanism_name"))
            if not normalized_name:
                continue
            mechanism_id = int(row["failure_mechanism_id"])
            mechanism_ids_by_name.setdefault(normalized_name, set()).add(mechanism_id)
            failure_mode_id = self._optional_int_value(row.get("failure_mode_id"))
            if failure_mode_id is not None:
                mechanism_ids_by_mode_and_name[(failure_mode_id, normalized_name)] = mechanism_id

        pending_dispositions: list[dict[str, Any]] = []
        for row_number, values in enumerate(rows[1:], start=2):
            data = {header: values[index] if index < len(values) else None for index, header in enumerate(headers) if header}
            raw_mapped_id = data.get("mapped_record_id")
            if raw_mapped_id in (None, ""):
                continue
            mapped_record_id = self._excel_required_int(raw_mapped_id, "mapped_record_id", row_number)
            if mapped_record_id not in valid_ids:
                raise ValueError(f"Mapped record {mapped_record_id} is not on the current {asset_number} {kind.upper()} disposition page.")
            disposition_category = self._excel_text(data.get("disposition_category")) or "UNKNOWN"
            record_class = self._excel_text(data.get("record_class")) or ("CORRECTIVE_WO" if kind == "wo" else "PM")
            include_candidate = self._excel_optional_bool(data.get("include_in_weibull_candidate"))
            kwargs: dict[str, Any] = {
                "kind": kind,
                "disposition_category": disposition_category,
                "disposition_text": self._excel_text(data.get("disposition_notes")),
                "record_class_final": record_class,
                "include_in_weibull_candidate": include_candidate,
            }
            if kind == "pm":
                reset_mode_text = self._excel_text(data.get("reset_target_failure_mode"))
                reset_mechanism_text = self._excel_text(data.get("reset_target_failure_mechanism"))
                kwargs.update({
                    "pm_reset_decision": self._excel_text(data.get("pm_reset_decision")) or "NEEDS_REVIEW",
                    "pm_reset_rationale": self._excel_text(data.get("pm_reset_renewal_rationale")),
                    "reset_target_failure_mode_id": self._excel_optional_int(data.get("reset_target_failure_mode_id")) or mode_name_to_id.get(self._normalize_taxonomy_text(reset_mode_text)),
                    "reset_target_failure_mechanism_id": None,
                })
                kwargs["reset_target_failure_mechanism_id"] = self._excel_optional_int(data.get("reset_target_failure_mechanism_id")) or self._excel_mechanism_id_for_mode(
                    mechanism_ids_by_mode_and_name,
                    mechanism_ids_by_name,
                    reset_mechanism_text,
                    kwargs.get("reset_target_failure_mode_id"),
                )
            else:
                failure_mode_text = self._excel_text(data.get("failure_mode"))
                failure_mechanism_text = self._excel_text(data.get("failure_mechanism"))
                kwargs.update({
                    "failure_mode_id": self._excel_optional_int(data.get("failure_mode_id")) or mode_name_to_id.get(self._normalize_taxonomy_text(failure_mode_text)),
                    "failure_mechanism_id": None,
                    "failure_mode_text": failure_mode_text,
                    "failure_mechanism_text": failure_mechanism_text,
                })
                kwargs["failure_mechanism_id"] = self._excel_optional_int(data.get("failure_mechanism_id")) or self._excel_mechanism_id_for_mode(
                    mechanism_ids_by_mode_and_name,
                    mechanism_ids_by_name,
                    failure_mechanism_text,
                    kwargs.get("failure_mode_id"),
                )
            if not self._excel_disposition_matches_current(current_rows[mapped_record_id], kind, kwargs):
                pending_dispositions.append({"mapped_record_id": mapped_record_id, **kwargs})
        return self.save_dispositions(pending_dispositions)

    def _excel_mechanism_id_for_mode(
        self,
        ids_by_mode_and_name: dict[tuple[int, str], int],
        ids_by_name: dict[str, set[int]],
        mechanism_text: str,
        failure_mode_id: Any,
    ) -> int | None:
        """Resolve an Excel mechanism name without crossing failure-mode context."""

        normalized_name = self._normalize_taxonomy_text(mechanism_text)
        if not normalized_name:
            return None
        mode_id = self._optional_int_value(failure_mode_id)
        if mode_id is not None:
            mechanism_id = ids_by_mode_and_name.get((mode_id, normalized_name))
            if mechanism_id is not None:
                return mechanism_id
        mechanism_ids = ids_by_name.get(normalized_name, set())
        if len(mechanism_ids) == 1:
            return next(iter(mechanism_ids))
        return None

    def _excel_disposition_matches_current(self, current_row: dict[str, Any], kind: str, imported: dict[str, Any]) -> bool:
        """Return True when an Excel row would not change the current disposition.

        Excel imports can include every row from a downloaded template. Skipping
        unchanged rows avoids creating duplicate historical disposition records
        and keeps GREMLIN.db queries fast after spreadsheet-based dispositioning.
        """

        default_class = "CORRECTIVE_WO" if kind == "wo" else "PM"
        current_category = current_row.get("disposition_category") or "UNKNOWN"
        current_class = current_row.get("effective_record_class") or default_class
        current_notes = self._excel_text(current_row.get("disposition_notes") or current_row.get("disposition_text"))
        current_include = bool(current_row.get("include_in_weibull_candidate"))
        imported_include = imported.get("include_in_weibull_candidate")
        imported_include_bool = current_include if imported_include is None else bool(imported_include)

        if (
            current_category != imported.get("disposition_category")
            or current_class != imported.get("record_class_final")
            or current_notes != self._excel_text(imported.get("disposition_text"))
            or current_include != imported_include_bool
        ):
            return False

        if kind == "pm":
            return (
                (current_row.get("pm_reset_inclusion_decision") or "NEEDS_REVIEW") == imported.get("pm_reset_decision")
                and self._excel_text(current_row.get("pm_reset_renewal_rationale")) == self._excel_text(imported.get("pm_reset_rationale"))
                and self._optional_int_value(current_row.get("reset_target_failure_mode_id")) == self._optional_int_value(imported.get("reset_target_failure_mode_id"))
                and self._optional_int_value(current_row.get("reset_target_failure_mechanism_id")) == self._optional_int_value(imported.get("reset_target_failure_mechanism_id"))
            )

        current_mode_id = self._optional_int_value(current_row.get("failure_mode_id"))
        current_mechanism_id = self._optional_int_value(current_row.get("failure_mechanism_id"))
        imported_mode_id = self._optional_int_value(imported.get("failure_mode_id"))
        imported_mechanism_id = self._optional_int_value(imported.get("failure_mechanism_id"))
        return (
            current_mode_id == imported_mode_id
            and current_mechanism_id == imported_mechanism_id
            and (imported_mode_id is not None or self._normalize_taxonomy_text(imported.get("failure_mode_text")) == self._normalize_taxonomy_text(current_row.get("failure_mode")))
            and (imported_mechanism_id is not None or self._normalize_taxonomy_text(imported.get("failure_mechanism_text")) == self._normalize_taxonomy_text(current_row.get("failure_mechanism")))
        )

    def _optional_int_value(self, value: Any) -> int | None:
        if value in (None, ""):
            return None
        return int(value)

    def _disposition_excel_validation_data(self, asset_number: str, kind: str, headers: tuple[str, ...]) -> tuple[list[ExcelValidation], list[list[Any]]]:
        """Build dropdown and type-validation metadata for disposition Excel exports."""

        category_options = PM_DISPOSITION_CATEGORIES if kind == "pm" else WO_DISPOSITION_CATEGORIES
        record_class_options = (
            ("PM", "PM_RESET_CANDIDATE", "INSPECTION", "PARTS_ORDER", "ADMINISTRATIVE", "PROJECT_WORK", "UNKNOWN")
            if kind == "pm"
            else ("CORRECTIVE_WO", "PM", "INSPECTION", "PARTS_ORDER", "ADMINISTRATIVE", "PROJECT_WORK", "UNKNOWN")
        )
        failure_mode_options = [row["failure_mode_name"] for row in self.get_asset_failure_mode_options(asset_number)]
        failure_mechanism_options = [row["failure_mechanism_name"] for row in self.get_asset_failure_mechanism_options(asset_number)]

        lookup_columns: list[tuple[str, tuple[Any, ...] | list[Any]]] = [
            ("disposition_category", category_options),
            ("record_class", record_class_options),
            ("include_in_weibull_candidate", ("TRUE", "FALSE")),
        ]
        if kind == "pm":
            lookup_columns.extend([
                ("pm_reset_decision", PM_RESET_DECISIONS),
                ("reset_target_failure_mode", failure_mode_options),
                ("reset_target_failure_mechanism", failure_mechanism_options),
            ])
        else:
            lookup_columns.extend([
                ("failure_mode", failure_mode_options),
                ("failure_mechanism", failure_mechanism_options),
            ])

        max_lookup_rows = max((len(values) for _, values in lookup_columns), default=0)
        lookup_rows: list[list[Any]] = []
        for row_index in range(max_lookup_rows + 1):
            row_values: list[Any] = []
            for label, values in lookup_columns:
                row_values.append(label if row_index == 0 else (values[row_index - 1] if row_index - 1 < len(values) else ""))
            lookup_rows.append(row_values)

        list_validations: list[ExcelValidation] = []
        for lookup_index, (column_name, values) in enumerate(lookup_columns, start=1):
            if column_name not in headers or not values:
                continue
            lookup_column = self._xlsx_column_name(lookup_index)
            formula = f"'Lookup Lists'!${lookup_column}$2:${lookup_column}${len(values) + 1}"
            list_validations.append(
                ExcelValidation(
                    column_name=column_name,
                    validation_type="list",
                    formula1=formula,
                    show_error=column_name not in {"failure_mode", "failure_mechanism"},
                    error=f"Select an allowed {column_name.replace('_', ' ')} value from the dropdown.",
                )
            )

        integer_validations = [
            ExcelValidation(
                column_name=column_name,
                validation_type="whole",
                operator="greaterThanOrEqual",
                formula1="0",
                error=f"{column_name} must be a whole-number ID.",
            )
            for column_name in (
                "mapped_record_id",
                "failure_mode_id",
                "failure_mechanism_id",
                "reset_target_failure_mode_id",
                "reset_target_failure_mechanism_id",
            )
            if column_name in headers
        ]
        return [*list_validations, *integer_validations], lookup_rows

    def _write_xlsx(self, output_path: str | Path, rows: list[list[Any]], sheet_name: str, *, validations: list[ExcelValidation] | None = None, lookup_rows: list[list[Any]] | None = None) -> None:
        """Write a simple Excel-compatible .xlsx workbook using only the standard library."""

        include_lookup_sheet = bool(lookup_rows)
        with zipfile.ZipFile(output_path, "w", compression=zipfile.ZIP_DEFLATED) as workbook:
            workbook.writestr("[Content_Types].xml", self._xlsx_content_types(include_lookup_sheet=include_lookup_sheet))
            workbook.writestr("_rels/.rels", self._xlsx_root_rels())
            workbook.writestr("xl/workbook.xml", self._xlsx_workbook_xml(sheet_name, include_lookup_sheet=include_lookup_sheet))
            workbook.writestr("xl/_rels/workbook.xml.rels", self._xlsx_workbook_rels(include_lookup_sheet=include_lookup_sheet))
            workbook.writestr("xl/worksheets/sheet1.xml", self._xlsx_sheet_xml(rows, validations=validations))
            if include_lookup_sheet:
                workbook.writestr("xl/worksheets/sheet2.xml", self._xlsx_sheet_xml(lookup_rows or []))

    def _read_xlsx(self, input_path: str | Path) -> list[tuple[Any, ...]]:
        """Read values from the first worksheet of an .xlsx workbook."""

        with zipfile.ZipFile(input_path) as workbook:
            shared_strings = self._xlsx_shared_strings(workbook)
            workbook_xml = ET.fromstring(workbook.read("xl/workbook.xml"))
            namespace = {"main": "http://schemas.openxmlformats.org/spreadsheetml/2006/main", "rel": "http://schemas.openxmlformats.org/officeDocument/2006/relationships"}
            first_sheet = workbook_xml.find("main:sheets/main:sheet", namespace)
            sheet_path = "xl/worksheets/sheet1.xml"
            if first_sheet is not None:
                relationship_id = first_sheet.attrib.get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id")
                rels = ET.fromstring(workbook.read("xl/_rels/workbook.xml.rels"))
                for rel in rels:
                    if rel.attrib.get("Id") == relationship_id:
                        target = rel.attrib.get("Target", "worksheets/sheet1.xml")
                        sheet_path = f"xl/{target.lstrip('/')}" if not target.startswith("xl/") else target
                        break
            sheet = ET.fromstring(workbook.read(sheet_path))
        namespace = {"main": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
        parsed_rows: list[tuple[Any, ...]] = []
        for row in sheet.findall(".//main:sheetData/main:row", namespace):
            values: list[Any] = []
            for cell in row.findall("main:c", namespace):
                column_index = self._xlsx_column_index(cell.attrib.get("r", "A1"))
                while len(values) < column_index:
                    values.append(None)
                values.append(self._xlsx_cell_value(cell, shared_strings))
            parsed_rows.append(tuple(values))
        return parsed_rows

    def _xlsx_content_types(self, *, include_lookup_sheet: bool = False) -> str:
        sheet2 = '\n<Override PartName="/xl/worksheets/sheet2.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>' if include_lookup_sheet else ""
        return f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>{sheet2}
</Types>'''

    def _xlsx_root_rels(self) -> str:
        return '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>'''

    def _xlsx_workbook_xml(self, sheet_name: str, *, include_lookup_sheet: bool = False) -> str:
        safe_sheet = escape(sheet_name[:31] or "Dispositions")
        lookup_sheet = '<sheet name="Lookup Lists" sheetId="2" state="hidden" r:id="rId2"/>' if include_lookup_sheet else ""
        return f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
<sheets><sheet name="{safe_sheet}" sheetId="1" r:id="rId1"/>{lookup_sheet}</sheets>
</workbook>'''

    def _xlsx_workbook_rels(self, *, include_lookup_sheet: bool = False) -> str:
        lookup_rel = '\n<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet2.xml"/>' if include_lookup_sheet else ""
        return f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>{lookup_rel}
</Relationships>'''

    def _xlsx_sheet_xml(self, rows: list[list[Any]], *, validations: list[ExcelValidation] | None = None) -> str:
        xml_rows = []
        for row_index, row in enumerate(rows, start=1):
            cells = []
            for column_index, value in enumerate(row, start=1):
                reference = f"{self._xlsx_column_name(column_index)}{row_index}"
                if value in (None, ""):
                    cells.append(f'<c r="{reference}"/>')
                elif isinstance(value, bool):
                    cells.append(f'<c r="{reference}" t="b"><v>{1 if value else 0}</v></c>')
                elif isinstance(value, (int, float)):
                    cells.append(f'<c r="{reference}"><v>{value}</v></c>')
                else:
                    cells.append(f'<c r="{reference}" t="inlineStr"><is><t>{escape(str(value))}</t></is></c>')
            xml_rows.append(f'<row r="{row_index}">{"".join(cells)}</row>')
        validation_xml = self._xlsx_data_validations(rows[0] if rows else [], validations or [])
        return f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
<sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/></sheetView></sheetViews>
<sheetData>{"".join(xml_rows)}</sheetData>{validation_xml}
</worksheet>'''

    def _xlsx_data_validations(self, headers: list[Any], validations: list[ExcelValidation]) -> str:
        header_indexes = {self._normalize_excel_header(header): index for index, header in enumerate(headers, start=1)}
        validation_nodes = []
        for validation in validations:
            column_index = header_indexes.get(self._normalize_excel_header(validation.column_name))
            if column_index is None:
                continue
            column_letter = self._xlsx_column_name(column_index)
            attributes = [
                f'type="{escape(validation.validation_type)}"',
                f'allowBlank="{1 if validation.allow_blank else 0}"',
                f'showErrorMessage="{1 if validation.show_error else 0}"',
                f'errorTitle="{escape(validation.error_title)}"',
                f'error="{escape(validation.error)}"',
                f'sqref="{column_letter}2:{column_letter}1048576"',
            ]
            if validation.operator:
                attributes.insert(1, f'operator="{escape(validation.operator)}"')
            validation_nodes.append(f'<dataValidation {" ".join(attributes)}><formula1>{escape(validation.formula1)}</formula1></dataValidation>')
        if not validation_nodes:
            return ""
        return f'<dataValidations count="{len(validation_nodes)}">{"".join(validation_nodes)}</dataValidations>'

    def _xlsx_shared_strings(self, workbook: zipfile.ZipFile) -> list[str]:
        if "xl/sharedStrings.xml" not in workbook.namelist():
            return []
        root = ET.fromstring(workbook.read("xl/sharedStrings.xml"))
        namespace = {"main": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
        values = []
        for item in root.findall("main:si", namespace):
            values.append("".join(text.text or "" for text in item.findall(".//main:t", namespace)))
        return values

    def _xlsx_cell_value(self, cell: ET.Element, shared_strings: list[str]) -> Any:
        namespace = {"main": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
        cell_type = cell.attrib.get("t")
        if cell_type == "inlineStr":
            return "".join(text.text or "" for text in cell.findall(".//main:t", namespace))
        value = cell.find("main:v", namespace)
        raw = value.text if value is not None else ""
        if cell_type == "s":
            return shared_strings[int(raw)] if raw else ""
        if cell_type == "b":
            return raw == "1"
        return self._xlsx_numeric_value(raw)

    def _xlsx_numeric_value(self, raw: str) -> Any:
        if raw == "":
            return ""
        try:
            number = float(raw)
        except ValueError:
            return raw
        if number.is_integer():
            return int(number)
        return number

    def _xlsx_column_name(self, index: int) -> str:
        name = ""
        while index:
            index, remainder = divmod(index - 1, 26)
            name = chr(65 + remainder) + name
        return name

    def _xlsx_column_index(self, cell_reference: str) -> int:
        letters = re.sub(r"[^A-Z]", "", cell_reference.upper())
        index = 0
        for letter in letters:
            index = index * 26 + ord(letter) - 64
        return max(index - 1, 0)

    def _normalize_excel_header(self, value: Any) -> str:
        return re.sub(r"\s+", "_", str(value or "").strip().lower())

    def _excel_text(self, value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, float) and value.is_integer():
            return str(int(value))
        return str(value).strip()

    def _excel_required_int(self, value: Any, field_name: str, row_number: int) -> int:
        parsed = self._excel_optional_int(value)
        if parsed is None:
            display_value = self._excel_text(value) or "blank"
            raise ValueError(f"Excel row {row_number} {field_name} must be a whole number; got {display_value!r}.")
        return parsed

    def _excel_optional_int(self, value: Any) -> int | None:
        text = self._excel_text(value).replace(",", "")
        if not text:
            return None
        if not re.fullmatch(r"[-+]?\d+(?:\.0+)?", text):
            return None
        return int(float(text))

    def _excel_optional_bool(self, value: Any) -> bool | None:
        if value in (None, ""):
            return None
        if isinstance(value, bool):
            return value
        text = self._excel_text(value).lower()
        return text in {"1", "true", "yes", "y", "checked", "include"}

    def _asset_failure_mode_id(self, asset_number: str, failure_mode_text: str) -> int | None:
        normalized = self._normalize_taxonomy_text(failure_mode_text)
        if not normalized:
            return None
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT fm.failure_mode_id
                FROM asset_failure_mode_option afmo
                JOIN failure_mode fm ON fm.failure_mode_id = afmo.failure_mode_id
                WHERE afmo.asset_number = ? AND afmo.is_active = 1 AND lower(fm.failure_mode_name) = lower(?)
                ORDER BY fm.failure_mode_id LIMIT 1
                """,
                (asset_number, normalized),
            ).fetchone()
        return int(row["failure_mode_id"]) if row else None

    def _asset_failure_mechanism_id(self, asset_number: str, failure_mechanism_text: str) -> int | None:
        normalized = self._normalize_taxonomy_text(failure_mechanism_text)
        if not normalized:
            return None
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT fmech.failure_mechanism_id
                FROM asset_failure_mechanism_option afmo
                JOIN failure_mechanism fmech ON fmech.failure_mechanism_id = afmo.failure_mechanism_id
                WHERE afmo.asset_number = ? AND afmo.is_active = 1 AND lower(fmech.failure_mechanism_name) = lower(?)
                ORDER BY fmech.failure_mechanism_id LIMIT 1
                """,
                (asset_number, normalized),
            ).fetchone()
        return int(row["failure_mechanism_id"]) if row else None

    def _normalize_taxonomy_text(self, text: str | None) -> str:
        return re.sub(r"\s+", " ", str(text or "").strip())

    def _lookup_failure_mode_id(self, conn: sqlite3.Connection, text: str) -> int | None:
        row = conn.execute(
            "SELECT failure_mode_id FROM failure_mode WHERE lower(failure_mode_name) = lower(?) ORDER BY failure_mode_id LIMIT 1",
            (text,),
        ).fetchone()
        return int(row["failure_mode_id"]) if row else None

    def _lookup_failure_mechanism_id(self, conn: sqlite3.Connection, text: str, failure_mode_id: int | None = None) -> int | None:
        if failure_mode_id is not None:
            # Only reuse a mechanism that belongs to the selected failure mode or
            # is mode-agnostic (NULL). A mechanism owned by a different mode must
            # not be returned, so the caller creates a new one under the selected
            # mode and (mode, mechanism) populations stay separated.
            row = conn.execute(
                """
                SELECT failure_mechanism_id FROM failure_mechanism
                WHERE lower(failure_mechanism_name) = lower(?)
                  AND (failure_mode_id = ? OR failure_mode_id IS NULL)
                ORDER BY
                    CASE WHEN failure_mode_id = ? THEN 0 ELSE 1 END,
                    failure_mechanism_id
                LIMIT 1
                """,
                (text, failure_mode_id, failure_mode_id),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT failure_mechanism_id FROM failure_mechanism WHERE lower(failure_mechanism_name) = lower(?) ORDER BY failure_mechanism_id LIMIT 1",
                (text,),
            ).fetchone()
        return int(row["failure_mechanism_id"]) if row else None

    def _lookup_failure_mechanism_id_by_name(self, conn: sqlite3.Connection, text: str) -> int | None:
        row = conn.execute(
            "SELECT failure_mechanism_id FROM failure_mechanism WHERE lower(failure_mechanism_name) = lower(?) ORDER BY failure_mechanism_id LIMIT 1",
            (text,),
        ).fetchone()
        return int(row["failure_mechanism_id"]) if row else None

    def get_asset_failure_mode_options(self, asset_number: str) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT fm.failure_mode_id, fm.failure_mode_name
                FROM asset_failure_mode_option afmo
                JOIN failure_mode fm ON fm.failure_mode_id = afmo.failure_mode_id
                WHERE afmo.asset_number = ? AND afmo.is_active = 1 AND fm.is_active = 1
                ORDER BY afmo.use_count DESC, fm.failure_mode_name ASC
                """,
                (asset_number,),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_asset_failure_mechanism_options(self, asset_number: str, failure_mode_id: int | None = None) -> list[dict[str, Any]]:
        params: list[Any] = [asset_number]
        mode_filter = ""
        if failure_mode_id is not None:
            mode_filter = "AND (afmo.failure_mode_id = ? OR fmech.failure_mode_id = ? OR afmo.failure_mode_id IS NULL OR fmech.failure_mode_id IS NULL)"
            params.extend([failure_mode_id, failure_mode_id])
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT fmech.failure_mechanism_id, fmech.failure_mechanism_name, COALESCE(afmo.failure_mode_id, fmech.failure_mode_id) AS failure_mode_id
                FROM asset_failure_mechanism_option afmo
                JOIN failure_mechanism fmech ON fmech.failure_mechanism_id = afmo.failure_mechanism_id
                WHERE afmo.asset_number = ? AND afmo.is_active = 1 AND fmech.is_active = 1 {mode_filter}
                ORDER BY afmo.use_count DESC, fmech.failure_mechanism_name ASC
                """,
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def failure_modes(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("SELECT failure_mode_id, failure_mode_name FROM failure_mode WHERE is_active = 1 ORDER BY failure_mode_name").fetchall()
        return [dict(row) for row in rows]

    def failure_mechanisms(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("SELECT failure_mechanism_id, failure_mechanism_name, failure_mode_id FROM failure_mechanism WHERE is_active = 1 ORDER BY failure_mechanism_name").fetchall()
        return [dict(row) for row in rows]

    def _upsert_failure_mode_for_asset(self, conn: sqlite3.Connection, asset_number: str, failure_mode_text: str, source_event_disposition_id: int | None = None) -> int:
        normalized = self._normalize_taxonomy_text(failure_mode_text)
        if not normalized:
            raise ValueError("Failure mode is required for this disposition.")
        failure_mode_id = self._lookup_failure_mode_id(conn, normalized)
        if failure_mode_id is None:
            failure_mode_id = int(conn.execute("INSERT INTO failure_mode(failure_mode_name) VALUES (?)", (normalized,)).lastrowid)
        if source_event_disposition_id is not None:
            self._touch_asset_failure_mode(conn, asset_number, failure_mode_id, source_event_disposition_id)
        return failure_mode_id

    def _touch_asset_failure_mode(self, conn: sqlite3.Connection, asset_number: str, failure_mode_id: int, source_event_disposition_id: int | None) -> None:
        conn.execute(
            """
            INSERT INTO asset_failure_mode_option(asset_number, failure_mode_id, first_source_event_disposition_id, last_used_event_disposition_id)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(asset_number, failure_mode_id) DO UPDATE SET
                use_count = use_count + 1,
                is_active = 1,
                last_used_at = datetime('now'),
                last_used_event_disposition_id = excluded.last_used_event_disposition_id
            """,
            (asset_number, failure_mode_id, source_event_disposition_id, source_event_disposition_id),
        )

    def _upsert_failure_mechanism_for_asset(self, conn: sqlite3.Connection, asset_number: str, failure_mechanism_text: str, failure_mode_id: int | None, source_event_disposition_id: int | None = None) -> int:
        normalized = self._normalize_taxonomy_text(failure_mechanism_text)
        if not normalized:
            raise ValueError("Failure mechanism text was empty.")
        failure_mechanism_id = self._lookup_failure_mechanism_id(conn, normalized, failure_mode_id)
        if failure_mechanism_id is None:
            try:
                failure_mechanism_id = int(conn.execute("INSERT INTO failure_mechanism(failure_mechanism_name, failure_mode_id) VALUES (?, ?)", (normalized, failure_mode_id)).lastrowid)
            except sqlite3.IntegrityError:
                failure_mechanism_id = self._lookup_failure_mechanism_id_by_name(conn, normalized)
                if failure_mechanism_id is None:
                    raise
        elif failure_mode_id is not None:
            conn.execute("UPDATE failure_mechanism SET failure_mode_id = COALESCE(failure_mode_id, ?) WHERE failure_mechanism_id = ?", (failure_mode_id, failure_mechanism_id))
        if source_event_disposition_id is not None:
            self._touch_asset_failure_mechanism(conn, asset_number, failure_mechanism_id, failure_mode_id, source_event_disposition_id)
        return failure_mechanism_id

    def _touch_asset_failure_mechanism(self, conn: sqlite3.Connection, asset_number: str, failure_mechanism_id: int, failure_mode_id: int | None, source_event_disposition_id: int | None) -> None:
        conn.execute(
            """
            INSERT INTO asset_failure_mechanism_option(asset_number, failure_mechanism_id, failure_mode_id, first_source_event_disposition_id, last_used_event_disposition_id)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(asset_number, failure_mechanism_id) DO UPDATE SET
                failure_mode_id = COALESCE(excluded.failure_mode_id, asset_failure_mechanism_option.failure_mode_id),
                use_count = use_count + 1,
                is_active = 1,
                last_used_at = datetime('now'),
                last_used_event_disposition_id = excluded.last_used_event_disposition_id
            """,
            (asset_number, failure_mechanism_id, failure_mode_id, source_event_disposition_id, source_event_disposition_id),
        )

    def _delete_population_weibull_artifacts(self, conn: sqlite3.Connection, population_id: int) -> None:
        """Remove generated Weibull rows for a population before rebuilding events.

        Event processing and observation rows are regenerated from current
        dispositions each time analysis runs.  Existing analysis datasets keep
        foreign keys back to the old observations, so they must be removed in
        dependency order before the old observations/events can be replaced.
        """

        dataset_ids = [
            int(row["analysis_dataset_id"])
            for row in conn.execute(
                "SELECT analysis_dataset_id FROM analysis_dataset WHERE modeled_population_id = ?",
                (population_id,),
            ).fetchall()
        ]
        run_ids: list[int] = []
        result_ids: list[int] = []
        adjustment_ids: list[int] = []
        if dataset_ids:
            placeholders = ",".join("?" for _ in dataset_ids)
            run_ids = [
                int(row["weibull_analysis_run_id"])
                for row in conn.execute(
                    f"SELECT weibull_analysis_run_id FROM weibull_analysis_run WHERE analysis_dataset_id IN ({placeholders})",
                    dataset_ids,
                ).fetchall()
            ]
        if run_ids:
            placeholders = ",".join("?" for _ in run_ids)
            result_ids = [
                int(row["weibull_result_id"])
                for row in conn.execute(
                    f"SELECT weibull_result_id FROM weibull_result WHERE weibull_analysis_run_id IN ({placeholders})",
                    run_ids,
                ).fetchall()
            ]
        if result_ids:
            placeholders = ",".join("?" for _ in result_ids)
            adjustment_ids = [
                int(row["parameter_adjustment_id"])
                for row in conn.execute(
                    f"SELECT parameter_adjustment_id FROM weibull_parameter_adjustment WHERE weibull_result_id IN ({placeholders})",
                    result_ids,
                ).fetchall()
            ]
            conn.execute(f"DELETE FROM approved_weibull_parameter WHERE weibull_result_id IN ({placeholders})", result_ids)
            conn.execute(f"DELETE FROM weibull_parameter_adjustment WHERE weibull_result_id IN ({placeholders})", result_ids)
            conn.execute(f"DELETE FROM weibull_result WHERE weibull_result_id IN ({placeholders})", result_ids)
        if adjustment_ids:
            placeholders = ",".join("?" for _ in adjustment_ids)
            conn.execute(f"DELETE FROM approved_weibull_parameter WHERE parameter_adjustment_id IN ({placeholders})", adjustment_ids)
        conn.execute("DELETE FROM approved_weibull_parameter WHERE approved_modeled_population_id = ?", (population_id,))
        if run_ids:
            placeholders = ",".join("?" for _ in run_ids)
            conn.execute(f"DELETE FROM kaplan_meier_point WHERE weibull_analysis_run_id IN ({placeholders})", run_ids)
            conn.execute(f"DELETE FROM weibull_curve_point WHERE weibull_analysis_run_id IN ({placeholders})", run_ids)
            conn.execute(f"DELETE FROM weibull_analysis_run WHERE weibull_analysis_run_id IN ({placeholders})", run_ids)
        if dataset_ids:
            placeholders = ",".join("?" for _ in dataset_ids)
            conn.execute(f"DELETE FROM analysis_dataset_member WHERE analysis_dataset_id IN ({placeholders})", dataset_ids)
            conn.execute(f"DELETE FROM analysis_dataset WHERE analysis_dataset_id IN ({placeholders})", dataset_ids)
        conn.execute("DELETE FROM weibull_observation WHERE modeled_population_id = ?", (population_id,))

    def upsert_failure_mode_for_asset(self, asset_number: str, failure_mode_text: str, source_event_disposition_id: int) -> int:
        with self.connect() as conn:
            return self._upsert_failure_mode_for_asset(conn, asset_number, failure_mode_text, source_event_disposition_id)

    def upsert_failure_mechanism_for_asset(self, asset_number: str, failure_mechanism_text: str, failure_mode_id: int | None, source_event_disposition_id: int) -> int:
        with self.connect() as conn:
            return self._upsert_failure_mechanism_for_asset(conn, asset_number, failure_mechanism_text, failure_mode_id, source_event_disposition_id)

    def get_or_create_modeled_population(self, asset_number: str, failure_mode_id: int, failure_mechanism_id: int | None = None) -> int:
        with self.connect() as conn:
            return self._get_or_create_modeled_population(conn, asset_number, failure_mode_id, failure_mechanism_id)

    def _get_or_create_modeled_population(self, conn: sqlite3.Connection, asset_number: str, failure_mode_id: int, failure_mechanism_id: int | None = None) -> int:
        mode = conn.execute(
            "SELECT failure_mode_name FROM failure_mode WHERE failure_mode_id = ? AND is_active = 1",
            (failure_mode_id,),
        ).fetchone()
        if mode is None:
            raise ValueError(f"Selected failure mode id {failure_mode_id} no longer exists. Re-save the disposition with a valid failure mode before running Weibull analysis.")
        mech = None
        if failure_mechanism_id is not None:
            mech = conn.execute(
                "SELECT failure_mechanism_name FROM failure_mechanism WHERE failure_mechanism_id = ? AND is_active = 1",
                (failure_mechanism_id,),
            ).fetchone()
            if mech is None:
                raise ValueError(f"Selected failure mechanism id {failure_mechanism_id} no longer exists. Re-save the disposition with a valid failure mechanism before running Weibull analysis.")
        row = conn.execute(
            """
            SELECT modeled_population_id FROM modeled_population
            WHERE asset_number = ? AND failure_mode_id = ? AND ((failure_mechanism_id IS NULL AND ? IS NULL) OR failure_mechanism_id = ?)
            ORDER BY modeled_population_id LIMIT 1
            """,
            (asset_number, failure_mode_id, failure_mechanism_id, failure_mechanism_id),
        ).fetchone()
        if row:
            return int(row["modeled_population_id"])
        asset = conn.execute("SELECT asset_name FROM mapped_cmms_record WHERE asset_number = ? AND asset_name IS NOT NULL LIMIT 1", (asset_number,)).fetchone()
        grouping = "FAILURE_MECHANISM" if failure_mechanism_id else "FAILURE_MODE"
        name_parts = [asset_number, mode["failure_mode_name"] if mode else f"Failure mode {failure_mode_id}"]
        if mech:
            name_parts.append(mech["failure_mechanism_name"])
        population_name = " - ".join(name_parts)
        return int(conn.execute(
            """
            INSERT INTO modeled_population(population_name, asset_number, asset_name, failure_mode_id, failure_mechanism_id, grouping_level_used, population_definition)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (population_name, asset_number, asset["asset_name"] if asset else None, failure_mode_id, failure_mechanism_id, grouping, f"{grouping} population for asset {asset_number}."),
        ).lastrowid)

    def save_disposition(self, mapped_record_id: int, *, kind: str, disposition_category: str, disposition_text: str = "", record_class_final: str | None = None, pm_reset_decision: str | None = None, pm_reset_rationale: str = "", failure_mode_id: int | None = None, failure_mechanism_id: int | None = None, failure_mode_text: str = "", failure_mechanism_text: str = "", reset_target_failure_mode_id: int | None = None, reset_target_failure_mechanism_id: int | None = None, include_in_weibull_candidate: bool | None = None) -> None:
        """Insert a new current disposition, retiring any previous current row."""

        with self.write_connection() as conn:
            self._save_disposition_with_conn(
                conn,
                mapped_record_id,
                kind=kind,
                disposition_category=disposition_category,
                disposition_text=disposition_text,
                record_class_final=record_class_final,
                pm_reset_decision=pm_reset_decision,
                pm_reset_rationale=pm_reset_rationale,
                failure_mode_id=failure_mode_id,
                failure_mechanism_id=failure_mechanism_id,
                failure_mode_text=failure_mode_text,
                failure_mechanism_text=failure_mechanism_text,
                reset_target_failure_mode_id=reset_target_failure_mode_id,
                reset_target_failure_mechanism_id=reset_target_failure_mechanism_id,
                include_in_weibull_candidate=include_in_weibull_candidate,
            )

    def save_dispositions(self, dispositions: Iterable[dict[str, Any]]) -> int:
        """Insert multiple current dispositions in one SQLite transaction."""

        saved = 0
        with self.write_connection() as conn:
            for disposition in dispositions:
                mapped_record_id = int(disposition["mapped_record_id"])
                kwargs = {key: value for key, value in disposition.items() if key != "mapped_record_id"}
                self._save_disposition_with_conn(conn, mapped_record_id, **kwargs)
                saved += 1
            if saved:
                conn.execute("PRAGMA optimize")
        return saved

    def _save_disposition_with_conn(self, conn: sqlite3.Connection, mapped_record_id: int, *, kind: str, disposition_category: str, disposition_text: str = "", record_class_final: str | None = None, pm_reset_decision: str | None = None, pm_reset_rationale: str = "", failure_mode_id: int | None = None, failure_mechanism_id: int | None = None, failure_mode_text: str = "", failure_mechanism_text: str = "", reset_target_failure_mode_id: int | None = None, reset_target_failure_mechanism_id: int | None = None, include_in_weibull_candidate: bool | None = None) -> None:
        """Insert a disposition using an existing connection/transaction."""

        if kind not in {"wo", "pm"}:
            raise ValueError("Disposition kind must be 'wo' or 'pm'.")
        if kind == "pm" and disposition_category == "INCLUDED_FAILURE":
            raise ValueError("PM records must never be saved as INCLUDED_FAILURE.")
        if kind == "wo" and disposition_category == "INCLUDED_PM_RESET_EVENT":
            raise ValueError("WO records cannot be saved as INCLUDED_PM_RESET_EVENT from the WO disposition screen.")
        if kind == "wo" and disposition_category not in WO_DISPOSITION_CATEGORIES:
            raise ValueError(f"Unsupported WO disposition category: {disposition_category}")
        if kind == "pm" and disposition_category not in PM_DISPOSITION_CATEGORIES:
            raise ValueError(f"Unsupported PM disposition category: {disposition_category}")
        if kind == "pm" and record_class_final == "CORRECTIVE_WO":
            raise ValueError("Corrective WO is not selectable for PM disposition records.")
        notes = disposition_text.strip()
        rationale = pm_reset_rationale.strip()
        if disposition_category in {"HELD_AMBIGUOUS", "EXCLUDED_MIXED_CONTAMINATING"} and not notes:
            raise ValueError(f"{disposition_category} requires disposition notes.")
        if kind == "pm" and pm_reset_decision == "REJECTED_RESET" and disposition_category != "REJECTED_PM_RESET":
            raise ValueError("REJECTED_RESET PM decisions must use disposition category REJECTED_PM_RESET.")
        if kind == "pm" and pm_reset_decision == "CONTEXT_ONLY" and disposition_category != "PM_CONTEXT_ONLY":
            raise ValueError("CONTEXT_ONLY PM decisions must use disposition category PM_CONTEXT_ONLY.")
        if kind == "pm" and disposition_category == "INCLUDED_PM_RESET_EVENT":
            if pm_reset_decision != "APPROVED_RESET":
                raise ValueError("INCLUDED_PM_RESET_EVENT requires APPROVED_RESET.")
            if reset_target_failure_mode_id is None:
                raise ValueError("INCLUDED_PM_RESET_EVENT requires a reset target failure mode.")
            if not rationale:
                raise ValueError("INCLUDED_PM_RESET_EVENT requires PM reset renewal rationale/evidence.")
        if kind == "pm" and pm_reset_decision == "APPROVED_RESET" and not rationale:
            raise ValueError("APPROVED_RESET requires PM reset renewal rationale/evidence.")


        record = conn.execute("SELECT asset_number FROM mapped_cmms_record WHERE mapped_record_id = ?", (mapped_record_id,)).fetchone()
        if not record:
            raise ValueError("Mapped record was not found.")
        asset_number = record["asset_number"]
        if not asset_number:
            raise ValueError("Mapped record has no asset_number; cannot save REL disposition.")

        if kind == "wo":
            if failure_mode_id is None and self._normalize_taxonomy_text(failure_mode_text):
                failure_mode_id = self._upsert_failure_mode_for_asset(conn, asset_number, failure_mode_text)
            elif failure_mode_id is not None:
                self._touch_asset_failure_mode(conn, asset_number, failure_mode_id, None)
            if failure_mechanism_id is None and self._normalize_taxonomy_text(failure_mechanism_text):
                failure_mechanism_id = self._upsert_failure_mechanism_for_asset(conn, asset_number, failure_mechanism_text, failure_mode_id)
            elif failure_mechanism_id is not None:
                self._touch_asset_failure_mechanism(conn, asset_number, failure_mechanism_id, failure_mode_id, None)
        else:
            failure_mode_id = None
            failure_mechanism_id = None
            if reset_target_failure_mode_id is not None and not conn.execute(
                "SELECT 1 FROM asset_failure_mode_option WHERE asset_number = ? AND failure_mode_id = ? AND is_active = 1",
                (asset_number, reset_target_failure_mode_id),
            ).fetchone():
                raise ValueError("PM reset target failure mode must already be a WO-dispositioned option for this asset.")
            if reset_target_failure_mechanism_id is not None:
                mechanism_option = conn.execute(
                    """
                    SELECT fmech.failure_mode_id AS mechanism_mode_id
                    FROM asset_failure_mechanism_option afmo
                    JOIN failure_mechanism fmech ON fmech.failure_mechanism_id = afmo.failure_mechanism_id
                    WHERE afmo.asset_number = ? AND afmo.failure_mechanism_id = ? AND afmo.is_active = 1 AND fmech.is_active = 1
                    """,
                    (asset_number, reset_target_failure_mechanism_id),
                ).fetchone()
                if mechanism_option is None:
                    raise ValueError("PM reset target failure mechanism must already be a WO-dispositioned option for this asset.")
                mechanism_mode_id = mechanism_option["mechanism_mode_id"]
                if (
                    reset_target_failure_mode_id is not None
                    and mechanism_mode_id is not None
                    and int(mechanism_mode_id) != int(reset_target_failure_mode_id)
                ):
                    raise ValueError(
                        "PM reset target failure mechanism does not belong to the selected reset target failure mode. "
                        "Choose a mechanism that was dispositioned under that failure mode."
                    )

        modeled_population_id = None
        if kind == "wo" and failure_mode_id is not None:
            modeled_population_id = self._get_or_create_modeled_population(conn, asset_number, failure_mode_id, failure_mechanism_id)
        if kind == "pm" and reset_target_failure_mode_id is not None:
            modeled_population_id = self._get_or_create_modeled_population(conn, asset_number, reset_target_failure_mode_id, reset_target_failure_mechanism_id)

        if kind == "wo" and disposition_category in {"INCLUDED_FAILURE", "INCLUDED_CENSORED_ASSET_EVENT"} and failure_mode_id is None:
            raise ValueError(f"{disposition_category} requires failure mode and modeled population.")
        if kind == "pm" and disposition_category == "INCLUDED_PM_RESET_EVENT" and modeled_population_id is None:
            raise ValueError("INCLUDED_PM_RESET_EVENT requires modeled population.")

        default_weibull = (kind == "wo" and disposition_category == "INCLUDED_FAILURE") or (kind == "pm" and disposition_category == "INCLUDED_PM_RESET_EVENT" and pm_reset_decision == "APPROVED_RESET")
        include_weibull = int(default_weibull if include_in_weibull_candidate is None else bool(include_in_weibull_candidate))
        if disposition_category in {"EXCLUDED_NON_FAILURE", "EXCLUDED_MIXED_CONTAMINATING"} or pm_reset_decision in {"REJECTED_RESET", "CONTEXT_ONLY"}:
            include_weibull = 0
        include_processing = int(disposition_category in {"INCLUDED_FAILURE", "INCLUDED_CENSORED_ASSET_EVENT", "INCLUDED_PM_RESET_EVENT"})
        if kind == "pm" and not record_class_final:
            record_class_final = "PM"
        if kind == "wo" and not record_class_final:
            record_class_final = "CORRECTIVE_WO"
        if kind == "pm" and record_class_final not in {"PM", "PM_RESET_CANDIDATE", "INSPECTION", "PARTS_ORDER", "ADMINISTRATIVE", "PROJECT_WORK", "UNKNOWN"}:
            raise ValueError("Unsupported PM record class.")

        conn.execute("UPDATE event_disposition SET is_current = 0 WHERE mapped_record_id = ? AND is_current = 1", (mapped_record_id,))
        event_disposition_id = int(conn.execute(
            """
            INSERT INTO event_disposition(
                mapped_record_id, modeled_population_id, record_class_final, disposition_category, include_in_event_processing,
                include_in_weibull_candidate, failure_mode_id, failure_mechanism_id,
                reset_target_failure_mode_id, reset_target_failure_mechanism_id, pm_reset_inclusion_decision,
                pm_reset_renewal_rationale, disposition_text, disposition_notes, is_current
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
            """,
            (
                mapped_record_id, modeled_population_id, record_class_final, disposition_category, include_processing,
                include_weibull, failure_mode_id, failure_mechanism_id, reset_target_failure_mode_id,
                reset_target_failure_mechanism_id, pm_reset_decision, rationale, notes, notes,
            ),
        ).lastrowid)
        if kind == "wo" and failure_mode_id is not None:
            self._touch_asset_failure_mode(conn, asset_number, failure_mode_id, event_disposition_id)
        if kind == "wo" and failure_mechanism_id is not None:
            self._touch_asset_failure_mechanism(conn, asset_number, failure_mechanism_id, failure_mode_id, event_disposition_id)
        conn.execute("UPDATE mapped_cmms_record SET record_class_final = ? WHERE mapped_record_id = ?", (record_class_final, mapped_record_id))


    def perform_weibull_analysis(
        self,
        asset_number: str,
        *,
        grouping_level: str,
        failure_mode_id: int,
        failure_mechanism_id: int | None = None,
    ) -> AnalysisResultView:
        if grouping_level not in {"FAILURE_MODE", "FAILURE_MECHANISM"}:
            raise ValueError("Select a failure mode or failure mechanism before performing Weibull analysis.")
        if grouping_level == "FAILURE_MECHANISM" and failure_mechanism_id is None:
            raise ValueError("Failure-mechanism Weibull analysis requires a selected failure mechanism.")
        cutoff = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        with self.write_connection() as conn:
            population_id = self._get_or_create_modeled_population(conn, asset_number, failure_mode_id, failure_mechanism_id if grouping_level == "FAILURE_MECHANISM" else None)
            population = conn.execute(
                "SELECT population_name, grouping_level_used FROM modeled_population WHERE modeled_population_id = ?",
                (population_id,),
            ).fetchone()
            life_basis_id = self._life_basis_id(conn)
            schedule_class_id = self._schedule_class_id(conn)
            self._refresh_event_processing(conn, asset_number, population_id, grouping_level=grouping_level, failure_mode_id=failure_mode_id, failure_mechanism_id=failure_mechanism_id)
            observation_ids = self._refresh_observations(conn, asset_number, population_id, life_basis_id, schedule_class_id, cutoff)
            if not observation_ids:
                raise ValueError("No valid life intervals could be built from dispositioned event dates for the selected failure group.")
            observations = conn.execute(
                f"""
                SELECT weibull_observation_id, observation_type, start_datetime, end_datetime, analysis_cutoff_datetime,
                       life_hours_for_weibull, failure_indicator, is_right_censored, weibull_life_note
                FROM weibull_observation
                WHERE weibull_observation_id IN ({','.join('?' for _ in observation_ids)}) AND is_usable = 1
                ORDER BY life_hours_for_weibull, weibull_observation_id
                """,
                observation_ids,
            ).fetchall()
            observation_views = [dict(row) for row in observations]
            for index, observation in enumerate(observation_views, start=1):
                observation["ordered_index"] = index
            data = [(float(row["life_hours_for_weibull"]), int(row["failure_indicator"])) for row in observations if row["life_hours_for_weibull"] and row["life_hours_for_weibull"] > 0]
            if not data:
                raise ValueError("No positive life-hour observations are available for the selected failure group.")
            if not any(failure for _, failure in data):
                raise ValueError("At least one INCLUDED_FAILURE observation is required to estimate Weibull MLE beta/eta for the selected failure group.")
            beta, eta, log_likelihood = self._fit_weibull_2p(data)
            beta_lo, beta_hi, eta_lo, eta_hi = self._weibull_confidence_intervals(data, beta, eta)
            mean_time_to_failure = eta * math.gamma(1 + 1 / beta)
            interpretation_summary = self._weibull_interpretation_summary(beta, eta, mean_time_to_failure, beta_lo, beta_hi, eta_lo, eta_hi)
            analysis_label = population["population_name"] if population else f"{asset_number} failure group"
            dataset_id = conn.execute(
                """
                INSERT INTO analysis_dataset(modeled_population_id, asset_number, analysis_name, analysis_cutoff_datetime, life_basis_id, notes)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (population_id, asset_number, f"Weibull Analysis - {analysis_label}", cutoff, life_basis_id, "Generated from current GREMLIN failure-mode/mechanism dispositions."),
            ).lastrowid
            conn.executemany(
                "INSERT INTO analysis_dataset_member(analysis_dataset_id, weibull_observation_id, included_in_fit) VALUES (?, ?, 1)",
                [(dataset_id, row["weibull_observation_id"]) for row in observations],
            )
            run_id = conn.execute(
                "INSERT INTO weibull_analysis_run(analysis_dataset_id, software_version, code_version, notes) VALUES (?, ?, ?, ?)",
                (dataset_id, "GREMLIN PyQt", "life-data-v1", "2P Weibull MLE with right-censored observations for selected failure group."),
            ).lastrowid
            km_points = self._kaplan_meier_points(data)
            conn.executemany(
                """
                INSERT INTO kaplan_meier_point(weibull_analysis_run_id, ordered_index, life_hours, at_risk_count,
                    failure_count_at_time, censored_count_at_time, survival_estimate, cdf_estimate, reliability_estimate,
                    weibull_plot_x, weibull_plot_y)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        run_id,
                        point["ordered_index"],
                        point["life_hours"],
                        point["at_risk_count"],
                        point["failure_count_at_time"],
                        point["censored_count_at_time"],
                        point["survival_estimate"],
                        point["cdf_estimate"],
                        point["reliability_estimate"],
                        point["weibull_plot_x"],
                        point["weibull_plot_y"],
                    )
                    for point in km_points
                ],
            )
            curve_points = self._curve_points(beta, eta, max(t for t, _ in data))
            conn.executemany(
                "INSERT INTO weibull_curve_point(weibull_analysis_run_id, life_hours, cdf, reliability, pdf, hazard_rate) VALUES (?, ?, ?, ?, ?, ?)",
                [(run_id, point["life_hours"], point["cdf"], point["reliability"], point["pdf"], point["hazard_rate"]) for point in curve_points],
            )
            failures = sum(f for _, f in data)
            censored = len(data) - failures
            result_id = conn.execute(
                """
                INSERT INTO weibull_result(weibull_analysis_run_id, beta_mle, eta_mle, beta_lower_ci, beta_upper_ci, eta_lower_ci, eta_upper_ci,
                    log_likelihood, aic, bic, failure_count, censored_count, total_observation_count, mean_time_to_failure, b10_life, b50_life,
                    fit_quality_notes, engineering_interpretation, recommended_action, limitations)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    beta,
                    eta,
                    beta_lo,
                    beta_hi,
                    eta_lo,
                    eta_hi,
                    log_likelihood,
                    4 - 2 * log_likelihood,
                    2 * math.log(len(data)) - 2 * log_likelihood,
                    failures,
                    censored,
                    len(data),
                    mean_time_to_failure,
                    eta * (-math.log(0.90)) ** (1 / beta),
                    eta * (math.log(2)) ** (1 / beta),
                    "MLE fit includes selected failure-group completed failures and right-censored observations.",
                    json.dumps(interpretation_summary),
                    interpretation_summary[0]["recommendation"] if interpretation_summary else "Review the Weibull fit before selecting a maintenance strategy.",
                    "Foundation implementation uses raw elapsed hours and current dispositions for a selected failure mode/mechanism population.",
                ),
            ).lastrowid
            return AnalysisResultView(
                run_id,
                result_id,
                beta,
                eta,
                failures,
                censored,
                len(data),
                km_points,
                curve_points,
                observation_views,
                analysis_label,
                population["grouping_level_used"] if population else grouping_level,
                beta_lo,
                beta_hi,
                eta_lo,
                eta_hi,
                mean_time_to_failure,
                interpretation_summary,
            )

    def _get_or_create_population(self, conn: sqlite3.Connection, asset_number: str) -> int:
        row = conn.execute(
            "SELECT modeled_population_id FROM modeled_population WHERE asset_number = ? AND grouping_level_used = 'ASSET_ONLY' ORDER BY modeled_population_id LIMIT 1",
            (asset_number,),
        ).fetchone()
        if row:
            return int(row["modeled_population_id"])
        asset = conn.execute("SELECT asset_name FROM mapped_cmms_record WHERE asset_number = ? AND asset_name IS NOT NULL LIMIT 1", (asset_number,)).fetchone()
        return int(
            conn.execute(
                """
                INSERT INTO modeled_population(population_name, asset_number, asset_name, grouping_level_used, population_definition, fallback_rationale)
                VALUES (?, ?, ?, 'ASSET_ONLY', ?, ?)
                """,
                (f"Asset {asset_number} Weibull population", asset_number, asset["asset_name"] if asset else None, "Single asset-number population for Life Data Analysis.", "Failure-mode/mechanism grouping can be added after engineering review."),
            ).lastrowid
        )

    def _life_basis_id(self, conn: sqlite3.Connection) -> int:
        return int(conn.execute("SELECT life_basis_id FROM life_basis WHERE life_basis_code = 'RAW_ELAPSED_HOURS'").fetchone()[0])

    def _schedule_class_id(self, conn: sqlite3.Connection) -> int:
        return int(conn.execute("SELECT schedule_class_id FROM asset_schedule_class WHERE schedule_class_code = 'RAW_ELAPSED_ONLY'").fetchone()[0])

    def _refresh_event_processing(
        self,
        conn: sqlite3.Connection,
        asset_number: str,
        population_id: int,
        *,
        grouping_level: str,
        failure_mode_id: int,
        failure_mechanism_id: int | None = None,
    ) -> None:
        self._delete_population_weibull_artifacts(conn, population_id)
        conn.execute("DELETE FROM event_processing_record WHERE asset_number = ? AND modeled_population_id = ?", (asset_number, population_id))
        population_row = conn.execute(
            "SELECT population_name FROM modeled_population WHERE modeled_population_id = ?",
            (population_id,),
        ).fetchone()
        modeled_population_used = (
            population_row["population_name"] if population_row and population_row["population_name"] else f"Asset {asset_number}"
        )
        if grouping_level == "FAILURE_MECHANISM":
            group_filter = """
                AND (
                    (d.disposition_category = 'INCLUDED_FAILURE'
                        AND d.failure_mode_id = :failure_mode_id
                        AND d.failure_mechanism_id = :failure_mechanism_id)
                    OR (d.disposition_category = 'INCLUDED_PM_RESET_EVENT'
                        AND d.reset_target_failure_mode_id = :failure_mode_id
                        AND d.reset_target_failure_mechanism_id = :failure_mechanism_id)
                )
            """
        else:
            group_filter = """
                AND (
                    (d.disposition_category = 'INCLUDED_FAILURE' AND d.failure_mode_id = :failure_mode_id)
                    OR (d.disposition_category = 'INCLUDED_PM_RESET_EVENT' AND d.reset_target_failure_mode_id = :failure_mode_id)
                )
            """
        rows = conn.execute(
            f"""
            SELECT m.*, d.event_disposition_id, d.disposition_category, d.failure_mode_id, d.failure_mechanism_id,
                   d.reset_target_failure_mode_id, d.reset_target_failure_mechanism_id
            FROM mapped_cmms_record m
            JOIN event_disposition d ON d.mapped_record_id = m.mapped_record_id AND d.is_current = 1
            WHERE m.asset_number = :asset_number
              AND d.include_in_event_processing = 1
              AND d.include_in_weibull_candidate = 1
              {group_filter}
            ORDER BY COALESCE(m.completed_date_final, m.start_date_final, m.created_date_final), m.mapped_record_id
            """,
            {"asset_number": asset_number, "failure_mode_id": failure_mode_id, "failure_mechanism_id": failure_mechanism_id},
        ).fetchall()
        previous_id = None
        previous_date = None
        sequence = 0
        for row in rows:
            parsed = self._parse_datetime(row["completed_date_final"] or row["start_date_final"] or row["created_date_final"])
            if not parsed:
                continue
            sequence += 1
            is_failure = row["disposition_category"] == "INCLUDED_FAILURE"
            is_pm_reset = row["disposition_category"] == "INCLUDED_PM_RESET_EVENT"
            role = "FAILURE_EVENT" if is_failure else "PM_RESET_EVENT" if is_pm_reset else "TRACEABILITY_ONLY"
            event_failure_mode_id = row["failure_mode_id"] or row["reset_target_failure_mode_id"]
            event_failure_mechanism_id = row["failure_mechanism_id"] or row["reset_target_failure_mechanism_id"]
            if event_failure_mode_id is not None and not conn.execute("SELECT 1 FROM failure_mode WHERE failure_mode_id = ? AND is_active = 1", (event_failure_mode_id,)).fetchone():
                raise ValueError(f"A current disposition references deleted failure mode id {event_failure_mode_id}. Re-save the affected disposition before running Weibull analysis.")
            if event_failure_mechanism_id is not None and not conn.execute("SELECT 1 FROM failure_mechanism WHERE failure_mechanism_id = ? AND is_active = 1", (event_failure_mechanism_id,)).fetchone():
                if grouping_level == "FAILURE_MECHANISM":
                    raise ValueError(f"A current disposition references deleted failure mechanism id {event_failure_mechanism_id}. Re-save the affected disposition before running Weibull analysis.")
                event_failure_mechanism_id = None
            event_id = conn.execute(
                """
                INSERT INTO event_processing_record(mapped_record_id, event_disposition_id, modeled_population_id, asset_number,
                    asset_name, event_role, completed_date_raw, completed_date_parsed, date_parse_status, failure_mode_id,
                    failure_mechanism_id, grouping_level_used, modeled_population_used, weibull_sequence_number,
                    previous_same_population_event_id, previous_same_population_date, is_failure_event, is_pm_reset_event,
                    is_valid_life_start, is_valid_life_end, weibull_life_note)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'PARSED', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row["mapped_record_id"],
                    row["event_disposition_id"],
                    population_id,
                    asset_number,
                    row["asset_name"],
                    role,
                    row["completed_date_final"],
                    parsed.isoformat(),
                    event_failure_mode_id,
                    event_failure_mechanism_id,
                    grouping_level,
                    modeled_population_used,
                    sequence,
                    previous_id,
                    previous_date,
                    int(is_failure),
                    int(is_pm_reset),
                    int(is_failure or is_pm_reset),
                    int(is_failure),
                    "Current user disposition included this event for REL processing.",
                ),
            ).lastrowid
            previous_id = event_id
            previous_date = parsed.isoformat()

    def _refresh_observations(self, conn: sqlite3.Connection, asset_number: str, population_id: int, life_basis_id: int, schedule_class_id: int, cutoff: str) -> list[int]:
        self._delete_population_weibull_artifacts(conn, population_id)
        conn.execute("DELETE FROM weibull_observation WHERE asset_number = ? AND modeled_population_id = ?", (asset_number, population_id))
        events = conn.execute(
            """
            SELECT event_processing_id, event_role, completed_date_parsed, is_failure_event, is_pm_reset_event
            FROM event_processing_record
            WHERE asset_number = ? AND modeled_population_id = ? AND event_role IN ('FAILURE_EVENT','PM_RESET_EVENT')
            ORDER BY completed_date_parsed, event_processing_id
            """,
            (asset_number, population_id),
        ).fetchall()
        ids: list[int] = []
        previous_event = None
        previous_date = None
        for event in events:
            event_date = self._parse_datetime(event["completed_date_parsed"])
            if not event_date:
                continue
            if previous_date is not None and previous_event is not None:
                hours = (event_date - previous_date).total_seconds() / 3600.0
                if hours > 0:
                    observation_type = "COMPLETED_FAILURE_LIFE" if event["is_failure_event"] else "PM_RESET_CENSORED_LIFE"
                    obs_id = conn.execute(
                        """
                        INSERT INTO weibull_observation(modeled_population_id, asset_number, start_event_processing_id,
                            end_event_processing_id, observation_type, censoring_type, start_datetime, end_datetime,
                            analysis_cutoff_datetime, life_basis_id, schedule_class_id, life_hours_raw_elapsed,
                            life_hours_for_weibull, failure_indicator, is_right_censored, is_usable, weibull_life_note)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
                        """,
                        (
                            population_id,
                            asset_number,
                            previous_event["event_processing_id"],
                            event["event_processing_id"],
                            observation_type,
                            None if event["is_failure_event"] else "RIGHT",
                            previous_date.isoformat(),
                            event_date.isoformat(),
                            cutoff,
                            life_basis_id,
                            schedule_class_id,
                            hours,
                            hours,
                            int(event["is_failure_event"]),
                            int(not event["is_failure_event"]),
                            "Life interval between current valid life-start event and this end/reset event.",
                        ),
                    ).lastrowid
                    ids.append(int(obs_id))
            previous_event = event
            previous_date = event_date
        if previous_date is not None and previous_event is not None:
            cutoff_dt = self._parse_datetime(cutoff) or datetime.now(timezone.utc)
            hours = (cutoff_dt - previous_date).total_seconds() / 3600.0
            if hours > 0:
                obs_id = conn.execute(
                    """
                    INSERT INTO weibull_observation(modeled_population_id, asset_number, start_event_processing_id,
                        observation_type, censoring_type, start_datetime, analysis_cutoff_datetime, life_basis_id,
                        schedule_class_id, life_hours_raw_elapsed, life_hours_for_weibull, failure_indicator,
                        is_right_censored, is_usable, weibull_life_note)
                    VALUES (?, ?, ?, 'RIGHT_CENSORED_LIFE', 'RIGHT', ?, ?, ?, ?, ?, ?, 0, 1, 1, ?)
                    """,
                    (population_id, asset_number, previous_event["event_processing_id"], previous_date.isoformat(), cutoff, life_basis_id, schedule_class_id, hours, hours, "Right-censored life from last valid start/reset/failure event to analysis cutoff."),
                ).lastrowid
                ids.append(int(obs_id))
        return ids

    def _parse_datetime(self, value: Any) -> datetime | None:
        if value in (None, ""):
            return None
        text = str(value).strip().replace("Z", "+00:00")
        formats = ("%Y-%m-%d", "%Y-%m-%d %H:%M:%S", "%m/%d/%Y", "%m/%d/%Y %H:%M", "%m/%d/%y", "%m/%d/%y %H:%M")
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            dt = None
            for fmt in formats:
                try:
                    dt = datetime.strptime(text, fmt)
                    break
                except ValueError:
                    pass
            if dt is None:
                return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)

    def _fit_weibull_2p(self, data: list[tuple[float, int]]) -> tuple[float, float, float]:
        failures = [t for t, failed in data if failed]
        all_times = [t for t, _ in data]
        d = len(failures)
        if d == 0:
            raise ValueError("Cannot fit Weibull without failures.")
        mean_log_fail = sum(math.log(t) for t in failures) / d

        def score(beta: float) -> float:
            weights = [t**beta for t in all_times]
            weighted_log = sum(w * math.log(t) for w, t in zip(weights, all_times)) / sum(weights)
            return (1 / beta) + mean_log_fail - weighted_log

        lo, hi = 0.1, 20.0
        prev_x = lo
        prev_y = score(prev_x)
        bracket = None
        for i in range(1, 400):
            x = lo + (hi - lo) * i / 399
            y = score(x)
            if prev_y == 0 or y == 0 or prev_y * y < 0:
                bracket = (prev_x, x)
                break
            prev_x, prev_y = x, y
        if bracket is None:
            beta = max(0.1, min(20.0, 1.2 / (self._coefficient_of_variation(failures) or 1.0)))
        else:
            a, b = bracket
            for _ in range(80):
                mid = (a + b) / 2
                if score(a) * score(mid) <= 0:
                    b = mid
                else:
                    a = mid
            beta = (a + b) / 2
        eta = (sum(t**beta for t in all_times) / d) ** (1 / beta)
        ll = sum(math.log(beta) - beta * math.log(eta) + (beta - 1) * math.log(t) for t in failures) - sum((t / eta) ** beta for t in all_times)
        return beta, eta, ll

    def _weibull_log_likelihood_from_log_params(self, data: list[tuple[float, int]], log_beta: float, log_eta: float) -> float:
        beta = math.exp(log_beta)
        eta = math.exp(log_eta)
        failures = [t for t, failed in data if failed]
        all_times = [t for t, _ in data]
        if not failures or beta <= 0 or eta <= 0:
            return float("-inf")
        return sum(math.log(beta) - beta * math.log(eta) + (beta - 1) * math.log(t) for t in failures) - sum((t / eta) ** beta for t in all_times)

    def _weibull_confidence_intervals(self, data: list[tuple[float, int]], beta: float, eta: float) -> tuple[float | None, float | None, float | None, float | None]:
        """Approximate 95% parameter CIs from the observed information matrix.

        The finite-difference Hessian is evaluated on log(beta), log(eta) so
        interval endpoints remain positive after exponentiation.
        """

        if beta <= 0 or eta <= 0 or len(data) < 3:
            return None, None, None, None
        theta_beta = math.log(beta)
        theta_eta = math.log(eta)
        h_beta = max(1e-4, abs(theta_beta) * 1e-4)
        h_eta = max(1e-4, abs(theta_eta) * 1e-4)
        f00 = self._weibull_log_likelihood_from_log_params(data, theta_beta, theta_eta)
        if not math.isfinite(f00):
            return None, None, None, None
        try:
            fpp = self._weibull_log_likelihood_from_log_params(data, theta_beta + h_beta, theta_eta)
            fmm = self._weibull_log_likelihood_from_log_params(data, theta_beta - h_beta, theta_eta)
            gee = self._weibull_log_likelihood_from_log_params(data, theta_beta, theta_eta + h_eta)
            gww = self._weibull_log_likelihood_from_log_params(data, theta_beta, theta_eta - h_eta)
            fp_ge = self._weibull_log_likelihood_from_log_params(data, theta_beta + h_beta, theta_eta + h_eta)
            fp_gw = self._weibull_log_likelihood_from_log_params(data, theta_beta + h_beta, theta_eta - h_eta)
            fm_ge = self._weibull_log_likelihood_from_log_params(data, theta_beta - h_beta, theta_eta + h_eta)
            fm_gw = self._weibull_log_likelihood_from_log_params(data, theta_beta - h_beta, theta_eta - h_eta)
            h11 = (fpp - 2 * f00 + fmm) / (h_beta**2)
            h22 = (gee - 2 * f00 + gww) / (h_eta**2)
            h12 = (fp_ge - fp_gw - fm_ge + fm_gw) / (4 * h_beta * h_eta)
            info11, info12, info22 = -h11, -h12, -h22
            determinant = info11 * info22 - info12 * info12
            if not all(math.isfinite(value) for value in (info11, info12, info22, determinant)):
                return None, None, None, None
            if determinant <= 0 or info11 <= 0 or info22 <= 0:
                return None, None, None, None
            var_log_beta = info22 / determinant
            var_log_eta = info11 / determinant
            if not all(math.isfinite(value) for value in (var_log_beta, var_log_eta)):
                return None, None, None, None
            if var_log_beta <= 0 or var_log_eta <= 0:
                return None, None, None, None
            z = 1.959963984540054
            se_log_beta = math.sqrt(var_log_beta)
            se_log_eta = math.sqrt(var_log_eta)
            return (
                math.exp(theta_beta - z * se_log_beta),
                math.exp(theta_beta + z * se_log_beta),
                math.exp(theta_eta - z * se_log_eta),
                math.exp(theta_eta + z * se_log_eta),
            )
        except (OverflowError, ValueError, ZeroDivisionError):
            return None, None, None, None

    def _weibull_interpretation_summary(
        self,
        beta: float,
        eta: float,
        mean_life: float,
        beta_lo: float | None,
        beta_hi: float | None,
        eta_lo: float | None,
        eta_hi: float | None,
    ) -> list[dict[str, str]]:
        rows = [
            {"metric": "Beta", "value": f"{beta:.4g}", "recommendation": self._beta_recommendation(beta)},
            {"metric": "Eta", "value": f"{eta:.4g} hours", "recommendation": self._eta_recommendation(beta, eta)},
            {"metric": "MTTF", "value": f"{mean_life:.4g} hours", "recommendation": self._mttf_recommendation(beta, mean_life)},
        ]
        if beta_lo is not None and beta_hi is not None:
            rows.append({"metric": "Beta 95% CI", "value": f"{beta_lo:.4g} to {beta_hi:.4g}", "recommendation": self._beta_ci_recommendation(beta_lo, beta_hi, beta)})
        else:
            rows.append({"metric": "Beta 95% CI", "value": "Not available", "recommendation": "The beta confidence interval could not be estimated from this dataset. Treat the failure-pattern conclusion cautiously and review sample size, censoring, and data quality."})
        if eta_lo is not None and eta_hi is not None:
            rows.append({"metric": "Eta 95% CI", "value": f"{eta_lo:.4g} to {eta_hi:.4g} hours", "recommendation": self._eta_ci_recommendation(eta_lo, eta_hi, eta)})
        else:
            rows.append({"metric": "Eta 95% CI", "value": "Not available", "recommendation": "The eta confidence interval could not be estimated from this dataset. Use eta directionally only until the fit and underlying data are reviewed."})
        return rows

    def _beta_recommendation(self, beta: float) -> str:
        if beta < 0.9:
            return "This pattern does not support jumping straight to age-based replacement. The better action is to investigate installation quality, setup variation, commissioning practices, repair quality, and latent defects that are being introduced into the population. Focus on defect elimination and standard work before spending effort on PM interval optimization."
        if beta <= 1.1:
            return "This result is more consistent with a random failure pattern, so a fixed replacement age is usually weak justification by itself. The better path is to improve detectability through inspection or condition checks, confirm whether the consequence of failure is acceptable, and use spare planning or run-to-failure logic where appropriate."
        return "This result supports wear-out behavior, so it is reasonable to evaluate age-based PM or planned replacement before the wear-out region becomes economically painful. Use this with eta, downtime impact, and replacement cost to decide whether a planned interval is justified and where that interval should be set."

    def _eta_recommendation(self, beta: float, eta: float) -> str:
        if beta > 1.1:
            return "Use eta as a practical planning reference because it represents the life where a large share of the population has failed. Do not treat it as an automatic replacement point, but use it to frame where intervention should probably occur relative to downtime cost, maintenance burden, and operational risk."
        return "Use eta primarily as a comparison and forecasting metric across similar populations rather than a strict intervention point. It is still useful for communicating relative life and planning spares, but by itself it is not strong justification for a replacement interval when the failure pattern is not clearly wear-out."

    def _mttf_recommendation(self, beta: float, mean_life: float) -> str:
        if beta > 1.1:
            return "Use MTTF as a high-level planning number for budgeting, manpower, and spare demand, but do not set PM timing from MTTF alone. The actual maintenance decision should still be anchored by the failure pattern shown by beta and the life scale shown by eta."
        return "Use MTTF mainly for planning and communication, not as a stand-alone maintenance trigger. In non-wear-out populations, replacing at the average life can create unnecessary work without materially reducing failures."

    def _beta_ci_recommendation(self, beta_lo: float, beta_hi: float, beta: float) -> str:
        if beta_lo < 1 and beta_hi > 1:
            return "The interval crossing 1.0 means the governing failure pattern is still uncertain. Do not overstate the conclusion. Before locking into a maintenance strategy, check whether the population is mixed, whether the bucket is too broad, and whether more failure observations are needed to stabilize the estimate."
        if beta_hi - beta_lo <= 0.3:
            return "The interval is relatively tight, which means the beta interpretation is more stable and more defensible. You can place more confidence in the recommended maintenance direction, while still confirming that the grouping makes physical sense."
        return "The interval is wide enough that the beta interpretation should be treated with caution. Review whether this bucket mixes different mechanisms, whether data quality is weak, or whether the sample size is still too small to confidently choose a maintenance strategy."

    def _eta_ci_recommendation(self, eta_lo: float, eta_hi: float, eta: float) -> str:
        if eta <= 0:
            return "Do not use eta for decisions until the underlying fit and data are reviewed."
        if (eta_hi - eta_lo) / eta <= 0.4:
            return "The interval is reasonably tight, so eta is stable enough to use for planning comparisons, maintenance timing discussions, and communication with stakeholders."
        return "The interval is wide, so avoid pretending there is a precise intervention point. Use eta as directional guidance only, and consider tightening the population or collecting more data before converting it into a hard decision."

    def _coefficient_of_variation(self, values: list[float]) -> float | None:
        if not values:
            return None
        mean = sum(values) / len(values)
        if mean <= 0:
            return None
        return (sum((v - mean) ** 2 for v in values) / len(values)) ** 0.5 / mean

    def _kaplan_meier_points(self, data: list[tuple[float, int]]) -> list[dict[str, Any]]:
        grouped: dict[float, dict[str, int]] = {}
        for time, failed in data:
            group = grouped.setdefault(time, {"failures": 0, "censored": 0})
            if failed:
                group["failures"] += 1
            else:
                group["censored"] += 1
        survival = 1.0
        at_risk = len(data)
        points = []
        index = 0
        for time in sorted(grouped):
            failures = grouped[time]["failures"]
            censored = grouped[time]["censored"]
            if failures and at_risk > 0:
                survival *= max(0.0, 1 - failures / at_risk)
                cdf = 1 - survival
                x = math.log(time)
                y = math.log(-math.log(max(survival, 1e-12))) if 0 < survival < 1 else None
                index += 1
                points.append(
                    {
                        "ordered_index": index,
                        "life_hours": time,
                        "at_risk_count": at_risk,
                        "failure_count_at_time": failures,
                        "censored_count_at_time": censored,
                        "survival_estimate": survival,
                        "cdf_estimate": cdf,
                        "reliability_estimate": survival,
                        "weibull_plot_x": x,
                        "weibull_plot_y": y,
                    }
                )
            at_risk -= failures + censored
        return points

    def _curve_points(self, beta: float, eta: float, max_time: float) -> list[dict[str, float]]:
        upper = max(max_time * 1.15, eta * 1.25, 1.0)
        points = []
        for i in range(1, 81):
            t = upper * i / 80
            z = (t / eta) ** beta
            reliability = math.exp(-z)
            cdf = 1 - reliability
            pdf = (beta / eta) * (t / eta) ** (beta - 1) * reliability
            hazard = (beta / eta) * (t / eta) ** (beta - 1)
            points.append({"life_hours": t, "cdf": cdf, "reliability": reliability, "pdf": pdf, "hazard_rate": hazard})
        return points

    def save_parameter_adjustment(self, weibull_result_id: int, adjusted_beta: float, adjusted_eta: float, reason: str = "") -> int:
        with self.write_connection() as conn:
            conn.execute("UPDATE weibull_parameter_adjustment SET is_current = 0 WHERE weibull_result_id = ? AND is_current = 1", (weibull_result_id,))
            return int(
                conn.execute(
                    """
                    INSERT INTO weibull_parameter_adjustment(weibull_result_id, adjusted_beta, adjusted_eta, adjustment_reason, is_current)
                    VALUES (?, ?, ?, ?, 1)
                    """,
                    (weibull_result_id, adjusted_beta, adjusted_eta, reason),
                ).lastrowid
            )
