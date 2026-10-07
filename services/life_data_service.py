"""SQLite-backed Life Data Analysis workflow services for GREMLIN.

This module extends the existing ``GREMLIN.db`` raw import database in-place.
It never modifies ``raw_cmms_record.raw_json``; instead, raw JSON is parsed into
REL-style mapped, disposition, event-processing, and Weibull analysis tables.
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import random
import re
import sqlite3
import threading
import zipfile
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone, tzinfo
from pathlib import Path
from contextlib import contextmanager
from typing import Any, Iterable, Iterator
from xml.sax.saxutils import escape
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
import xml.etree.ElementTree as ET

from services.availability_config import DEFAULT_TIMEZONE
from services.wo_narrative import NARRATIVE_FIELDS, NARRATIVE_KEYS, extract_narrative

ROOT_DIR = Path(__file__).resolve().parents[1]

# The plant's standard schedule (REL-WBL-DAT-003 §7): every asset the Weibull
# schedule register does not list is on it.
PLANT_DEFAULT_SCHEDULE_CODE = "20H_MON_FRI"
DEFAULT_WEEKDAY_SCHEDULE_HOURS_PER_DAY = 20.0
# The schedules the register can put an asset on, in the order the Configuration
# page offers them: the plant default, 24 hours on weekdays, and every clock hour for
# an asset that runs through weekends.
ASSIGNABLE_SCHEDULE_CODES = ("20H_MON_FRI", "24H_MON_FRI", "CONTINUOUS")
# The assets GREMLIN used to have written into its code as 24 hours Monday-Friday.
# They seed the register the first time it is created; after that it is the list.
BUILT_IN_24H_ASSET_NUMBERS = ("3101", "3102", "3103", "3104", "3105", "3106", "3107", "3154", "3142", "3023", "3253")
# How far ahead the "most likely to fail" list looks unless the page asks otherwise:
# four weeks, a monthly planning cycle (REL-WBL-MTH-001 §8.1).
RISK_WINDOW_WEEKS = 4

# The fewest lives ending in a failure GREMLIN fits and reports a Weibull
# distribution from: the minimum REL-WBL-MTH-001 §4 sets (REL-WBL-REQ-001
# VV-071). Fewer give a beta that looks precise and is not (maximum likelihood
# overstates beta on small samples), and the interpretation summary would still
# turn it into a maintenance recommendation.
MIN_WEIBULL_FAILURE_LIVES = 5

# The rules a Weibull run is built and fitted by, stamped on every run as
# weibull_analysis_run.code_version so a saved result can be traced to the
# method behind it and one saved under an earlier method is flagged. Bump it
# whenever life construction or the fit changes. v1 dated events completed,
# else start, else created; split days at midnight UTC; had no minimum failure
# count; and fell back to a heuristic beta when the likelihood had no root. v2
# let a PM aimed at one mechanism restart its whole failure mode's lives, and
# didn't let a PM aimed at the whole mode restart the mechanisms under it; v3
# restarts only what a PM restores (REL-WBL-DAT-004 §7).
WEIBULL_METHOD_VERSION = "life-data-v3"

# A life that ends within this many calendar hours of the event that started it
# is flagged for a duplicate check (REL-WBL-DAT-004 §12). Two work orders for one
# breakdown close that close together, and the near-zero life between them pulls
# beta below 1, the reading that steers away from age-based PM. Flagged rather
# than dropped: a genuine repeat failure is real repair-quality information.
DUPLICATE_CHECK_RAW_HOURS = 1.0
# Completed dates that carry no time. The Limble sync stores a timestamp, but an
# exported or hand-entered date can be a bare day, which names a plant-calendar day.
DATE_ONLY_FORMATS = ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y")
# Repeat Fix Rate: a failure is a repeat when it follows the mechanism's last one
# within this many scheduled hours, and a mechanism needs this many intervals before
# its rate is ranked, so one quick repeat out of two failures doesn't top the list.
REPEAT_FIX_DEFAULT_WINDOW_HOURS = 24.0
REPEAT_FIX_MAX_WINDOW_HOURS = 720.0
REPEAT_FIX_MIN_INTERVALS = 5

# The interpretation-summary row for the probability plot's R² (REL-WBL-REQ-001
# VV-070, VV-074), by which a saved summary that predates it is recognised.
R_SQUARED_METRIC = "Probability plot R²"

# How wide a 95% interval can be, as a share of its estimate, for the reading the
# interpretation summary gives to count as stable (REL-WBL-MTH-001 §8). Relative for
# beta as for eta: a beta interval's width grows with beta itself, so a fixed width
# would let early-life results pass and practically never a wear-out one. 70% of beta
# is an upper limit about twice the lower, which about 20 failures reach at any beta.
BETA_INTERVAL_STABLE_FRACTION = 0.70
ETA_INTERVAL_STABLE_FRACTION = 0.40

# The probability-plot R² below which a fit is flagged for engineering review
# (REL-WBL-REQ-001 VV-074): for each number of lives ending in a failure, the R² that
# 90% of genuine two-parameter Weibull samples of that size reach, so a fit under it
# is less straight than chance alone explains. A fixed pass mark would not do: R²
# runs lower on few points, so 0.90 would flag over a third of genuine five-failure
# samples and miss a real problem at fifty. R² does not depend on beta or eta, so one
# table serves every fit. Each value is LifeDataService.simulated_r_squared_threshold
# (failures, samples=20000, seed=1000 + failures); counts in between interpolate.
R_SQUARED_REVIEW_THRESHOLDS = (
    (5, 0.806), (6, 0.812), (7, 0.821), (8, 0.831), (9, 0.840), (10, 0.846),
    (12, 0.861), (15, 0.876), (20, 0.893), (25, 0.905), (30, 0.913), (40, 0.928),
    (50, 0.938), (75, 0.952), (100, 0.961), (150, 0.970), (200, 0.976),
)

_CODE_VERSION: str | None = None


def _git_commit(root: Path) -> str | None:
    """The commit a git checkout at ``root`` has checked out, read from its .git files.

    Read from the files rather than by running git, which the machine GREMLIN is
    deployed on need not have on its PATH. Follows a worktree's ``gitdir:``
    pointer and falls back to packed-refs. None when it cannot tell.
    """

    git_dir = root / ".git"
    try:
        if git_dir.is_file():
            pointer = git_dir.read_text(encoding="utf-8").strip()
            if not pointer.startswith("gitdir:"):
                return None
            git_dir = (root / pointer[len("gitdir:"):].strip()).resolve()
        head = (git_dir / "HEAD").read_text(encoding="utf-8").strip()
        if not head.startswith("ref:"):
            return head[:12] or None
        ref = head[len("ref:"):].strip()
        search = [git_dir]
        common = git_dir / "commondir"
        if common.is_file():
            search.append((git_dir / common.read_text(encoding="utf-8").strip()).resolve())
        for base in search:
            ref_file = base / ref
            if ref_file.is_file():
                return ref_file.read_text(encoding="utf-8").strip()[:12] or None
        for base in search:
            packed = base / "packed-refs"
            if packed.is_file():
                for line in packed.read_text(encoding="utf-8").splitlines():
                    parts = line.split()
                    if len(parts) == 2 and parts[1] == ref:
                        return parts[0][:12]
    except OSError:
        return None
    return None


def gremlin_code_version() -> str:
    """The code a Weibull run was made with, as ``GREMLIN <commit>``.

    GREMLIN_VERSION overrides it, for a deployment that is not a git checkout.
    Read once per process: the code a process runs does not change under it.
    """

    global _CODE_VERSION
    if _CODE_VERSION is None:
        version = os.environ.get("GREMLIN_VERSION", "").strip() or _git_commit(ROOT_DIR) or "version unknown"
        _CODE_VERSION = f"GREMLIN {version}"
    return _CODE_VERSION


class WeibullFitError(ValueError):
    """The lives on hand cannot be fitted: too few failures, or no likelihood root."""

# The one and only location for GREMLIN.db. GREMLIN no longer probes mapped
# drive letters, UNC shares, or user folders for the database; it opens this
# single path. Override with the GREMLIN_DB_PATH environment variable to point
# at a different file.
DEFAULT_DB_PATH = Path(r"C:\GREMLIN\GREMLIN.db")
DB_WRITE_TIMEOUT_SECONDS = 30
_DEFAULT_DB_PATH_SENTINEL = object()
_LOCK_WAIT_CONTEXT = threading.local()

# Every column the Weibull data table shows for one observation, including the source
# CMMS work order fields joined in from the event that closed the life interval: its
# task id / title / downtime / request description / completion notes describe the
# observation. Trailing right-censored "current life" rows have no end event, so those
# columns are NULL there. Recorded downtime below zero is a data-entry artifact, so it
# is clamped to zero the same way failure_mechanism_pareto() clamps it; a missing value
# stays NULL and renders blank rather than as a real zero.
#
# The closing record's mapped_record_id and its event role travel too, so the table can
# open that record's disposition in place: a FAILURE_EVENT is a corrective work order
# and a PM_RESET_EVENT a PM, which is the disposition kind the editor has to ask for.
# So do the raw elapsed hours and the weekend and non-run hours taken out of them, which
# REL-WBL-DAT-004 §5 asks for so a life's hours can be checked against its dates.
#
# Shared by perform_weibull_analysis() (fresh fit) and load_saved_weibull_analysis()
# (read-back of a saved fit), through _load_weibull_view(), so both describe an
# observation identically. Callers append their own WHERE and ORDER BY.
_WEIBULL_OBSERVATION_SELECT = """
    SELECT wo.weibull_observation_id, wo.observation_type, wo.start_datetime, wo.end_datetime,
           wo.analysis_cutoff_datetime, wo.life_hours_for_weibull, wo.failure_indicator,
           wo.is_right_censored, wo.weibull_life_note,
           wo.life_hours_raw_elapsed, wo.excluded_weekend_hours, wo.excluded_schedule_non_run_hours,
           wo.data_quality_assumption_flag,
           m.mapped_record_id AS source_mapped_record_id,
           ep.event_role AS source_event_role,
           m.task_id AS source_task_id,
           m.task_name AS source_work_title,
           m.requestor_description AS source_request_description,
           m.completion_notes AS source_completion_notes,
           m.area_affected AS source_area_affected,
           m.condition_found AS source_condition_found,
           m.cause AS source_cause,
           m.action_taken AS source_action_taken,
           CASE
               WHEN m.downtime_hours IS NULL THEN NULL
               WHEN m.downtime_hours < 0 THEN 0
               ELSE m.downtime_hours
           END AS source_downtime_hours
    FROM weibull_observation wo
    LEFT JOIN event_processing_record ep ON ep.event_processing_id = wo.end_event_processing_id
    LEFT JOIN mapped_cmms_record m ON m.mapped_record_id = ep.mapped_record_id
"""


# One record as the disposition screens show it: the read-only CMMS columns, the
# four narrative boxes, and the current disposition with its taxonomy names.
# Shared by disposition_rows() (the paged table) and disposition_record() (the
# single-record editor the analysis tables open), so a record reads the same in
# both. Callers append their own WHERE, ORDER BY and paging.
_DISPOSITION_ROW_SELECT = """
    SELECT m.mapped_record_id,
           m.task_name AS name,
           m.task_id AS taskID,
           m.created_date_final AS createdDate_Final,
           m.completed_date_final AS completedDate_Final,
           ROUND(m.downtime_hours, 2) AS downtime,
           m.completion_notes AS completionNotes,
           m.request_title AS requestTitle,
           m.requestor_description AS requestorDescription,
           m.area_affected,
           m.condition_found,
           m.cause,
           m.action_taken,
           COALESCE(d.record_class_final, m.record_class_final, m.record_class_auto) AS effective_record_class,
           d.event_disposition_id,
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
"""


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


def database_write_error(exc: sqlite3.Error | OSError, db_path: Path | str) -> DatabaseWriteError:
    """Turn a SQLite/OS write failure into something a plant user can act on.

    Module level so every writer to the shared database reports failures the
    same way. GREMLIN.db lives on a network share and serialises writers through
    ``BEGIN IMMEDIATE``, so "another user is saving", "the share is read-only"
    and "the drive is full" are the everyday failures. A raw
    ``sqlite3.OperationalError: database is locked`` names none of them.
    """

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
        reason = "GREMLIN could not open the database path"
        action = "Confirm the database file above exists and that its folder is reachable."
    elif "disk" in reason_lower or "space" in reason_lower or "full" in reason_lower:
        reason = "the shared drive may be out of space or unavailable"
        action = "Check the shared drive status and free space, then try again."
    else:
        reason = raw_reason
        action = "Try again. If it repeats, send this message to the GREMLIN maintainer."
    return DatabaseWriteError(
        "GREMLIN could not write to the shared database.\n\n"
        f"Database: {db_path}\n"
        f"Reason: {reason}.\n"
        f"What to do: {action}"
    )


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


# Converts a legacy ``downtime_source_unit`` (written by the retired ingestion
# downtime_unit path) to minutes, used only to validate stale provenance.
_DOWNTIME_SOURCE_UNIT_MINUTES = {"minutes": 1.0, "seconds": 1.0 / 60.0, "hours": 60.0}

# Version stamp on every mapped row. Bump it whenever _map_raw_record's output
# changes so already-mapped rows are re-derived from stored raw JSON on the next
# service construction (see _mapped_records_need_remap). v2 == downtime seconds fix;
# v3 == the Area Affected / Condition / Cause / Action narrative boxes, which have to
# be re-read out of raw JSON for every row imported before they were mapped.
# v4 == asset_number / asset_name stored stripped: the asset list shows them
# trimmed and every per-asset query matches that exactly, so a padded " C-3 "
# row was listed as C-3 and then found by nothing.
_MAPPING_VERSION = "v4"

DISPLAY_COLUMNS = (
    "name",
    "taskID",
    "createdDate_Final",
    "completedDate_Final",
    "downtime",
    "completionNotes",
    "requestTitle",
    "requestorDescription",
)

# What each of those columns actually holds, and where it comes from. Every one
# of them reaches the screen as text -- SQLite has no date type, and the CMMS
# hands over task ids as strings -- so a screen that is not told the real type
# compares them all as text, which puts 10 before 9 and orders dates by the
# digits they happen to start with. Both halves read this: the ORDER BY built in
# _disposition_sort_expressions, and the column menus in life_data_analysis.js,
# which are handed the types over the API.
COLUMN_TYPE_TEXT = "text"
COLUMN_TYPE_NUMBER = "number"
COLUMN_TYPE_DATETIME = "datetime"
COLUMN_TYPE_BOOLEAN = "boolean"

# Keyed by DISPLAY_COLUMNS, so every column the table draws is one it can be
# sorted by (test_disposition_sorting holds the two together).
DISPLAY_COLUMN_SOURCES = {
    "name": ("m.task_name", COLUMN_TYPE_TEXT),
    "taskID": ("m.task_id", COLUMN_TYPE_NUMBER),
    "createdDate_Final": ("m.created_date_final", COLUMN_TYPE_DATETIME),
    "completedDate_Final": ("m.completed_date_final", COLUMN_TYPE_DATETIME),
    "downtime": ("m.downtime_hours", COLUMN_TYPE_NUMBER),
    "completionNotes": ("m.completion_notes", COLUMN_TYPE_TEXT),
    "requestTitle": ("m.request_title", COLUMN_TYPE_TEXT),
    "requestorDescription": ("m.requestor_description", COLUMN_TYPE_TEXT),
}

# The four structured text boxes the maintenance teams fill out on a work order,
# as the screens address them. They are carried separately from DISPLAY_COLUMNS
# because the two are consumed differently: the tables render these four as one
# stacked "Failure Narrative" cell (four more columns on an already-wide
# disposition table costs about 900px of horizontal scrolling), while Excel keeps
# them as four columns of their own so each can be sorted and filtered.
NARRATIVE_COLUMNS = tuple({"key": field.key, "label": field.label} for field in NARRATIVE_FIELDS)

# What the disposition table shows in Modeled Population for a row that has none
# yet -- the population is created on save, from the asset and the mode/mechanism
# being assigned. It lives here rather than in the client script because it is a
# real value on that column: the cell reads it, the value filter lists it, and
# the ORDER BY has to sort by it, or the column reads "Auto-create..." while
# ordering as though the cell were empty. The screen is handed this over the API
# so there is one copy of it.
MODELED_POPULATION_PLACEHOLDER = "Auto-create from selected asset + mode/mechanism on save"

EXCEL_BASE_COLUMNS = ("mapped_record_id",) + DISPLAY_COLUMNS + NARRATIVE_KEYS
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
# The columns a reader fills in: everything after the record columns, which is
# what import_disposition_excel reads back. The workbook highlights these so the
# sheet says which cells are the reader's without a trip to the explainer -- an
# edit in any other column is discarded on import, silently, and a reader who
# spent an afternoon correcting completion notes finds that out too late.
# mapped_record_id is read back as well, but as the key a row is matched by, not
# a value to change, so it stays with the record columns.
EXCEL_EDITABLE_COLUMNS = frozenset(EXCEL_WO_DISPOSITION_COLUMNS + EXCEL_PM_DISPOSITION_COLUMNS) - frozenset(EXCEL_BASE_COLUMNS)

# What each exported column holds, so the workbook carries Excel's own types
# rather than a sheet of text. Excel sorts, filters and formats by cell type, and
# a column written as text sorts as text there exactly as it used to on the
# screen: "10" ahead of "9", and dates by the digits they happen to start with.
# Worse, a text date cannot be filtered by "last month" or reformatted at all --
# the value is a sentence to Excel, not a day.
#
# The record columns take the types the screen already sorts them by
# (DISPLAY_COLUMN_SOURCES), so the workbook and the table order a column the same
# way; the disposition columns are named here. A value that does not fit its type
# is written as the text it is rather than dropped -- a task id of "A-14" is
# still that task id, and losing it to keep the column tidy is the worse trade.
EXCEL_COLUMN_TYPES: dict[str, str] = {
    "mapped_record_id": COLUMN_TYPE_NUMBER,
    **{key: column_type for key, (_, column_type) in DISPLAY_COLUMN_SOURCES.items()},
    **{key: COLUMN_TYPE_TEXT for key in NARRATIVE_KEYS},
    "disposition_notes": COLUMN_TYPE_TEXT,
    "disposition_category": COLUMN_TYPE_TEXT,
    "record_class": COLUMN_TYPE_TEXT,
    "include_in_weibull_candidate": COLUMN_TYPE_BOOLEAN,
    "failure_mode_id": COLUMN_TYPE_NUMBER,
    "failure_mode": COLUMN_TYPE_TEXT,
    "failure_mechanism_id": COLUMN_TYPE_NUMBER,
    "failure_mechanism": COLUMN_TYPE_TEXT,
    "pm_reset_decision": COLUMN_TYPE_TEXT,
    "reset_target_failure_mode_id": COLUMN_TYPE_NUMBER,
    "reset_target_failure_mode": COLUMN_TYPE_TEXT,
    "reset_target_failure_mechanism_id": COLUMN_TYPE_NUMBER,
    "reset_target_failure_mechanism": COLUMN_TYPE_TEXT,
    "pm_reset_renewal_rationale": COLUMN_TYPE_TEXT,
}

# The largest whole number a spreadsheet can hold without changing it: fifteen
# significant decimal digits, which is what Excel keeps regardless of the double
# underneath. That bites well before the binary limit does -- 2**53 is sixteen
# digits, so an id like 1234567890123456 sits under it and is still shown, and
# saved back, as 1234567890123460, one digit away from the record beside it.
# Rounding a quantity is a rounding; rounding an id is a different record, so
# anything wider stays text, the only form that survives the trip.
EXACT_INTEGER_LIMIT = 10**15 - 1
# The width SQLite stores an INTEGER in, and so the widest value the ordering can
# compare exactly. Handing a Python int past this to a SQL function raises rather
# than rounding quietly, so the sort key falls back to a double there.
SQLITE_INTEGER_LIMIT = 2**63
INTEGER_TEXT = re.compile(r"[-+]?\d+")

# Excel counts a date as a number of days, and a date has to reach the sheet as
# that number, under a date number format, for Excel to be able to treat it as a
# date at all. Which day it counts from depends on where the date falls, because
# Excel has a 1900-02-29 that never happened -- inherited from Lotus 1-2-3 and
# kept for compatibility. From 1900-03-01 the phantom day is already in the count,
# so 1899-12-30 is the epoch that lands on it; before that it is not, and counting
# from 1899-12-30 puts the date a day late (1900-01-01 would be 2 where Excel says
# 1, and 1900-02-28 would be 60, which is the phantom day itself).
#
# The phantom day needs no handling of its own: _parse_datetime refuses
# 1900-02-29 along with every other day that does not exist, so nothing ever
# reaches serial 60.
EXCEL_DATE_EPOCH = datetime(1899, 12, 30, tzinfo=timezone.utc)
EXCEL_DATE_EPOCH_BEFORE_THE_PHANTOM_DAY = datetime(1899, 12, 31, tzinfo=timezone.utc)
EXCEL_PHANTOM_DAY_PASSED = datetime(1900, 3, 1, tzinfo=timezone.utc)
# Excel's first representable date. Anything earlier has no serial at all -- the
# count would go negative, which a date cell cannot show -- so it stays text.
EXCEL_FIRST_DATE = datetime(1900, 1, 1, tzinfo=timezone.utc)

# Indexes into the cellXfs list _xlsx_styles_xml writes, in the order it writes
# them. A cell names the format it is drawn in by index, so the two move together.
EXCEL_STYLE_DEFAULT = 0
EXCEL_STYLE_HEADER = 1
EXCEL_STYLE_DATETIME = 2
EXCEL_STYLE_DECIMAL = 3
# A sheet told which of its columns are editable draws their header and cells
# over a yellow fill, and the header of every other column over grey. The body
# styles repeat the date and decimal formats because a cell has exactly one
# style: an editable cell that needs a number format still needs the fill.
EXCEL_STYLE_READ_ONLY_HEADER = 4
EXCEL_STYLE_EDITABLE_HEADER = 5
EXCEL_STYLE_EDITABLE = 6
EXCEL_STYLE_EDITABLE_DATETIME = 7
EXCEL_STYLE_EDITABLE_DECIMAL = 8

# Characters XML 1.0 cannot carry. Free-text CMMS boxes pick them up from
# copy-pasted terminal output and barcode scanners, and one of them in one
# completion note is the difference between a workbook and a file Excel refuses
# to open ("unreadable content"). Lone surrogates go with them: they cannot even
# be encoded to UTF-8, so they take the whole download down with a
# UnicodeEncodeError rather than merely corrupting it.
# Six of them are separators -- \s matches them, and collapsing runs of
# whitespace turned each into a space long before any of this. Deleting those
# outright would join the words they stood between, so "Bearing\x0bfailure"
# would become one word and stop matching the failure mode it names. They become
# a space; everything else in the set separates nothing and simply goes.
ILLEGAL_XML_WHITESPACE = re.compile(r"[\x0b\x0c\x1c-\x1f]")
ILLEGAL_XML_CHARACTERS = re.compile(r"[\x00-\x08\x0e-\x1b\ud800-\udfff\ufffe\uffff]")


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
    asset_number: str = ""
    b10_life: float | None = None
    b50_life: float | None = None
    # How straight the probability plot's Kaplan-Meier failure points lie (squared
    # correlation in Weibull coordinates): a check on the model, not the fit.
    probability_plot_r_squared: float | None = None
    # The R² this fit is flagged for review below (R_SQUARED_REVIEW_THRESHOLDS at its
    # failure count), and whether it falls below it (REL-WBL-REQ-001 VV-074).
    probability_plot_r_squared_threshold: float | None = None
    probability_plot_review: bool = False
    # The window the lives were built in: the start (None = all history), the
    # cutoff the current life is censored at, and what set the cutoff -- USER, the
    # LAST_IMPORT from Limble, or NOW (no import was recorded later than the data).
    analysis_start: str | None = None
    analysis_cutoff: str | None = None
    analysis_cutoff_source: str | None = None
    # What the life hours count: the life basis, the schedule class, and the time
    # zone days were split in (with the reason when the plant zone could not load).
    life_basis: dict[str, Any] | None = None
    # REL-WBL-DAT-004's event processing table: every event the population's
    # dispositions offered, in date order, including the ones left out and why.
    events: list[dict[str, Any]] = field(default_factory=list)
    pm_reset_censored_count: int = 0
    current_life_censored_count: int = 0
    run_datetime: str | None = None
    method_version: str | None = None
    software_version: str | None = None
    # False for a result saved under an earlier method version or with fewer
    # failure lives than the minimum: shown with a notice, never ranked or reported.
    method_current: bool = True
    meets_minimum: bool = True
    min_failure_lives: int = MIN_WEIBULL_FAILURE_LIVES
    # Why a failure-mode population was fitted rather than a mechanism, which a
    # failure-mode report has to state (REL-WBL-PLN-003 §8).
    fallback_rationale: str | None = None
    # False when the asset has been moved to another Weibull schedule since this
    # result's life hours were counted: shown with a notice naming the schedule it is
    # on now, still ranked but marked, and not reported until it is run again.
    schedule_current: bool = True
    current_schedule_name: str | None = None


class LifeDataService:
    """Owns GREMLIN.db schema, CMMS mapping, disposition, and Weibull analysis."""

    def __init__(self, db_path: Path | str | object = _DEFAULT_DB_PATH_SENTINEL, *, refresh_on_startup: bool = True) -> None:
        if db_path is _DEFAULT_DB_PATH_SENTINEL:
            self.db_path = DEFAULT_DB_PATH
        else:
            self.db_path = Path(db_path)
        self._asset_number_options_cache: list[dict[str, str]] | None = None
        # Building the asset list is a read followed by a store, and the two are
        # not one step: a sync can finish in between, so the store has to be able
        # to tell that what it holds was overtaken while it was being assembled.
        # The counter says which era the data belongs to; the lock keeps the
        # counter and the cache moving together.
        self._asset_cache_lock = threading.Lock()
        self._asset_cache_generation = 0
        self.ensure_schema()
        # Always remap when the stored rows predate the current mapper version,
        # even if refresh_on_startup is disabled (the web app and desktop GUI both
        # build with refresh_on_startup=False). Otherwise a mapping-logic change
        # like the downtime seconds fix would not reach an existing database until
        # someone triggered a manual refresh.
        if refresh_on_startup or self._mapped_records_need_remap():
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
        # SQLite has no date type, and the CMMS dates land in TEXT columns in
        # whichever shape the source wrote them ("2026-01-15T15:00:00+00:00" from
        # the Limble sync, "1/15/2026 15:00" from an older import). Ordering
        # those as text is ordering them by their leading digits, so queries that
        # need a real chronology sort on this instead: it parses the value the
        # same way the analysis code does and returns a fixed-width UTC string,
        # which then compares chronologically as plain text.
        conn.create_function("gremlin_sort_datetime", 1, self._datetime_sort_key, deterministic=True)
        # And the same for the numbers: task ids arrive as TEXT, so ordering them
        # needs to know which of those strings are numbers at all. This is the one
        # answer to that, shared with the Excel export.
        conn.create_function("gremlin_sort_number", 1, self._number_sort_key, deterministic=True)
        # And the tie-breaker behind it, for the integers it has to round.
        conn.create_function("gremlin_sort_integer", 1, self._integer_sort_key, deterministic=True)
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

    @contextmanager
    def read_transaction(self) -> Iterator[sqlite3.Connection]:
        """Open a connection whose every read sees one consistent database snapshot.

        Outside a transaction each SELECT is its own implicit one, so a read built from
        several queries can straddle another process's write and stitch together rows
        that never coexisted -- a parent row from before the write beside child rows
        from after it, or none at all. A deferred ``BEGIN`` takes SQLite's shared read
        lock on the first query and holds it until the transaction ends, which keeps a
        concurrent writer from committing mid-read; the writer waits on its own busy
        timeout, and these reads are short.

        Read-only by contract: nothing is committed, and the rollback on the way out is
        what releases the shared lock.
        """

        conn = self.connect()
        try:
            conn.execute("BEGIN DEFERRED")
            yield conn
        finally:
            conn.rollback()
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
        return database_write_error(exc, self.db_path)


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
                    area_affected TEXT,
                    condition_found TEXT,
                    cause TEXT,
                    action_taken TEXT,
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
                -- The Weibull schedule register: the assets not on the plant default
                -- schedule and the one each is on. Every change to it is kept below,
                -- with who made it, when and why (REL-WBL-DAT-003 §7).
                CREATE TABLE IF NOT EXISTS asset_schedule_assignment (
                    asset_number TEXT PRIMARY KEY,
                    schedule_class_id INTEGER NOT NULL REFERENCES asset_schedule_class(schedule_class_id),
                    changed_by TEXT,
                    changed_at TEXT NOT NULL DEFAULT (datetime('now'))
                );
                CREATE TABLE IF NOT EXISTS asset_schedule_change (
                    asset_schedule_change_id INTEGER PRIMARY KEY,
                    asset_number TEXT NOT NULL,
                    from_schedule_class_code TEXT NOT NULL,
                    to_schedule_class_code TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    changed_by TEXT,
                    changed_at TEXT NOT NULL DEFAULT (datetime('now'))
                );
                CREATE INDEX IF NOT EXISTS idx_asset_schedule_change_asset ON asset_schedule_change(asset_number, changed_at);
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
                    analysis_start_datetime TEXT,
                    analysis_cutoff_source TEXT,
                    schedule_class_id INTEGER REFERENCES asset_schedule_class(schedule_class_id),
                    schedule_time_zone TEXT,
                    schedule_time_zone_warning TEXT,
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
                    probability_plot_r_squared REAL,
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

                CREATE TABLE IF NOT EXISTS weibull_report_log (
                    weibull_report_log_id INTEGER PRIMARY KEY,
                    asset_number TEXT NOT NULL,
                    report_number TEXT NOT NULL,
                    sequence_number INTEGER NOT NULL,
                    analysis_label TEXT,
                    weibull_result_id INTEGER,
                    generated_at TEXT NOT NULL DEFAULT (datetime('now'))
                );
                CREATE INDEX IF NOT EXISTS idx_weibull_report_log_asset ON weibull_report_log(asset_number);
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
                    ("CONTINUOUS", "Continuous, every clock hour", 24.0, 7.0, 0, "Runs around the clock, weekends included."),
                ],
            )
            self._migrate_rel_disposition_schema(conn)
            self._seed_schedule_register(conn)

    def _migrate_rel_disposition_schema(self, conn: sqlite3.Connection) -> None:
        """Safely add REL disposition columns/tables to existing GREMLIN.db files."""

        if self._table_exists(conn, "mapped_cmms_record"):
            required_mapped_columns = {
                "raw_content_hash": "TEXT",
                "downtime_raw": "TEXT",
                "downtime_minutes": "REAL",
                "downtime_hours": "REAL",
                "downtime_backfill_attempted": "INTEGER NOT NULL DEFAULT 0",
                # The four work-order narrative boxes. Existing rows land NULL and
                # are filled by the remap the mapping_version bump below forces,
                # which re-reads them out of raw JSON -- so a database synced before
                # the Limble template change picks them up without a fresh pull.
                "area_affected": "TEXT",
                "condition_found": "TEXT",
                "cause": "TEXT",
                "action_taken": "TEXT",
                # Databases predating this column get 'v1' on every existing row,
                # so the startup remap gate (_mapped_records_need_remap) treats
                # them as stale and re-derives them under the current mapper.
                "mapping_version": "TEXT NOT NULL DEFAULT 'v1'",
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
        if self._table_exists(conn, "analysis_dataset"):
            # The analysis window and the clock a run's lives were counted on, so a
            # saved result can say what it was built from. Datasets saved before
            # these existed read back NULL, which the result view reports as
            # "not recorded" (and, for the time zone, as the UTC v1 used).
            for column, ddl in {
                "analysis_start_datetime": "TEXT",
                "analysis_cutoff_source": "TEXT",
                "schedule_class_id": "INTEGER REFERENCES asset_schedule_class(schedule_class_id)",
                "schedule_time_zone": "TEXT",
                "schedule_time_zone_warning": "TEXT",
            }.items():
                if not self._column_exists(conn, "analysis_dataset", column):
                    conn.execute(f"ALTER TABLE analysis_dataset ADD COLUMN {column} {ddl}")
        if self._table_exists(conn, "weibull_result") and not self._column_exists(conn, "weibull_result", "probability_plot_r_squared"):
            # Results saved before R² was stored read back NULL; the result view works
            # it out from their stored Kaplan-Meier points instead.
            conn.execute("ALTER TABLE weibull_result ADD COLUMN probability_plot_r_squared REAL")
        if self._table_exists(conn, "failure_mechanism") and self._column_exists(conn, "failure_mechanism", "failure_mode_id"):
            conn.execute("CREATE INDEX IF NOT EXISTS idx_failure_mechanism_failure_mode ON failure_mechanism(failure_mode_id)")
            conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_failure_mechanism_name_mode ON failure_mechanism(failure_mechanism_name, failure_mode_id)")

    @staticmethod
    def _seed_schedule_register(conn: sqlite3.Connection) -> None:
        """Put the assets GREMLIN used to hard-code as 24-hour on that schedule, once.

        Only into a register that has never held anything: once it has a change on
        record, emptying it is a decision somebody made, not a gap to fill again.
        """

        if conn.execute("SELECT 1 FROM asset_schedule_change LIMIT 1").fetchone():
            return
        if conn.execute("SELECT 1 FROM asset_schedule_assignment LIMIT 1").fetchone():
            return
        class_id = conn.execute(
            "SELECT schedule_class_id FROM asset_schedule_class WHERE schedule_class_code = '24H_MON_FRI'"
        ).fetchone()[0]
        for asset_number in BUILT_IN_24H_ASSET_NUMBERS:
            conn.execute(
                "INSERT INTO asset_schedule_assignment(asset_number, schedule_class_id, changed_by) VALUES (?, ?, 'GREMLIN')",
                (asset_number, class_id),
            )
            conn.execute(
                """
                INSERT INTO asset_schedule_change(asset_number, from_schedule_class_code, to_schedule_class_code, reason, changed_by)
                VALUES (?, ?, '24H_MON_FRI', ?, 'GREMLIN')
                """,
                (asset_number, PLANT_DEFAULT_SCHEDULE_CODE, "Carried over from the 24-hour list built into GREMLIN before the register existed."),
            )

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
            downtime_raw, downtime_minutes = self._downtime_from_raw(raw)
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

    def _mapped_records_need_remap(self) -> bool:
        """True when any mapped row predates the current mapper version.

        Drives a one-time remap after a mapping-logic change so already-mapped
        rows are re-derived from stored raw JSON without a manual refresh. Cheap
        after the migration runs: once every row carries the current version the
        check short-circuits on the first mismatch it fails to find.
        """
        with self.connect() as conn:
            if not self._table_exists(conn, "mapped_cmms_record"):
                return False
            if not self._column_exists(conn, "mapped_cmms_record", "mapping_version"):
                return False
            row = conn.execute(
                "SELECT 1 FROM mapped_cmms_record "
                "WHERE mapping_version IS NULL OR mapping_version <> ? LIMIT 1",
                (_MAPPING_VERSION,),
            ).fetchone()
        return row is not None

    def refresh_mapped_cmms_records(self) -> int:
        """Map only new or changed raw JSON records into ``mapped_cmms_record``."""

        # An explicit mapping refresh may be intended to pick up rows already
        # imported or mapped by another GREMLIN process. Drop local asset-list
        # state before any early return so the next dropdown population reads
        # ``mapped_cmms_record`` even when this process has no upserts to make.
        self.invalidate_caches()
        mapping_version = _MAPPING_VERSION
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

    def _get_alias_stripped(self, raw: dict[str, Any], *keys: str) -> Any:
        """``_get_alias`` for an identifying field, without surrounding whitespace.

        The asset list trims what it shows, and the pages ask for exactly what
        was picked from it, so a value stored padded is one no page can find.
        Whitespace-only reads as absent; non-text values pass through as they are.
        """
        value = self._get_alias(raw, *keys)
        if isinstance(value, str):
            return value.strip() or None
        return value

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
        # The Area Affected / Condition / Cause / Action boxes, wherever this
        # payload happens to carry them (see services.wo_narrative).
        narrative = extract_narrative(raw)
        downtime_raw, downtime_minutes = self._downtime_from_raw(raw)
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
            "asset_name": self._get_alias_stripped(raw, "Asset Name"),
            "asset_number": self._get_alias_stripped(raw, "Asset Number"),
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
            **{key: narrative.get(key) for key in NARRATIVE_KEYS},
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
            "mapping_version": _MAPPING_VERSION,
        }

    def _downtime_from_raw(self, raw: dict[str, Any]) -> tuple[Any, float | None]:
        """Return the stored raw ``downtime`` and its value in minutes.

        Most rows carry Limble's raw ``downtime`` in seconds. Rows imported by
        the retired ingestion ``downtime_unit`` path instead stored ``downtime``
        already normalised to minutes and preserved the original value/unit in
        ``downtime_source_value`` / ``downtime_source_unit`` provenance fields.

        ``RawRepository`` clears that provenance whenever a refresh supplies a
        fresh ``downtime`` (see ``_merge_preserved_fields``), so its presence
        marks a row whose ``downtime`` is still the pre-scaled minutes value.
        As a belt-and-suspenders check the already-minutes shortcut is only taken
        when the stored ``downtime`` still matches the recorded source
        relationship; otherwise ``downtime`` is normalised as raw seconds.
        """
        downtime_raw = self._get_alias(raw, "downtime")
        source_unit = self._get_alias(raw, "downtime_source_unit")
        source_value = self._get_alias(raw, "downtime_source_value")
        if source_unit is not None and self._downtime_matches_provenance(downtime_raw, source_value, source_unit):
            try:
                return downtime_raw, float(downtime_raw)
            except (TypeError, ValueError):
                # Not a bare number; fall back to unit-aware text parsing.
                return downtime_raw, self._parse_downtime_minutes(downtime_raw)
        return downtime_raw, self._parse_downtime_minutes(downtime_raw)

    @staticmethod
    def _downtime_matches_provenance(downtime: Any, source_value: Any, source_unit: Any) -> bool:
        """True when ``downtime`` (minutes) still matches its recorded source.

        Guards the already-minutes shortcut against provenance left stale by a
        resync that refreshed ``downtime`` but not the ``downtime_source_*`` keys.
        """
        factor = _DOWNTIME_SOURCE_UNIT_MINUTES.get(str(source_unit).strip().lower())
        if factor is None:
            return False
        try:
            expected_minutes = float(source_value) * factor
            actual_minutes = float(downtime)
        except (TypeError, ValueError):
            return False
        return math.isclose(actual_minutes, expected_minutes, rel_tol=1e-6, abs_tol=1e-6)

    def _parse_downtime_minutes(self, value: Any) -> float | None:
        """Normalise a raw CMMS downtime value to minutes.

        Limble reports task ``downtime`` in **seconds** (e.g. ``12600`` == 3.5 h),
        so a bare numeric value is seconds and must be divided by 60 to reach the
        minutes the rest of the pipeline stores (``downtime_hours`` then divides
        by 60 again). Explicit textual units are still honoured for legacy or
        hand-entered values: ``"3.5 hours"`` -> 210 min, ``"45 min"`` -> 45 min.
        """
        if value in (None, ""):
            return None
        if isinstance(value, (int, float)):
            return float(value) / 60.0
        text = str(value).strip().lower()
        match = re.search(r"[-+]?\d*\.?\d+", text)
        if not match:
            return None
        number = float(match.group())
        if "hour" in text or re.search(r"\bhrs?\b", text):
            return number * 60.0
        if "min" in text:
            return number
        # Bare number or an explicit "seconds" label: the raw Limble unit.
        return number / 60.0

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

    def invalidate_caches(self) -> None:
        """Forget derived state, because the database changed behind this instance.

        The asset list is cached in memory and only dropped by *this* instance's
        own mapping refresh, which is the right rule while this instance is the
        only writer. It stops being true when something else maps into the same
        database -- another GREMLIN process, or an on-demand Limble sync, which
        maps through a service of its own. Without this the app would keep
        serving the asset list from before the import, so a sync that reported
        hundreds of newly mapped records would leave the Life Data page insisting
        the new assets do not exist.

        Safe to call from another thread while a request is mid-build: the
        generation bump tells that build its rows are already out of date, so it
        returns them to its own caller but does not leave them behind as the
        cache. Otherwise the invalidation could be undone a moment after it
        happened, and the stale list would then be served indefinitely -- the
        exact failure this method exists to prevent.
        """

        with self._asset_cache_lock:
            self._asset_number_options_cache = None
            self._asset_cache_generation += 1

    def asset_numbers(self, *, refresh: bool = False) -> list[str]:
        return [row["asset_number"] for row in self.asset_number_options(refresh=refresh)]

    def asset_number_options(self, *, refresh: bool = False) -> list[dict[str, str]]:
        if refresh:
            mapped_count = self.refresh_mapped_cmms_records()
            if mapped_count:
                self.invalidate_caches()
        else:
            # Take the cache once, under the lock. Testing the field and then
            # iterating it are two separate reads of something another thread is
            # now allowed to clear between them, and the reader that lost that
            # race would iterate None -- turning a sync finishing at an unlucky
            # moment into a 500 on the asset list.
            with self._asset_cache_lock:
                cached = self._asset_number_options_cache
            if cached is not None:
                return [dict(option) for option in cached]
            mapped_count = self.ensure_mapped_records_available()
            if mapped_count:
                self.invalidate_caches()
        # Read the era *after* any mapping work above (which invalidates in its
        # own right) and before the query, so that anything invalidating from
        # here on is understood to have happened after these rows were taken.
        with self._asset_cache_lock:
            generation = self._asset_cache_generation
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
        self._store_asset_options(generation, options)
        return options

    def _store_asset_options(self, generation: int, options: list[dict[str, str]]) -> None:
        """Cache a freshly built asset list, unless it was overtaken while building."""

        with self._asset_cache_lock:
            if generation != self._asset_cache_generation:
                return
            self._asset_number_options_cache = [dict(option) for option in options]

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
                failures AS (
                    SELECT wo_failure_mode_id AS failure_mode_id, wo_failure_mechanism_id AS failure_mechanism_id
                    FROM included WHERE disposition_category = 'INCLUDED_FAILURE'
                ),
                resets AS (
                    SELECT pm_failure_mode_id AS failure_mode_id, pm_failure_mechanism_id AS failure_mechanism_id
                    FROM included WHERE disposition_category = 'INCLUDED_PM_RESET_EVENT'
                ),
                -- A PM reset restarts only what it restores: a mode-wide one (no target
                -- mechanism) counts for the mode and every mechanism under it, one aimed
                -- at a mechanism counts for that mechanism alone.
                mode_groups AS (
                    SELECT
                        'FAILURE_MODE' AS grouping_level,
                        f.failure_mode_id,
                        NULL AS failure_mechanism_id,
                        COUNT(*) AS failure_count,
                        (SELECT COUNT(*) FROM resets r
                         WHERE r.failure_mode_id = f.failure_mode_id AND r.failure_mechanism_id IS NULL) AS reset_count
                    FROM failures f
                    GROUP BY f.failure_mode_id
                ),
                mechanism_groups AS (
                    SELECT
                        'FAILURE_MECHANISM' AS grouping_level,
                        f.failure_mode_id,
                        f.failure_mechanism_id,
                        COUNT(*) AS failure_count,
                        (SELECT COUNT(*) FROM resets r
                         WHERE r.failure_mode_id = f.failure_mode_id
                           AND (r.failure_mechanism_id = f.failure_mechanism_id OR r.failure_mechanism_id IS NULL)) AS reset_count
                    FROM failures f
                    WHERE f.failure_mechanism_id IS NOT NULL
                    GROUP BY f.failure_mode_id, f.failure_mechanism_id
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
                # Each life ending in a failure ends at one of these failures, so a group
                # with fewer than the minimum cannot be fitted however its dates fall.
                "fittable": int(row["failure_count"] or 0) >= MIN_WEIBULL_FAILURE_LIVES,
                "min_failure_lives": MIN_WEIBULL_FAILURE_LIVES,
            })
        return options


    def _latest_mechanism_fits(self, asset_number: str) -> list[dict[str, Any]]:
        """The latest saved fit of each failure mechanism on the asset that can be ranked.

        Only fits resting on at least ``MIN_WEIBULL_FAILURE_LIVES`` failure lives: a
        beta from fewer is not one to choose a maintenance strategy by. Each carries
        what both rankings need -- the fit, its R² and review flag, the current life
        at the run's cutoff and the schedule it was counted on -- and whether it is
        still current: saved under today's method, on the asset's current schedule.
        A fit that is not is still ranked, marked, so the page can say it wants
        running again.
        """

        with self.connect() as conn:
            current_schedule_id = self._schedule_class_id(conn, asset_number)
            rows = conn.execute(
                """
                WITH latest_result AS (
                    SELECT
                        ad.modeled_population_id,
                        ad.analysis_dataset_id,
                        ad.analysis_cutoff_datetime,
                        ad.schedule_time_zone,
                        COALESCE(
                            ad.schedule_class_id,
                            (SELECT wo.schedule_class_id FROM weibull_observation wo
                             JOIN analysis_dataset_member adm ON adm.weibull_observation_id = wo.weibull_observation_id
                             WHERE adm.analysis_dataset_id = ad.analysis_dataset_id AND wo.schedule_class_id IS NOT NULL
                             LIMIT 1)
                        ) AS schedule_class_id,
                        wr.weibull_result_id,
                        wr.beta_mle,
                        wr.eta_mle,
                        wr.failure_count,
                        wr.censored_count,
                        wr.probability_plot_r_squared,
                        war.run_datetime,
                        war.code_version,
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
                    mp.failure_mode_id,
                    mp.failure_mechanism_id,
                    fm.failure_mode_name,
                    fmech.failure_mechanism_name,
                    lr.beta_mle,
                    lr.eta_mle,
                    lr.failure_count,
                    lr.censored_count,
                    lr.probability_plot_r_squared,
                    lr.run_datetime,
                    lr.code_version,
                    lr.analysis_cutoff_datetime,
                    lr.schedule_time_zone,
                    lr.schedule_class_id,
                    sc.schedule_class_name,
                    sc.hours_per_day,
                    sc.exclude_weekends,
                    (SELECT wo.life_hours_for_weibull FROM weibull_observation wo
                     JOIN analysis_dataset_member adm ON adm.weibull_observation_id = wo.weibull_observation_id
                     WHERE adm.analysis_dataset_id = lr.analysis_dataset_id AND wo.observation_type = 'RIGHT_CENSORED_LIFE'
                     LIMIT 1) AS current_life_hours
                FROM latest_result lr
                JOIN modeled_population mp ON mp.modeled_population_id = lr.modeled_population_id
                JOIN failure_mode fm ON fm.failure_mode_id = mp.failure_mode_id
                JOIN failure_mechanism fmech ON fmech.failure_mechanism_id = mp.failure_mechanism_id
                LEFT JOIN asset_schedule_class sc ON sc.schedule_class_id = lr.schedule_class_id
                WHERE lr.result_rank = 1
                  AND mp.asset_number = :asset_number
                  AND mp.grouping_level_used = 'FAILURE_MECHANISM'
                  AND lr.failure_count >= :min_failure_lives
                """,
                {"asset_number": asset_number, "min_failure_lives": MIN_WEIBULL_FAILURE_LIVES},
            ).fetchall()
        fits = []
        for row in rows:
            failure_count = int(row["failure_count"] or 0)
            r_squared = row["probability_plot_r_squared"]
            fits.append({
                "modeled_population_id": int(row["modeled_population_id"]),
                # What the page asks saved-analysis for, to open this fit.
                "failure_mode_id": int(row["failure_mode_id"]),
                "failure_mechanism_id": int(row["failure_mechanism_id"]),
                "failure_mode_name": row["failure_mode_name"],
                "failure_mechanism_name": row["failure_mechanism_name"],
                "beta_mle": float(row["beta_mle"]),
                "eta_mle": float(row["eta_mle"]),
                "failure_count": failure_count,
                "censored_count": int(row["censored_count"] or 0),
                # NULL for a fit saved before R² was stored; running it again fills it in.
                "probability_plot_r_squared": r_squared,
                "probability_plot_review": bool(r_squared is not None and r_squared < self.r_squared_review_threshold(failure_count)),
                "run_datetime": row["run_datetime"],
                "analysis_cutoff": row["analysis_cutoff_datetime"],
                "time_zone": row["schedule_time_zone"] or "UTC",
                "current_life_hours": float(row["current_life_hours"] or 0.0),
                "schedule_name": row["schedule_class_name"],
                "hours_per_day": row["hours_per_day"],
                "exclude_weekends": None if row["exclude_weekends"] is None else bool(row["exclude_weekends"]),
                "method_version": row["code_version"],
                "method_current": row["code_version"] == WEIBULL_METHOD_VERSION,
                "schedule_current": row["schedule_class_id"] is None or int(row["schedule_class_id"]) == current_schedule_id,
            })
        return fits

    @staticmethod
    def _weekly_schedule_hours(hours_per_day: float | None, exclude_weekends: bool | None) -> float:
        """The life hours a calendar week adds on a schedule, counted as life hours are."""

        if hours_per_day is None and exclude_weekends is None:
            # No schedule recorded at all: the life hours were counted on the plant default.
            return DEFAULT_WEEKDAY_SCHEDULE_HOURS_PER_DAY * 5
        if hours_per_day is None:
            # A schedule with no hours set (raw elapsed) counts every clock hour.
            return 24.0 * 7
        return float(hours_per_day) * (5 if exclude_weekends else 7)

    def latest_failure_mechanism_beta_rankings(self, asset_number: str, *, limit: int = 5) -> list[dict[str, Any]]:
        """The asset's failure mechanisms with the highest beta in their latest saved fits.

        Where an age-based PM is most likely to pay off: the strongest wear-out
        patterns first (REL-WBL-MTH-001 §8.1).
        """

        fits = self._latest_mechanism_fits(asset_number)
        fits.sort(key=lambda fit: (-fit["beta_mle"], -fit["failure_count"], str(fit["failure_mechanism_name"])))
        return fits[:limit]

    def latest_failure_mechanism_risk_rankings(
        self, asset_number: str, *, weeks: float = RISK_WINDOW_WEEKS, limit: int = 5
    ) -> list[dict[str, Any]]:
        """The asset's failure mechanisms most likely to fail in the next ``weeks`` weeks.

        For each latest saved fit, the chance its current life ends in a failure within
        the window, given it has lasted this long (REL-WBL-MTH-001 §8.1):

            P = 1 - R(t + Δ) / R(t)

        t is the current life at the run's cutoff, the scheduled hours since the last
        failure or PM reset; Δ is the window in the same hours, the weeks times the
        schedule's weekly hours (100 on 20 hours Monday-Friday). Unlike the chance of
        having failed by now, which climbs toward 100% for any long survivor, this is
        the chance of what happens next, and it falls with age when beta is below 1.
        Measured from each run's cutoff, which each row carries.
        """

        if not (isinstance(weeks, (int, float)) and math.isfinite(weeks) and 1 <= weeks <= 52):
            raise ValueError("The window has to be between 1 and 52 weeks.")
        ranked = []
        for fit in self._latest_mechanism_fits(asset_number):
            window_hours = weeks * self._weekly_schedule_hours(fit["hours_per_day"], fit["exclude_weekends"])
            t = fit["current_life_hours"]
            beta, eta = fit["beta_mle"], fit["eta_mle"]
            try:
                probability = -math.expm1((t / eta) ** beta - ((t + window_hours) / eta) ** beta)
            except (OverflowError, ZeroDivisionError, ValueError):
                probability = 1.0
            ranked.append({**fit, "window_weeks": weeks, "window_hours": window_hours, "probability": probability})
        ranked.sort(key=lambda fit: (-fit["probability"], -fit["failure_count"], str(fit["failure_mechanism_name"])))
        return ranked[:limit]

    def tour_example_asset(self) -> str | None:
        """The asset the Perform an Analysis walk-through picks for somebody who hasn't.

        The one with the most to show: an asset with a saved failure-mechanism
        Weibull fit first, since a saved fit is the only kind the tour can open
        without running and storing one of its own, then whichever has the most
        included failures, which are what fill the Pareto and the trend, PM and
        downtime charts. None when no asset has an included failure to show.
        """

        with self.connect() as conn:
            row = conn.execute(
                """
                WITH failures AS (
                    SELECT m.asset_number, COUNT(*) AS failure_count
                    FROM mapped_cmms_record m
                    JOIN event_disposition d ON d.mapped_record_id = m.mapped_record_id
                    WHERE d.is_current = 1
                      AND d.include_in_weibull_candidate = 1
                      AND d.disposition_category = 'INCLUDED_FAILURE'
                      AND d.failure_mechanism_id IS NOT NULL
                      AND TRIM(COALESCE(m.asset_number, '')) <> ''
                    GROUP BY m.asset_number
                ),
                fitted AS (
                    SELECT DISTINCT mp.asset_number
                    FROM weibull_result wr
                    JOIN weibull_analysis_run war ON war.weibull_analysis_run_id = wr.weibull_analysis_run_id
                    JOIN analysis_dataset ad ON ad.analysis_dataset_id = war.analysis_dataset_id
                    JOIN modeled_population mp ON mp.modeled_population_id = ad.modeled_population_id
                    JOIN failure_mode fm ON fm.failure_mode_id = mp.failure_mode_id
                    JOIN failure_mechanism fmech ON fmech.failure_mechanism_id = mp.failure_mechanism_id
                    WHERE mp.asset_number = ad.asset_number
                      AND mp.grouping_level_used = 'FAILURE_MECHANISM'
                )
                SELECT f.asset_number
                FROM failures f
                LEFT JOIN fitted ON fitted.asset_number = f.asset_number
                ORDER BY fitted.asset_number IS NULL, f.failure_count DESC, f.asset_number
                LIMIT 1
                """
            ).fetchone()
        return str(row["asset_number"]) if row else None

    def disposition_tour_example_asset(self, kind: str, *, only_needing_disposition: bool = False, search: str | None = None) -> str | None:
        """The asset the Disposition walk-through picks for somebody who hasn't.

        Whichever has the most rows in the table the page would draw for it: of
        the Record Type showing, only the new ones when Rows says so, and only
        those matching the Search box. Built from the same WHERE clauses as that
        table, so the example is never an asset whose table would come up empty.
        None when no asset has any.

        Only rows stored under the number exactly as the asset list offers it
        count. The list trims the stored number, and the table matches the one
        picked from the list exactly, so a row stored as " A-1 " is in neither
        the example's lookup nor its table.
        """

        where = self._disposition_where(kind)
        needs_disposition_where = self._needs_disposition_where(kind) if only_needing_disposition else ""
        search_clause, search_params = self._disposition_search_clause(search)
        with self.connect() as conn:
            row = conn.execute(
                f"""
                SELECT m.asset_number, COUNT(*) AS record_count
                FROM mapped_cmms_record m
                LEFT JOIN event_disposition d ON d.mapped_record_id = m.mapped_record_id AND d.is_current = 1
                WHERE m.asset_number = TRIM(m.asset_number) AND m.asset_number <> ''
                  AND {where} {needs_disposition_where}{search_clause}
                GROUP BY m.asset_number
                ORDER BY record_count DESC, m.asset_number
                LIMIT 1
                """,
                search_params,
            ).fetchone()
        return str(row["asset_number"]) if row else None

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
                    COALESCE(SUM(CASE WHEN COALESCE(m.downtime_hours, 0) < 0 THEN 0 ELSE COALESCE(m.downtime_hours, 0) END), 0) AS downtime_hours
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

    @staticmethod
    def _continuous_month_range(month_keys: Iterable[str]) -> list[str]:
        """Return every ``YYYY-MM`` from the earliest to the latest key, inclusive.

        Filling the gap months (rather than only the months that actually have
        failures) is what lets the Failure Mode Trend line include zero-occurrence
        months instead of skipping straight from one populated month to the next.
        """

        keys = sorted(month_keys)
        if not keys:
            return []
        start_year, start_month = (int(part) for part in keys[0].split("-"))
        end_year, end_month = (int(part) for part in keys[-1].split("-"))
        months: list[str] = []
        year, month = start_year, start_month
        while (year, month) <= (end_year, end_month):
            months.append(f"{year:04d}-{month:02d}")
            if month == 12:
                year, month = year + 1, 1
            else:
                month += 1
        return months

    @staticmethod
    def _trend_summary_entry(mechanism: dict[str, Any], value: float | int) -> dict[str, Any]:
        return {
            "failure_mode_id": mechanism["failure_mode_id"],
            "failure_mechanism_id": mechanism["failure_mechanism_id"],
            "failure_mechanism_name": mechanism["failure_mechanism_name"],
            "failure_mode_name": mechanism["failure_mode_name"],
            "value": value,
        }

    def _failure_mode_trend_summary(
        self, mechanisms: list[dict[str, Any]], has_growth_window: bool
    ) -> dict[str, Any]:
        """Pick the headline mechanism for each Failure Mode Trend summary card.

        ``fastest_growing`` / ``most_improved`` stay ``None`` when there are fewer
        than six months of data (no recent-3-vs-previous-3 window); the client
        renders that as "Insufficient Data".
        """

        summary: dict[str, Any] = {
            "most_frequent": None,
            "highest_downtime": None,
            "fastest_growing": None,
            "most_improved": None,
        }
        if not mechanisms:
            return summary
        most_frequent = max(mechanisms, key=lambda m: (m["total_count"], m["total_downtime_hours"]))
        summary["most_frequent"] = self._trend_summary_entry(most_frequent, int(most_frequent["total_count"]))
        highest_downtime = max(mechanisms, key=lambda m: (m["total_downtime_hours"], m["total_count"]))
        summary["highest_downtime"] = self._trend_summary_entry(
            highest_downtime, round(float(highest_downtime["total_downtime_hours"]), 4)
        )
        if has_growth_window:
            # Fastest growing = largest positive recent-vs-previous change; most
            # improved = largest decrease. Each card only considers mechanisms that
            # actually moved in its direction, so a declining mechanism never shows
            # up as "Fastest Growing" (and vice versa); the card stays empty when no
            # mechanism moved that way.
            growing = [m for m in mechanisms if m["growth"] is not None and m["growth"] > 0]
            if growing:
                fastest = max(growing, key=lambda m: (m["growth"], m["total_count"]))
                summary["fastest_growing"] = self._trend_summary_entry(fastest, int(fastest["growth"]))
            declining = [m for m in mechanisms if m["growth"] is not None and m["growth"] < 0]
            if declining:
                improved = min(declining, key=lambda m: (m["growth"], -m["total_count"]))
                summary["most_improved"] = self._trend_summary_entry(improved, int(improved["growth"]))
        return summary

    def failure_mode_trend(self, asset_number: str) -> dict[str, Any]:
        """Monthly failure-mechanism occurrence trend for the Failure Mode Trend panel.

        Uses the same included-failure dataset, failure-mechanism field, work-order
        date, and downtime field as :meth:`failure_mechanism_pareto`, so the trend
        stays consistent with the Pareto chart and any asset filter already applied.
        Records are bucketed by the month of the work-order date (completed, else
        start, else created), and the returned month axis is continuous (zero-filled)
        so the trend line never skips a missing month.

        ``growth`` compares the most recent three months against the previous three
        months and is only computed when at least six months of data exist.
        """

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
                    m.mapped_record_id,
                    m.task_id,
                    m.task_name,
                    m.requestor_description,
                    m.completion_notes,
                    m.area_affected,
                    m.condition_found,
                    m.cause,
                    m.action_taken,
                    -- NULLIF(TRIM(...), '') so a blank (empty/whitespace) completed
                    -- date doesn't stop COALESCE and hide a record that has a usable
                    -- start/created date — otherwise it would count in the totals but
                    -- drop out of every monthly bucket, undercounting the trend.
                    COALESCE(
                        NULLIF(TRIM(m.completed_date_final), ''),
                        NULLIF(TRIM(m.start_date_final), ''),
                        NULLIF(TRIM(m.created_date_final), '')
                    ) AS wo_date,
                    CASE WHEN COALESCE(m.downtime_hours, 0) < 0 THEN 0 ELSE COALESCE(m.downtime_hours, 0) END AS downtime_hours
                FROM mapped_cmms_record m
                JOIN current_disp d ON d.mapped_record_id = m.mapped_record_id
                LEFT JOIN failure_mode fm ON fm.failure_mode_id = d.failure_mode_id
                LEFT JOIN failure_mechanism fmech ON fmech.failure_mechanism_id = d.failure_mechanism_id
                WHERE m.asset_number = :asset_number
                  AND d.disposition_category = 'INCLUDED_FAILURE'
                  AND d.failure_mechanism_id IS NOT NULL
                """,
                {"asset_number": asset_number},
            ).fetchall()

        mechanisms: dict[tuple[int, int], dict[str, Any]] = {}
        month_keys: set[str] = set()
        for row in rows:
            key = (int(row["failure_mode_id"]), int(row["failure_mechanism_id"]))
            mechanism = mechanisms.get(key)
            if mechanism is None:
                mechanism = {
                    "failure_mode_id": int(row["failure_mode_id"]),
                    "failure_mechanism_id": int(row["failure_mechanism_id"]),
                    "failure_mechanism_name": row["failure_mechanism_name"],
                    "failure_mode_name": row["failure_mode_name"],
                    "total_count": 0,
                    "total_downtime_hours": 0.0,
                    "monthly": {},  # YYYY-MM -> occurrence count
                    "records": [],  # per-WO detail backing the trend (dated rows only)
                }
                mechanisms[key] = mechanism
            mechanism["total_count"] += 1
            mechanism["total_downtime_hours"] += float(row["downtime_hours"] or 0.0)
            parsed = self._parse_datetime(row["wo_date"])
            if parsed is not None:
                month_key = f"{parsed.year:04d}-{parsed.month:02d}"
                mechanism["monthly"][month_key] = mechanism["monthly"].get(month_key, 0) + 1
                month_keys.add(month_key)
                # Keep the underlying work order so the client can show which WOs
                # populate each plotted month. Only dated records are kept so the
                # detail table stays consistent with the chart's monthly buckets.
                mechanism["records"].append({
                    "mapped_record_id": int(row["mapped_record_id"]),
                    "task_id": row["task_id"],
                    "task_name": row["task_name"],
                    "requestor_description": row["requestor_description"],
                    "completion_notes": row["completion_notes"],
                    **{key: row[key] for key in NARRATIVE_KEYS},
                    "downtime_hours": round(float(row["downtime_hours"] or 0.0), 4),
                    "month": month_key,
                    "wo_date": parsed.date().isoformat(),
                })

        months = self._continuous_month_range(month_keys)
        has_growth_window = len(months) >= 6
        recent_months = months[-3:] if has_growth_window else []
        previous_months = months[-6:-3] if has_growth_window else []

        mechanism_views: list[dict[str, Any]] = []
        for mechanism in mechanisms.values():
            monthly = mechanism["monthly"]
            monthly_counts = [int(monthly.get(month_key, 0)) for month_key in months]
            recent_count = sum(int(monthly.get(month_key, 0)) for month_key in recent_months)
            previous_count = sum(int(monthly.get(month_key, 0)) for month_key in previous_months)
            mechanism_views.append({
                "failure_mode_id": mechanism["failure_mode_id"],
                "failure_mechanism_id": mechanism["failure_mechanism_id"],
                "failure_mechanism_name": mechanism["failure_mechanism_name"],
                "failure_mode_name": mechanism["failure_mode_name"],
                "total_count": int(mechanism["total_count"]),
                "total_downtime_hours": round(float(mechanism["total_downtime_hours"]), 4),
                "monthly_counts": monthly_counts,
                "recent_count": int(recent_count),
                "previous_count": int(previous_count),
                "growth": (recent_count - previous_count) if has_growth_window else None,
                "records": mechanism["records"],
            })
        # Stable order mirroring the Pareto (downtime desc, then count, then name).
        mechanism_views.sort(
            key=lambda m: (-m["total_downtime_hours"], -m["total_count"], m["failure_mechanism_name"])
        )

        return {
            "months": months,
            "mechanisms": mechanism_views,
            "has_growth_window": has_growth_window,
            "summary": self._failure_mode_trend_summary(mechanism_views, has_growth_window),
        }

    @staticmethod
    def _pm_effectiveness_rating(average_days: float | None) -> str:
        """Map an average days-to-failure value onto the qualitative PM rating.

        Mirrors the RCA dashboard PM Effectiveness card bands; ``None`` (no PM ->
        failure pairs) is reported as "Insufficient Data".
        """

        if average_days is None:
            return "Insufficient Data"
        if average_days >= 180:
            return "Excellent"
        if average_days >= 90:
            return "Good"
        if average_days >= 30:
            return "Fair"
        return "Poor"

    @staticmethod
    def _pair_pms_to_failures(
        pms: list[tuple[datetime, dict[str, Any]]],
        failures: list[tuple[datetime, dict[str, Any]]],
    ) -> list[tuple[datetime, dict[str, Any], datetime, dict[str, Any], float]]:
        """Pair each completed PM with the first failure that follows it.

        ``pms`` and ``failures`` are ``(datetime, payload)`` tuples and need not be
        pre-sorted. Each PM is paired with the earliest failure strictly after its
        completion datetime (only the first occurrence); PMs with no later failure
        are dropped. When several PMs precede the same failure they each pair with
        it, so the per-PM PM-to-failure table and average-days metric cover every
        such PM — de-duplicating to distinct corrective work orders for the
        "Failures After PM" count / trend is handled by the caller. Returns
        ``(pm_dt, pm, fail_dt, failure, days_between)`` tuples.
        """

        ordered_failures = sorted(failures, key=lambda item: item[0])
        pairs: list[tuple[datetime, dict[str, Any], datetime, dict[str, Any], float]] = []
        for pm_dt, pm in sorted(pms, key=lambda item: item[0]):
            next_failure = next(((fd, f) for fd, f in ordered_failures if fd > pm_dt), None)
            if next_failure is None:
                continue
            fail_dt, failure = next_failure
            days = (fail_dt - pm_dt).total_seconds() / 86400.0
            pairs.append((pm_dt, pm, fail_dt, failure, days))
        return pairs

    def pm_effectiveness(
        self,
        asset_number: str,
        failure_mechanism_id: int,
        failure_mode_id: int | None = None,
    ) -> dict[str, Any]:
        """Evaluate whether PMs are reducing failures for one failure mechanism.

        For the selected asset and failure mechanism, pair every completed PM with
        the first corrective (INCLUDED_FAILURE) work order that follows it on the
        same asset, then summarise the days-between values into the PM Effectiveness
        cards, the "Failures Following PM" monthly trend, and the PM-to-failure table
        the RCA dashboard shows in place of the Weibull summary.

        The PM-to-failure table and the Average Days to Failure are per PM (so when
        several PMs precede the same corrective WO each PM still gets a row), while
        "Failures After PM" and the monthly trend count the *distinct* corrective
        WOs so a single failure preceded by multiple PMs is only counted once.

        Uses the same datasets the Pareto / Failure Mode Trend panels use: PMs come
        from the PM record-class filter, failures from the current included-failure
        dispositions for the chosen mechanism, and the work-order date is the
        completed/start/created fallback already used across the dashboard.
        """

        # Match the dashboard's PM disposition dataset while respecting explicit
        # reclassification. Like _disposition_where("pm") this counts a record whose
        # effective class is PM and undispositioned PM candidates, but the
        # is_pm_candidate fallback is only honored while record_class_final IS NULL:
        # once a user saves a final class (save_disposition writes record_class_final
        # on mapped_cmms_record), that final class wins, so a record reclassified
        # away from PM (e.g. to INSPECTION) is excluded even though is_pm_candidate
        # stays set, and a still-candidate PM auto-classed as something else (e.g.
        # parts/project) is still counted until it is dispositioned.
        pm_clause = (
            "(COALESCE(m.record_class_final, m.record_class_auto) IN ('PM','PM_RESET_CANDIDATE') "
            "OR (m.record_class_final IS NULL AND m.is_pm_candidate = 1))"
        )
        # `is_completed` alone is unreliable for the completion gate: _map_raw_record
        # sets it from a "complete" substring test, which also matches "Incomplete"
        # and "Not Complete". Treat a PM as completed only when it has a real
        # completion timestamp, or is_completed is set and the status does not read
        # as not-complete (compared with spaces/dashes/underscores stripped so
        # "Not Complete"/"not-complete" are caught alongside "Incomplete").
        normalized_status = "REPLACE(REPLACE(REPLACE(LOWER(COALESCE(m.status_raw, '')), ' ', ''), '-', ''), '_', '')"
        completed_pm_clause = (
            "(NULLIF(TRIM(m.completed_date_final), '') IS NOT NULL "
            f"OR (m.is_completed = 1 AND {normalized_status} NOT LIKE '%incomplete%' "
            f"AND {normalized_status} NOT LIKE '%notcomplete%'))"
        )
        with self.connect() as conn:
            mechanism_row = conn.execute(
                "SELECT failure_mechanism_name FROM failure_mechanism WHERE failure_mechanism_id = :id",
                {"id": failure_mechanism_id},
            ).fetchone()
            pm_rows = conn.execute(
                f"""
                SELECT
                    m.task_id,
                    m.asset_number,
                    -- Same completed -> start -> created fallback the rest of the
                    -- dashboard uses, so a completed PM with a blank completion
                    -- timestamp but a usable start/created date still counts.
                    COALESCE(
                        NULLIF(TRIM(m.completed_date_final), ''),
                        NULLIF(TRIM(m.start_date_final), ''),
                        NULLIF(TRIM(m.created_date_final), '')
                    ) AS completed_date
                FROM mapped_cmms_record m
                WHERE m.asset_number = :asset_number
                  AND {pm_clause}
                  -- Only actually-completed PMs count; the date fallback below
                  -- supplies a timestamp for a completed PM with a blank
                  -- completion date, but must not pull in open/pending PMs that
                  -- merely have a start/created date.
                  AND {completed_pm_clause}
                  AND COALESCE(
                        NULLIF(TRIM(m.completed_date_final), ''),
                        NULLIF(TRIM(m.start_date_final), ''),
                        NULLIF(TRIM(m.created_date_final), '')
                      ) IS NOT NULL
                """,
                {"asset_number": asset_number},
            ).fetchall()
            failure_rows = conn.execute(
                """
                WITH current_disp AS (
                    SELECT * FROM event_disposition WHERE is_current = 1 AND include_in_weibull_candidate = 1
                )
                SELECT
                    m.mapped_record_id,
                    m.task_id,
                    COALESCE(fmech.failure_mechanism_name, 'Unspecified mechanism') AS failure_mechanism_name,
                    COALESCE(
                        NULLIF(TRIM(m.completed_date_final), ''),
                        NULLIF(TRIM(m.start_date_final), ''),
                        NULLIF(TRIM(m.created_date_final), '')
                    ) AS wo_date,
                    CASE WHEN COALESCE(m.downtime_hours, 0) < 0 THEN 0 ELSE COALESCE(m.downtime_hours, 0) END AS downtime_hours
                FROM mapped_cmms_record m
                JOIN current_disp d ON d.mapped_record_id = m.mapped_record_id
                LEFT JOIN failure_mechanism fmech ON fmech.failure_mechanism_id = d.failure_mechanism_id
                WHERE m.asset_number = :asset_number
                  AND d.disposition_category = 'INCLUDED_FAILURE'
                  AND d.failure_mechanism_id = :failure_mechanism_id
                  -- The same mechanism id can appear under more than one failure
                  -- mode (the Pareto emits a row per mode/mechanism pair and the
                  -- client sends both ids); when a mode is given, match it so the
                  -- analysis matches exactly the selected Pareto row.
                  AND (:failure_mode_id IS NULL OR d.failure_mode_id = :failure_mode_id)
                """,
                {
                    "asset_number": asset_number,
                    "failure_mechanism_id": failure_mechanism_id,
                    "failure_mode_id": failure_mode_id,
                },
            ).fetchall()

        mechanism_name = (
            mechanism_row["failure_mechanism_name"] if mechanism_row else "Unspecified mechanism"
        )

        pms: list[tuple[datetime, dict[str, Any]]] = []
        for row in pm_rows:
            parsed = self._parse_datetime(row["completed_date"])
            if parsed is None:
                continue
            pms.append((parsed, {"task_id": row["task_id"], "asset_number": row["asset_number"]}))

        failures: list[tuple[datetime, dict[str, Any]]] = []
        for row in failure_rows:
            parsed = self._parse_datetime(row["wo_date"])
            if parsed is None:
                continue
            failures.append(
                (
                    parsed,
                    {
                        "mapped_record_id": int(row["mapped_record_id"]),
                        "task_id": row["task_id"],
                        "failure_mechanism_name": row["failure_mechanism_name"],
                        "downtime_hours": float(row["downtime_hours"] or 0.0),
                    },
                )
            )

        pairs = self._pair_pms_to_failures(pms, failures)

        # Table + average days are per PM (each completed PM paired with its first
        # following corrective WO). "Failures After PM" and the monthly trend count
        # the distinct corrective WOs instead, so one failure preceded by several
        # PMs contributes multiple table rows but is only counted once as a failure.
        table_rows: list[dict[str, Any]] = []
        days_values: list[float] = []
        distinct_failure_dates: dict[int, datetime] = {}
        for pm_dt, pm, fail_dt, failure, days in pairs:
            days_values.append(days)
            distinct_failure_dates.setdefault(int(failure["mapped_record_id"]), fail_dt)
            table_rows.append(
                {
                    "pm_completion_date": pm_dt.date().isoformat(),
                    "asset_number": pm.get("asset_number") or asset_number,
                    "next_failure_date": fail_dt.date().isoformat(),
                    "days_to_failure": round(days, 1),
                    "failure_mechanism_name": failure.get("failure_mechanism_name") or mechanism_name,
                    "downtime_hours": round(float(failure.get("downtime_hours") or 0.0), 4),
                    "corrective_wo_number": failure.get("task_id"),
                    # The work order behind that number, so the table can open its
                    # disposition when it turns out to be misclassified.
                    "corrective_mapped_record_id": failure.get("mapped_record_id"),
                }
            )

        # Newest PM first so the most recent activity heads the table.
        table_rows.sort(key=lambda r: r["pm_completion_date"], reverse=True)

        month_counts: dict[str, int] = {}
        for fail_dt in distinct_failure_dates.values():
            month_key = f"{fail_dt.year:04d}-{fail_dt.month:02d}"
            month_counts[month_key] = month_counts.get(month_key, 0) + 1
        months = self._continuous_month_range(month_counts.keys())
        monthly_counts = [int(month_counts.get(key, 0)) for key in months]
        average_days = (sum(days_values) / len(days_values)) if days_values else None

        return {
            "asset_number": asset_number,
            "failure_mode_id": failure_mode_id,
            "failure_mechanism_id": failure_mechanism_id,
            "failure_mechanism_name": mechanism_name,
            "pms_performed": len(pms),
            "failures_after_pm": len(distinct_failure_dates),
            "average_days_to_failure": round(average_days, 1) if average_days is not None else None,
            "effectiveness": self._pm_effectiveness_rating(average_days),
            "months": months,
            "monthly_counts": monthly_counts,
            "rows": table_rows,
            "has_pm_history": bool(pms),
        }

    def repeat_fix_rate(self, asset_number: str, window_hours: float = REPEAT_FIX_DEFAULT_WINDOW_HOURS) -> dict[str, Any]:
        """How often a failure comes straight back after it was fixed, per mechanism.

        Reads the failures Weibull reads -- current INCLUDED_FAILURE dispositions with
        Include in Weibull Candidate, each with a mechanism, dated by its completed date
        alone -- and, mechanism by mechanism, measures the gap from each failure to the
        one after it in scheduled hours, on the asset's Weibull schedule and the plant's
        time zone, just as a Weibull life is measured. A failure is a repeat when that
        gap is ``window_hours`` or less: the fix before it did not hold.

        The repeat rate is repeats over intervals, an interval being each failure after
        a mechanism's first. A PM reset does not break the chain, since the question is
        whether the last repair held. This is report-only: nothing here changes which
        lives the Weibull fit uses.
        """

        window = float(window_hours)
        with self.connect() as conn:
            schedule_id = self._schedule_class_id(conn, asset_number)
            schedule = conn.execute(
                "SELECT schedule_class_name, hours_per_day, exclude_weekends FROM asset_schedule_class WHERE schedule_class_id = ?",
                (schedule_id,),
            ).fetchone()
            zone, zone_name, zone_warning = self._plant_time_zone(conn)
            rows = conn.execute(
                """
                SELECT
                    d.failure_mode_id,
                    d.failure_mechanism_id,
                    COALESCE(fmech.failure_mechanism_name, 'Unspecified mechanism') AS failure_mechanism_name,
                    COALESCE(fm.failure_mode_name, 'Unspecified mode') AS failure_mode_name,
                    m.mapped_record_id,
                    m.task_id,
                    m.task_name,
                    m.completed_date_final
                FROM mapped_cmms_record m
                JOIN event_disposition d ON d.mapped_record_id = m.mapped_record_id AND d.is_current = 1
                LEFT JOIN failure_mode fm ON fm.failure_mode_id = d.failure_mode_id
                LEFT JOIN failure_mechanism fmech ON fmech.failure_mechanism_id = d.failure_mechanism_id
                WHERE m.asset_number = :asset_number
                  AND d.disposition_category = 'INCLUDED_FAILURE'
                  AND d.include_in_weibull_candidate = 1
                  AND d.failure_mechanism_id IS NOT NULL
                ORDER BY gremlin_sort_datetime(m.completed_date_final), m.mapped_record_id
                """,
                {"asset_number": asset_number},
            ).fetchall()

        hours_per_day = float(schedule["hours_per_day"])
        exclude_weekends = bool(schedule["exclude_weekends"])
        mechanisms: dict[tuple[int, int], dict[str, Any]] = {}
        undated = 0
        for row in rows:
            completed = self._parse_event_datetime(row["completed_date_final"], zone)
            if completed is None:
                undated += 1
                continue
            key = (int(row["failure_mode_id"]), int(row["failure_mechanism_id"]))
            mechanism = mechanisms.setdefault(
                key,
                {
                    "failure_mode_id": key[0],
                    "failure_mechanism_id": key[1],
                    "failure_mode_name": row["failure_mode_name"],
                    "failure_mechanism_name": row["failure_mechanism_name"],
                    "events": [],
                },
            )
            mechanism["events"].append((completed, row))

        results = []
        pairs = []
        for mechanism in mechanisms.values():
            events = sorted(mechanism.pop("events"), key=lambda e: (e[0], int(e[1]["mapped_record_id"])))
            repeats = 0
            for (prior_dt, prior), (repeat_dt, repeat) in zip(events, events[1:]):
                scheduled, _, _ = self._scheduled_life_hours(
                    prior_dt, repeat_dt, hours_per_day, exclude_weekends=exclude_weekends, tz=zone
                )
                if scheduled > window:
                    continue
                repeats += 1
                raw = (repeat_dt - prior_dt).total_seconds() / 3600.0
                pairs.append(
                    {
                        "failure_mode_id": mechanism["failure_mode_id"],
                        "failure_mechanism_id": mechanism["failure_mechanism_id"],
                        "failure_mechanism_name": mechanism["failure_mechanism_name"],
                        "failure_mode_name": mechanism["failure_mode_name"],
                        "prior_task_id": prior["task_id"],
                        "prior_mapped_record_id": int(prior["mapped_record_id"]),
                        "prior_task_name": prior["task_name"],
                        "prior_completed": prior_dt.isoformat(),
                        "repeat_task_id": repeat["task_id"],
                        "repeat_mapped_record_id": int(repeat["mapped_record_id"]),
                        "repeat_task_name": repeat["task_name"],
                        "repeat_completed": repeat_dt.isoformat(),
                        "scheduled_hours": round(scheduled, 2),
                        "raw_hours": round(raw, 2),
                        "duplicate_check": self._duplicate_check_flag(raw),
                    }
                )
            intervals = len(events) - 1
            results.append(
                {
                    **mechanism,
                    "failures": len(events),
                    "intervals": intervals,
                    "repeats": repeats,
                    "repeat_rate": round(repeats / intervals, 4) if intervals > 0 else None,
                }
            )
        results.sort(key=lambda m: (-m["repeats"], -(m["repeat_rate"] or 0.0), -m["failures"], m["failure_mechanism_name"]))
        pairs.sort(key=lambda p: (p["repeat_completed"], p["repeat_mapped_record_id"]))

        total_intervals = sum(m["intervals"] for m in results)
        total_repeats = sum(m["repeats"] for m in results)
        rated = [m for m in results if m["intervals"] >= REPEAT_FIX_MIN_INTERVALS and m["repeats"] > 0]
        highest = max(rated, key=lambda m: (m["repeat_rate"], m["repeats"]), default=None)
        most = results[0] if results and results[0]["repeats"] > 0 else None

        def entry(mechanism: dict[str, Any] | None) -> dict[str, Any] | None:
            if mechanism is None:
                return None
            return {key: mechanism[key] for key in (
                "failure_mode_id", "failure_mechanism_id", "failure_mechanism_name", "failure_mode_name",
                "failures", "intervals", "repeats", "repeat_rate",
            )}

        return {
            "asset_number": asset_number,
            "window_hours": window,
            "min_intervals_for_rate": REPEAT_FIX_MIN_INTERVALS,
            "schedule_name": schedule["schedule_class_name"],
            "hours_per_day": hours_per_day,
            "exclude_weekends": exclude_weekends,
            "time_zone": zone_name,
            "time_zone_warning": zone_warning,
            "failures": sum(m["failures"] for m in results),
            "undated_failures": undated,
            "intervals": total_intervals,
            "repeats": total_repeats,
            "repeat_rate": round(total_repeats / total_intervals, 4) if total_intervals else None,
            "possible_duplicates": sum(1 for p in pairs if p["duplicate_check"]),
            "highest_rate": entry(highest),
            "most_repeats": entry(most),
            "mechanisms": results,
            "pairs": pairs,
        }

    @staticmethod
    def _median(values: list[float]) -> float:
        """Median of ``values`` (0.0 for an empty list)."""

        ordered = sorted(values)
        count = len(ordered)
        if count == 0:
            return 0.0
        mid = count // 2
        if count % 2:
            return float(ordered[mid])
        return (float(ordered[mid - 1]) + float(ordered[mid])) / 2.0

    @staticmethod
    def _first_nonempty(*values: Any) -> str | None:
        """First non-blank value (trimmed) among ``values``, else ``None``."""

        for value in values:
            if value is None:
                continue
            text = str(value).strip()
            if text:
                return text
        return None

    def downtime_driver_analysis(
        self,
        asset_number: str,
        failure_mechanism_id: int | None = None,
        failure_mode_id: int | None = None,
    ) -> dict[str, Any]:
        """Explain why a failure mechanism drives downtime, for the Downtime Driver panel.

        Uses the same included-failure dataset, failure-mechanism field, work-order
        date, and downtime field as :meth:`failure_mechanism_pareto` /
        :meth:`failure_mode_trend`, so the analysis stays consistent with the Pareto
        chart and any asset/disposition filter already applied. ``failure_mechanism_id``
        may be ``None`` to aggregate every mechanism under ``failure_mode_id`` (the
        mode-level "all mechanisms" selection); otherwise the analysis is restricted to
        the single mechanism (matched within the given mode when one is supplied).

        Returns the downtime summary statistics, a continuous zero-filled monthly
        downtime series (so the trend never skips a missing month), a binned downtime
        distribution, downtime grouped by asset/location, and the highest-downtime
        work orders. Every included corrective work order for the selection counts
        toward the summary/distribution/grouping; only dated records contribute to the
        monthly trend.
        """

        # Downtime range buckets for the Downtime Distribution histogram, as
        # (label, lower_inclusive, upper_exclusive) with an open-ended final bucket
        # (upper None). Mirrors the RCA dashboard's suggested 0-1/1-4/4-8/8-24/24+
        # hour bins so the chart shows whether downtime is driven by many short
        # events or a few long ones.
        distribution_bins: tuple[tuple[str, float, float | None], ...] = (
            ("0–1 hr", 0.0, 1.0),
            ("1–4 hr", 1.0, 4.0),
            ("4–8 hr", 4.0, 8.0),
            ("8–24 hr", 8.0, 24.0),
            ("24+ hr", 24.0, None),
        )

        with self.connect() as conn:
            rows = conn.execute(
                """
                WITH current_disp AS (
                    SELECT * FROM event_disposition WHERE is_current = 1 AND include_in_weibull_candidate = 1
                )
                SELECT
                    m.mapped_record_id,
                    m.task_id,
                    m.task_name,
                    m.asset_name,
                    m.asset_number,
                    m.immediate_parent_asset_name,
                    m.root_asset_name,
                    m.requestor_description,
                    m.request_title,
                    m.completion_notes,
                    m.area_affected,
                    m.condition_found,
                    m.cause,
                    m.action_taken,
                    COALESCE(fmech.failure_mechanism_name, 'Unspecified mechanism') AS failure_mechanism_name,
                    COALESCE(fm.failure_mode_name, 'Unspecified mode') AS failure_mode_name,
                    COALESCE(
                        NULLIF(TRIM(m.completed_date_final), ''),
                        NULLIF(TRIM(m.start_date_final), ''),
                        NULLIF(TRIM(m.created_date_final), '')
                    ) AS wo_date,
                    CASE WHEN COALESCE(m.downtime_hours, 0) < 0 THEN 0 ELSE COALESCE(m.downtime_hours, 0) END AS downtime_hours
                FROM mapped_cmms_record m
                JOIN current_disp d ON d.mapped_record_id = m.mapped_record_id
                LEFT JOIN failure_mode fm ON fm.failure_mode_id = d.failure_mode_id
                LEFT JOIN failure_mechanism fmech ON fmech.failure_mechanism_id = d.failure_mechanism_id
                WHERE m.asset_number = :asset_number
                  AND d.disposition_category = 'INCLUDED_FAILURE'
                  AND d.failure_mechanism_id IS NOT NULL
                  -- A mechanism-level selection restricts to one mechanism; a
                  -- mode-level selection (mechanism NULL) keeps every mechanism under
                  -- the mode. The same mechanism id can appear under more than one
                  -- mode, so match the mode too when one is supplied.
                  AND (:failure_mechanism_id IS NULL OR d.failure_mechanism_id = :failure_mechanism_id)
                  AND (:failure_mode_id IS NULL OR d.failure_mode_id = :failure_mode_id)
                """,
                {
                    "asset_number": asset_number,
                    "failure_mechanism_id": failure_mechanism_id,
                    "failure_mode_id": failure_mode_id,
                },
            ).fetchall()

        work_orders: list[dict[str, Any]] = []
        downtime_values: list[float] = []
        total_downtime = 0.0
        monthly: dict[str, float] = {}
        month_keys: set[str] = set()
        asset_downtime: dict[str, float] = {}
        bin_counts = [0 for _ in distribution_bins]

        for row in rows:
            downtime = float(row["downtime_hours"] or 0.0)
            if downtime < 0:
                downtime = 0.0
            total_downtime += downtime
            downtime_values.append(downtime)

            # "Use asset if available. If asset is unavailable, use location." The
            # mapped CMMS schema has no work-order location column, so the asset
            # hierarchy's parent/root asset name stands in for location.
            asset_label = self._first_nonempty(row["asset_name"], row["asset_number"])
            location_label = self._first_nonempty(
                row["root_asset_name"], row["immediate_parent_asset_name"]
            )
            group_label = asset_label or location_label or "Unspecified asset"
            asset_downtime[group_label] = asset_downtime.get(group_label, 0.0) + downtime

            for index, (_, lower, upper) in enumerate(distribution_bins):
                if downtime >= lower and (upper is None or downtime < upper):
                    bin_counts[index] += 1
                    break

            parsed = self._parse_datetime(row["wo_date"])
            month_key = None
            if parsed is not None:
                month_key = f"{parsed.year:04d}-{parsed.month:02d}"
                monthly[month_key] = monthly.get(month_key, 0.0) + downtime
                month_keys.add(month_key)

            work_orders.append({
                "mapped_record_id": int(row["mapped_record_id"]),
                "task_id": row["task_id"],
                "task_name": row["task_name"],
                "asset": asset_label,
                "location": location_label,
                "failure_mechanism_name": row["failure_mechanism_name"],
                "failure_mode_name": row["failure_mode_name"],
                # The mapped CMMS schema does not capture a work-order operator /
                # assignee, so this is None and the table shows a placeholder.
                "operator": None,
                "downtime_hours": round(downtime, 4),
                "requestor_description": row["requestor_description"],
                "request_title": row["request_title"],
                "completion_notes": row["completion_notes"],
                **{key: row[key] for key in NARRATIVE_KEYS},
                "wo_date": parsed.date().isoformat() if parsed is not None else None,
                "month": month_key,
            })

        months = self._continuous_month_range(month_keys)
        monthly_downtime = [round(float(monthly.get(key, 0.0)), 4) for key in months]
        distribution = [
            {"label": label, "count": int(count)}
            for (label, _, _), count in zip(distribution_bins, bin_counts)
        ]
        by_asset = [
            {"label": label, "downtime_hours": round(value, 4)}
            for label, value in sorted(asset_downtime.items(), key=lambda kv: (-kv[1], kv[0]))
        ]
        # Highest single-event downtime first; ties fall back to the most recent
        # dated work order (None dates sort last).
        top_events = sorted(
            work_orders,
            key=lambda w: (w["downtime_hours"], w["wo_date"] or ""),
            reverse=True,
        )[:10]

        work_order_count = len(work_orders)
        return {
            "asset_number": asset_number,
            "failure_mode_id": failure_mode_id,
            "failure_mechanism_id": failure_mechanism_id,
            "summary": {
                "total_downtime_hours": round(total_downtime, 4),
                "work_order_count": work_order_count,
                "average_downtime_hours": round(total_downtime / work_order_count, 4) if work_order_count else 0.0,
                "median_downtime_hours": round(self._median(downtime_values), 4),
                "max_downtime_hours": round(max(downtime_values), 4) if downtime_values else 0.0,
            },
            "months": months,
            "monthly_downtime_hours": monthly_downtime,
            "distribution": distribution,
            "by_asset": by_asset,
            "top_events": top_events,
            "has_records": work_order_count > 0,
        }

    # ------------------------------------------------------------------ #
    # Metrics dashboard read-model
    # ------------------------------------------------------------------ #
    def asset_reliability_metrics(
        self,
        asset_numbers: list[str] | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> dict[str, Any]:
        """Per-asset reliability KPIs powering the high-level Metrics dashboard.

        The dataset is the same corrective work order set used by the Life Data
        Analysis disposition screen (``_disposition_where('wo')`` — every record
        classified or flagged as a corrective work order), and the work-order date
        is coalesced exactly as the Pareto / Downtime Driver analyses do
        (``completed_date_final`` → ``start_date_final`` → ``created_date_final``).
        That keeps the dashboard consistent with the rest of the app.

        The active window is split in half so a *current* period can be compared
        against a *baseline* period for trend direction and the reliability risk
        score. When no record falls in the baseline half for any asset, the risk
        score falls back to ranking the current period across the selected assets.

        Args:
            asset_numbers: Restrict to these asset numbers; ``None``/empty = all.
            start_date / end_date: Inclusive ``YYYY-MM-DD`` bounds for the window;
                ``None`` lets the bound fall back to the data's own extent.

        Returns a JSON-friendly dict: the applied window, the full data extent (for
        defaulting the date pickers), whether scheduled/operating hours exist (they
        do not yet; MTBF therefore uses calendar hours between dated corrective
        failures and Availability stays a placeholder), the risk-score mode, and
        one row per asset.
        """

        # On a fresh database the raw layer can hold data while mapped_cmms_record
        # is still empty. The asset list path maps on demand; do the same here so a
        # metrics request that wins the startup race still sees the mapped rows
        # instead of returning an empty dashboard.
        self.ensure_mapped_records_available()

        requested = {str(a).strip() for a in (asset_numbers or []) if str(a).strip()}
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT
                    TRIM(m.asset_number) AS asset_number,
                    COALESCE(NULLIF(TRIM(m.asset_name), ''), '') AS asset_name,
                    CASE WHEN COALESCE(m.downtime_hours, 0) < 0 THEN 0 ELSE COALESCE(m.downtime_hours, 0) END AS downtime_hours,
                    COALESCE(
                        NULLIF(TRIM(m.completed_date_final), ''),
                        NULLIF(TRIM(m.start_date_final), ''),
                        NULLIF(TRIM(m.created_date_final), '')
                    ) AS wo_date,
                    COALESCE(
                        NULLIF(TRIM(m.request_title), ''),
                        NULLIF(TRIM(m.task_name), ''),
                        NULLIF(TRIM(m.requestor_description), '')
                    ) AS wo_label
                FROM mapped_cmms_record m
                WHERE m.asset_number IS NOT NULL AND TRIM(m.asset_number) <> ''
                  AND (COALESCE(m.record_class_final, m.record_class_auto) = 'CORRECTIVE_WO'
                       OR (m.record_class_final IS NULL AND m.is_corrective_wo_candidate = 1))
                """
            ).fetchall()

        # Resolve the active window. Parse the requested bounds and the data's own
        # extent; an open bound defers to the data so "all dates" still splits into
        # two comparable halves.
        start_dt = self._parse_metric_date(start_date, end_of_day=False)
        end_dt = self._parse_metric_date(end_date, end_of_day=True)

        parsed_rows: list[dict[str, Any]] = []
        data_min: datetime | None = None
        data_max: datetime | None = None
        for row in rows:
            asset_number = row["asset_number"]
            if requested and asset_number not in requested:
                continue
            when = self._parse_datetime(row["wo_date"])
            downtime = float(row["downtime_hours"] or 0.0)
            if downtime < 0:
                downtime = 0.0
            if when is not None:
                data_min = when if data_min is None or when < data_min else data_min
                data_max = when if data_max is None or when > data_max else data_max
            parsed_rows.append(
                {
                    "asset_number": asset_number,
                    "asset_name": row["asset_name"],
                    "downtime": downtime,
                    "when": when,
                    "label": (row["wo_label"] or "").strip().lower(),
                }
            )

        window_start = start_dt or data_min
        window_end = end_dt or data_max
        # Keep only records inside the active window. Undated records can be
        # included only when no explicit date range is active; otherwise they
        # cannot be proven to belong to the requested period.
        date_range_active = start_dt is not None or end_dt is not None
        in_window: list[dict[str, Any]] = []
        for record in parsed_rows:
            when = record["when"]
            if when is None:
                if date_range_active:
                    continue
            else:
                if window_start is not None and when < window_start:
                    continue
                if window_end is not None and when > window_end:
                    continue
            in_window.append(record)

        # Midpoint of the active window splits baseline (earlier) from current
        # (recent). Without two real bounds there is nothing to split on.
        midpoint: datetime | None = None
        if window_start is not None and window_end is not None and window_end > window_start:
            midpoint = window_start + (window_end - window_start) / 2

        by_asset: dict[str, dict[str, Any]] = {}
        for record in in_window:
            asset_number = record["asset_number"]
            bucket = by_asset.get(asset_number)
            if bucket is None:
                bucket = by_asset.setdefault(
                    asset_number,
                    {
                        "asset_number": asset_number,
                        "asset_name": record["asset_name"],
                        "all": [],
                        "current": [],
                        "baseline": [],
                    },
                )
            if not bucket["asset_name"] and record["asset_name"]:
                bucket["asset_name"] = record["asset_name"]
            bucket["all"].append(record)
            when = record["when"]
            if midpoint is not None and when is not None:
                (bucket["current"] if when >= midpoint else bucket["baseline"]).append(record)

        baseline_available = any(bucket["baseline"] for bucket in by_asset.values())

        assets: list[dict[str, Any]] = []
        for bucket in by_asset.values():
            assets.append(self._asset_metric_row(bucket, baseline_available))

        # Relative risk fallback: with no baseline anywhere, rank the current
        # period across the selected assets instead of period-over-period change.
        if not baseline_available and assets:
            self._apply_relative_risk(assets)

        for asset in assets:
            asset["status"] = self._risk_status(asset["risk_score"])

        assets.sort(key=lambda a: self._natural_key(a["asset_number"]))

        return {
            "scheduled_hours_available": False,
            "baseline_mode": "period" if baseline_available else "relative",
            "applied_window": {
                "start": window_start.date().isoformat() if window_start else None,
                "end": window_end.date().isoformat() if window_end else None,
                "midpoint": midpoint.date().isoformat() if midpoint else None,
            },
            "data_window": {
                "start": data_min.date().isoformat() if data_min else None,
                "end": data_max.date().isoformat() if data_max else None,
            },
            "asset_count": len(assets),
            "assets": assets,
        }

    def _asset_metric_row(self, bucket: dict[str, Any], baseline_available: bool) -> dict[str, Any]:
        """Build one asset's KPI/risk row from its bucketed corrective work orders."""

        all_records = bucket["all"]
        current = bucket["current"]
        baseline = bucket["baseline"]

        wo_count = len(all_records)
        total_downtime = sum(r["downtime"] for r in all_records)
        # MTTR = total downtime / number of corrective work orders.
        mttr = total_downtime / wo_count if wo_count else None

        # MTBF: use calendar hours between dated corrective work orders.
        # If true operating-hour meter data is added later, replace this with
        # operating exposure between failures instead of elapsed wall-clock time.
        # Fewer than two dated failures cannot yield an interval -> Data Required.
        failure_times = sorted(r["when"] for r in all_records if r["when"] is not None)
        if len(failure_times) >= 2:
            span_hours = (failure_times[-1] - failure_times[0]).total_seconds() / 3600.0
            gaps = len(failure_times) - 1
            mtbf = span_hours / gaps if gaps and span_hours > 0 else None
            mtbf_status = "ok" if mtbf is not None else "data_required"
        else:
            mtbf = None
            mtbf_status = "data_required"

        cur_count = len(current)
        base_count = len(baseline)
        cur_downtime = sum(r["downtime"] for r in current)
        base_downtime = sum(r["downtime"] for r in baseline)
        cur_mttr = cur_downtime / cur_count if cur_count else 0.0
        base_mttr = base_downtime / base_count if base_count else 0.0

        # Repeat-failure frequency in the current period: the share of failures
        # that are not the first occurrence of their work-order label.
        labels = [r["label"] for r in current if r["label"]]
        repeat_failures = len(labels) - len(set(labels)) if labels else 0
        repeat_score = (repeat_failures / len(labels) * 100.0) if labels else 0.0

        has_baseline = base_count > 0
        downtime_change = self._pct_change(cur_downtime, base_downtime)
        wo_change = self._pct_change(cur_count, base_count)
        mttr_change = self._pct_change(cur_mttr, base_mttr)

        if baseline_available:
            dt_score = self._increase_score(cur_downtime, base_downtime)
            wo_score = self._increase_score(cur_count, base_count)
            mttr_score = self._increase_score(cur_mttr, base_mttr)
            risk = 0.40 * dt_score + 0.25 * wo_score + 0.25 * mttr_score + 0.10 * repeat_score
            risk_score: float | None = round(max(0.0, min(100.0, risk)), 1)
        else:
            # Filled in later by _apply_relative_risk once every asset is known.
            risk_score = None

        # Trend direction follows the current-vs-baseline downtime movement.
        if downtime_change is None:
            trend = "flat"
        elif downtime_change > 5:
            trend = "up"
        elif downtime_change < -5:
            trend = "down"
        else:
            trend = "flat"

        alert_count = 0
        if risk_score is not None and risk_score >= 70:
            alert_count += 1
        for change in (downtime_change, wo_change, mttr_change):
            if change is not None and change >= 50:
                alert_count += 1

        return {
            "asset_number": bucket["asset_number"],
            "asset_name": bucket["asset_name"] or "",
            "work_order_count": wo_count,
            "total_downtime_hours": round(total_downtime, 2),
            "mttr_hours": round(mttr, 2) if mttr is not None else None,
            "mtbf_hours": round(mtbf, 2) if mtbf is not None else None,
            "mtbf_status": mtbf_status,
            "current_downtime_hours": round(cur_downtime, 2),
            "baseline_downtime_hours": round(base_downtime, 2),
            "current_wo_count": cur_count,
            "baseline_wo_count": base_count,
            "has_baseline": has_baseline,
            "downtime_change_pct": downtime_change,
            "wo_change_pct": wo_change,
            "mttr_change_pct": mttr_change,
            "repeat_failure_count": repeat_failures,
            "risk_score": risk_score,
            "trend": trend,
            "alert_count": alert_count,
        }

    def _apply_relative_risk(self, assets: list[dict[str, Any]]) -> None:
        """Score risk by ranking the current period across the selected assets.

        Used when no baseline period is available: downtime, work-order count and
        MTTR are each min-max normalised across the assets, then blended.
        """

        def rel(values: list[float], value: float) -> float:
            lo, hi = min(values), max(values)
            if hi <= lo:
                return 50.0 if value > 0 else 0.0
            return (value - lo) / (hi - lo) * 100.0

        downtimes = [a["current_downtime_hours"] or a["total_downtime_hours"] for a in assets]
        counts = [float(a["current_wo_count"] or a["work_order_count"]) for a in assets]
        mttrs = [float(a["mttr_hours"] or 0.0) for a in assets]
        for asset in assets:
            dt = asset["current_downtime_hours"] or asset["total_downtime_hours"]
            ct = float(asset["current_wo_count"] or asset["work_order_count"])
            mt = float(asset["mttr_hours"] or 0.0)
            score = (
                0.45 * rel(downtimes, dt)
                + 0.275 * rel(counts, ct)
                + 0.275 * rel(mttrs, mt)
            )
            asset["risk_score"] = round(max(0.0, min(100.0, score)), 1)
            if asset["risk_score"] >= 70 and asset["alert_count"] == 0:
                asset["alert_count"] = 1

    @staticmethod
    def _increase_score(current: float, baseline: float) -> float:
        """0–100 score for how much ``current`` rose above ``baseline``.

        No rise scores 0; a doubling (or a value appearing where the baseline was
        zero) scores 100. Decreases are treated as 0 (improving, not a risk).
        """

        if current <= 0:
            return 0.0
        if baseline <= 0:
            return 100.0
        change = (current - baseline) / baseline
        if change <= 0:
            return 0.0
        return min(100.0, change * 100.0)

    @staticmethod
    def _pct_change(current: float, baseline: float) -> float | None:
        """Percent change from ``baseline`` to ``current`` (None when undefined)."""

        if baseline and baseline > 0:
            return round((current - baseline) / baseline * 100.0, 1)
        if current > 0:
            return None  # baseline of zero -> "new" activity, not a finite percent
        return 0.0

    @staticmethod
    def _risk_status(risk_score: float | None) -> str:
        if risk_score is None:
            return "Low"
        if risk_score >= 70:
            return "High"
        if risk_score >= 40:
            return "Medium"
        return "Low"

    def _parse_metric_date(self, value: str | None, *, end_of_day: bool) -> datetime | None:
        """Parse a ``YYYY-MM-DD`` filter bound into a UTC datetime (or None)."""

        parsed = self._parse_datetime(value)
        if parsed is None:
            return None
        if end_of_day:
            return parsed.replace(hour=23, minute=59, second=59, microsecond=0)
        return parsed.replace(hour=0, minute=0, second=0, microsecond=0)

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
            # A PM reset with a target mode but no mechanism is aimed at the whole
            # mode (it restarts the mode and every mechanism under it), so only a
            # missing mode leaves it unfinished.
            return (
                "AND (d.event_disposition_id IS NULL OR ("
                "d.disposition_category = 'INCLUDED_PM_RESET_EVENT' "
                "AND d.reset_target_failure_mode_id IS NULL))"
            )
        raise ValueError("Disposition kind must be 'wo' or 'pm'.")

    # ---- disposition table ordering ------------------------------------------
    # Every column the disposition table can be sorted by, keyed by the name its
    # API row carries so the browser names a column and the server orders by it.
    # The value is (SQL expression, column type): the expression produces the
    # value the cell shows, and the type is what stops the ordering from being a
    # text comparison -- dates go through gremlin_sort_datetime, numbers compare
    # as numbers, and text compares case-insensitively.
    #
    # Sorting is done here rather than in the browser because the table is
    # paginated: reordering the 50 rows on screen answers "which of these 50 is
    # oldest", when what the question means is "which of this asset's records is
    # oldest". Ordering in SQL puts that row on page 1.
    def _disposition_sort_expressions(self, kind: str) -> dict[str, tuple[str, str]]:
        if kind not in ("wo", "pm"):
            raise ValueError("Disposition kind must be 'wo' or 'pm'.")
        # The narrative is four boxes rendered as one cell, and the cell shows only
        # the boxes that were filled in, each captioned, joined by " · "
        # (narrativeText in life_data_analysis.js). The sort key is built the same
        # way, because ordering the raw values run together orders something the
        # screen does not show: a row reading "Area Affected: Z" would sort before
        # one reading "Condition: A" on the "Z", while the cells read the other way
        # round. The separator trails the last entry instead of sitting between
        # them, which is the same order with less SQL -- every key carries the same
        # suffix, so it can never decide a comparison.
        narrative = " || ".join(
            "CASE WHEN NULLIF(TRIM(m.{key}), '') IS NOT NULL "
            "THEN '{label}: ' || TRIM(m.{key}) || ' · ' ELSE '' END".format(
                key=field.key, label=field.label.replace("'", "''")
            )
            for field in NARRATIVE_FIELDS
        )
        # The read-only source columns come straight from DISPLAY_COLUMN_SOURCES,
        # so every column the table draws is one the table can be sorted by.
        columns: dict[str, tuple[str, str]] = dict(DISPLAY_COLUMN_SOURCES)
        columns.update({
            "failure_narrative": (f"({narrative})", COLUMN_TYPE_TEXT),
            "disposition_notes": ("COALESCE(NULLIF(TRIM(d.disposition_notes), ''), d.disposition_text)", COLUMN_TYPE_TEXT),
            "disposition_category": ("COALESCE(NULLIF(TRIM(d.disposition_category), ''), 'UNKNOWN')", COLUMN_TYPE_TEXT),
            "effective_record_class": ("COALESCE(d.record_class_final, m.record_class_final, m.record_class_auto)", COLUMN_TYPE_TEXT),
            "modeled_population_name": (
                "COALESCE(NULLIF(TRIM(mp.population_name), ''), '{}')".format(
                    MODELED_POPULATION_PLACEHOLDER.replace("'", "''")
                ),
                COLUMN_TYPE_TEXT,
            ),
            # The screen's checkbox shows the flag a saved disposition stores
            # (see buildDispositionControls), so the ordering reads the same
            # flag or it disagrees with the boxes it is sorting. The category
            # default the screen falls back to only applies without a saved
            # disposition, where there is no category to imply one either.
            "include_in_weibull_candidate": ("COALESCE(d.include_in_weibull_candidate, 0)", COLUMN_TYPE_BOOLEAN),
        })
        if kind == "pm":
            columns.update(
                {
                    "pm_reset_inclusion_decision": (
                        "COALESCE(NULLIF(TRIM(d.pm_reset_inclusion_decision), ''), 'NEEDS_REVIEW')",
                        COLUMN_TYPE_TEXT,
                    ),
                    "reset_target_failure_mode": ("rtfm.failure_mode_name", COLUMN_TYPE_TEXT),
                    "reset_target_failure_mechanism": ("rtfmech.failure_mechanism_name", COLUMN_TYPE_TEXT),
                    "pm_reset_renewal_rationale": ("d.pm_reset_renewal_rationale", COLUMN_TYPE_TEXT),
                }
            )
        else:
            columns.update(
                {
                    "failure_mode": ("fm.failure_mode_name", COLUMN_TYPE_TEXT),
                    "failure_mechanism": ("fmech.failure_mechanism_name", COLUMN_TYPE_TEXT),
                }
            )
        return columns

    def disposition_sort_columns(self, kind: str) -> dict[str, str]:
        """The disposition table's sortable columns for ``kind``, as name -> type.

        Handed to the browser so a column menu can offer the sort its column
        actually is ("Oldest -> Newest" on a date, "Smallest -> Largest" on a
        number) and so an unknown column name is refused rather than pasted into
        SQL.
        """

        return {key: column_type for key, (_, column_type) in self._disposition_sort_expressions(kind).items()}

    @staticmethod
    def _sort_key_expressions(expression: str, column_type: str) -> list[str]:
        """The ORDER BY term(s) that compare ``expression`` as ``column_type``."""

        if column_type == COLUMN_TYPE_DATETIME:
            return [f"gremlin_sort_datetime({expression})"]
        if column_type == COLUMN_TYPE_NUMBER:
            # A number the CMMS stored as text ("1042") compares as the number it
            # is. Anything that is not a number is NULL rather than the 0.0 a CAST
            # would make of it -- "A-14" is not the smallest task id on the asset,
            # and reading it as one put it at the top of the ascending page. It
            # sorts with the blanks at the end instead, and the text form below
            # orders those among themselves rather than leaving them arbitrary.
            # Three keys: the number, then every integer in an exact form, then the
            # text. The middle one only ever decides a tie the first key could not,
            # which past SQLite's integer width is the only place it has any left.
            return [
                f"gremlin_sort_number({expression})",
                f"gremlin_sort_integer({expression})",
                LifeDataService._text_sort_key(expression),
            ]
        if column_type == COLUMN_TYPE_BOOLEAN:
            return [f"CAST({expression} AS INTEGER)"]
        return [LifeDataService._text_sort_key(expression)]

    @staticmethod
    def _text_sort_key(expression: str) -> str:
        """Compare ``expression`` as text, case-insensitively, empty cells as NULL.

        A cell is empty when it holds nothing, and whitespace is something it
        holds: " 7 " compares as the characters it has rather than as "7", and a
        cell of three spaces is a value rather than a blank. Trimming anything off
        before the comparison would sort a cell somewhere other than where it
        reads, and put the table's order out of step with the workbook's, which
        can only compare what is actually in the cell.

        Both the text columns and the text half of a number column's ordering read
        this one expression -- as two copies they drifted, and the number column
        kept comparing a trimmed value after the text columns had stopped.
        """

        return f"NULLIF({expression}, '') COLLATE NOCASE"

    def _disposition_order_by(self, kind: str, sort: str | None, sort_dir: str | None) -> str:
        """The ORDER BY for one disposition page, typed by column."""

        columns = self._disposition_sort_expressions(kind)
        if not sort or sort not in columns:
            # No column chosen: the date the record actually happened, read as a
            # date rather than as the text SQLite holds it in, then task id
            # numerically so 9 comes before 10.
            return (
                "ORDER BY gremlin_sort_datetime(COALESCE(m.completed_date_final, m.start_date_final, m.created_date_final)),"
                # The same three keys the chosen-column path uses, for the same
                # reason: past SQLite's integer width the first one rounds, and
                # m.task_id alone would order negatives backwards.
                " gremlin_sort_number(m.task_id), gremlin_sort_integer(m.task_id),"
                " m.task_id, m.mapped_record_id"
            )
        expression, column_type = columns[sort]
        order = "DESC" if str(sort_dir or "").lower() == "desc" else "ASC"
        keys = self._sort_key_expressions(expression, column_type)
        # The blocks first, then the values inside them. mapped_record_id breaks
        # the remaining ties: without a total order, two rows that compare equal
        # can swap between pages and the same record shows up twice, or not at all.
        clauses = [self._sort_group_rank(expression, column_type, keys[0], order)]
        clauses.extend(f"{key} {order}" for key in keys)
        clauses.append("m.mapped_record_id")
        return "ORDER BY " + ", ".join(clauses)

    def _sort_group_rank(self, expression: str, column_type: str, sort_key: str, order: str) -> str:
        """Which block a row belongs to, so the blocks sit where a spreadsheet puts them.

        Empty cells go last whichever way the column points, so sorting
        descending never opens on a page of blanks.

        A number column has a third block between those two: the values that are
        not numbers. A task id like "A-14" is neither a number nor an empty cell,
        and reading it as either misplaces it -- as 0.0 (what CAST would make of
        it) it led the ascending page as the smallest id on the asset; pinned with
        the blanks it sat at the bottom of a descending sort, while the workbook
        built from the same rows put it at the top. A spreadsheet keeps text in a
        block of its own that swaps ends with the direction, after the numbers
        ascending and ahead of them descending, and that is what this reproduces.

        Dates keep the two-block rule. A value that will not parse as a date is a
        broken date rather than a value of another kind -- 2025-02-31 is somebody's
        typo, not an identifier -- and ImpossibleDateTests pins those to the end in
        both directions, where a data-quality problem is found in one place.
        """

        if column_type != COLUMN_TYPE_NUMBER:
            return f"CASE WHEN {sort_key} IS NULL THEN 1 ELSE 0 END"
        numbers, text = ("0", "1") if order == "ASC" else ("1", "0")
        return (
            f"CASE WHEN NULLIF({expression}, '') IS NULL THEN 2"
            f" WHEN {sort_key} IS NOT NULL THEN {numbers} ELSE {text} END"
        )

    @staticmethod
    def _escape_like(value: str) -> str:
        # Escape LIKE wildcards so user-typed % / _ are matched literally (paired
        # with ESCAPE '\' on the LIKE expression).
        return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")

    def _disposition_search_clause(self, search: str | None) -> tuple[str, list[Any]]:
        """Build a parameterized WHERE fragment that free-text filters disposition
        rows. Each whitespace-separated token must appear (case-insensitively, via
        SQLite's ASCII LIKE) in at least one of the readable record columns shown on
        the table, so users can filter by task ID, dates, downtime, notes, titles or
        description using either text or numbers."""

        if not search:
            return "", []
        tokens = [token for token in search.split() if token]
        if not tokens:
            return "", []
        searchable = (
            "m.task_name",
            "CAST(m.task_id AS TEXT)",
            "m.created_date_final",
            "m.completed_date_final",
            # The date columns are also matched in the normalised form the table
            # renders them in, so a value copied out of a date cell still finds
            # its row even though the database holds it as "2026-01-15T15:00:00+00:00".
            "gremlin_sort_datetime(m.created_date_final)",
            "gremlin_sort_datetime(m.completed_date_final)",
            "CAST(ROUND(m.downtime_hours, 2) AS TEXT)",
            "m.completion_notes",
            "m.request_title",
            "m.requestor_description",
            "m.area_affected",
            "m.condition_found",
            "m.cause",
            "m.action_taken",
        )
        per_token = " OR ".join(f"{column} LIKE ? ESCAPE '\\'" for column in searchable)
        clauses: list[str] = []
        params: list[Any] = []
        for token in tokens:
            clauses.append(f"({per_token})")
            params.extend([f"%{self._escape_like(token)}%"] * len(searchable))
        return " AND " + " AND ".join(clauses), params

    def disposition_row_count(self, asset_number: str, kind: str, *, only_needing_disposition: bool = False, search: str | None = None) -> int:
        where = self._disposition_where(kind)
        needs_disposition_where = self._needs_disposition_where(kind) if only_needing_disposition else ""
        search_clause, search_params = self._disposition_search_clause(search)
        with self.connect() as conn:
            row = conn.execute(
                f"""
                SELECT COUNT(*) AS count
                FROM mapped_cmms_record m
                LEFT JOIN event_disposition d ON d.mapped_record_id = m.mapped_record_id AND d.is_current = 1
                WHERE m.asset_number = ? AND {where} {needs_disposition_where}{search_clause}
                """,
                (asset_number, *search_params),
            ).fetchone()
        return int(row["count"] or 0)

    def disposition_rows(self, asset_number: str, kind: str, *, only_needing_disposition: bool = False, limit: int | None = None, offset: int = 0, search: str | None = None, sort: str | None = None, sort_dir: str = "asc") -> list[dict[str, Any]]:
        where = self._disposition_where(kind)
        needs_disposition_where = self._needs_disposition_where(kind) if only_needing_disposition else ""
        search_clause, search_params = self._disposition_search_clause(search)
        # Ordering runs across the whole selection before LIMIT/OFFSET, so a sort
        # chosen on one page is a sort of every eligible row rather than of the
        # 50 that happen to be on screen.
        order_by = self._disposition_order_by(kind, sort, sort_dir)
        pagination = ""
        params: list[Any] = [asset_number, *search_params]
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
                {_DISPOSITION_ROW_SELECT}
                WHERE m.asset_number = ? AND {where} {needs_disposition_where}{search_clause}
                {order_by}
                {pagination}
                """,
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def disposition_record(self, asset_number: str, mapped_record_id: int) -> dict[str, Any] | None:
        """One record's current disposition, shaped like a disposition_rows() row.

        For the analysis tables, which open a single work order's disposition in
        place rather than sending the user to page through the disposition screen
        for it. Deliberately not narrowed by the record-type filter the table
        uses: a record that is in an analysis is worth being able to correct even
        if a saved record class has since moved it out of that screen's list.
        Scoped to the asset, since the taxonomy options offered beside it are that
        asset's. Returns ``None`` when the record is not on the asset.
        """

        mapped_record_id = int(mapped_record_id)
        # Wider than SQLite can bind is an id no record has, not a server error.
        if not -SQLITE_INTEGER_LIMIT <= mapped_record_id < SQLITE_INTEGER_LIMIT:
            return None
        with self.connect() as conn:
            row = conn.execute(
                f"""
                {_DISPOSITION_ROW_SELECT}
                WHERE m.asset_number = ? AND m.mapped_record_id = ?
                """,
                (asset_number, mapped_record_id),
            ).fetchone()
        return dict(row) if row is not None else None

    def disposition_excel_headers(self, kind: str) -> tuple[str, ...]:
        """Return the Excel template columns for a disposition screen."""

        if kind == "wo":
            return EXCEL_WO_DISPOSITION_COLUMNS
        if kind == "pm":
            return EXCEL_PM_DISPOSITION_COLUMNS
        raise ValueError("Disposition kind must be 'wo' or 'pm'.")

    def export_disposition_excel(self, asset_number: str, kind: str, output_path: str | Path, *, only_needing_disposition: bool = False) -> int:
        """Write the selected asset's disposition table to an Excel workbook.

        ``only_needing_disposition`` is the Rows selector's "Only new /
        undispositioned" setting, and it narrows the workbook the same way it
        narrows the table -- through the one WHERE clause both read, so the two
        can never disagree about which rows are new. The point of that setting is
        to work through the backlog, and a workbook of every eligible row hands
        the reader the job of finding the new ones again in Excel.

        The search box and the page you are on still do not narrow it: those cut
        the table down to look at something, while this one names which records
        are outstanding.
        """

        headers = self.disposition_excel_headers(kind)
        rows = self.disposition_rows(asset_number, kind, only_needing_disposition=only_needing_disposition)
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
            # Read-only in the workbook: import_disposition_excel only reads the
            # disposition columns, so an edit here is discarded rather than
            # written back over what the maintenance team recorded in Limble.
            record.update({key: row.get(key) for key in NARRATIVE_KEYS})
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
            column_types=EXCEL_COLUMN_TYPES,
            editable_columns=EXCEL_EDITABLE_COLUMNS,
        )
        return len(rows)

    # ---- Weibull report (Word .docx) -----------------------------------------
    @staticmethod
    def _safe_asset_token(asset_number: str) -> str:
        token = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in str(asset_number)).strip("_")
        return token or "asset"

    def next_weibull_report_number(self, asset_number: str, *, analysis_label: str = "", weibull_result_id: int | None = None) -> tuple[str, int]:
        """Reserve the next ``REL-WBL-RPT-<asset>-00x`` report number for an asset.

        The sequence increments per asset so repeat reports get -001, -002, … The
        reservation is recorded in ``weibull_report_log`` so numbers are never reused.
        """

        token = self._safe_asset_token(asset_number)
        with self.write_connection() as conn:
            row = conn.execute(
                "SELECT COALESCE(MAX(sequence_number), 0) AS last_seq FROM weibull_report_log WHERE asset_number = ?",
                (asset_number,),
            ).fetchone()
            sequence_number = int(row["last_seq"]) + 1 if row else 1
            report_number = f"REL-WBL-RPT-{token}-{sequence_number:03d}"
            conn.execute(
                """
                INSERT INTO weibull_report_log(asset_number, report_number, sequence_number, analysis_label, weibull_result_id)
                VALUES (?, ?, ?, ?, ?)
                """,
                (asset_number, report_number, sequence_number, analysis_label or None, weibull_result_id),
            )
        return report_number, sequence_number

    def load_weibull_result_for_report(self, result_id: int, asset_number: str) -> dict[str, Any]:
        """Load a persisted Weibull result and confirm it belongs to ``asset_number``.

        Everything the report states -- parameters, intervals, the life basis and window,
        the lives themselves and the interpretation summary -- is read back from the
        database (never trusted from the client), through the same view the page is
        drawn from, so a tampered request cannot mint a numbered REL report with
        arbitrary values or a result id borrowed from another asset.
        """

        with self.read_transaction() as conn:
            view = self._load_weibull_view(conn, int(result_id))
        if view is None:
            raise ValueError("That Weibull result no longer exists. Re-run the analysis before generating a report.")
        if view.asset_number != str(asset_number):
            raise ValueError("The selected Weibull result does not belong to this asset.")
        result = {name: getattr(view, name) for name in view.__dataclass_fields__}
        result["analysis_label"] = view.analysis_label or "Selected failure group"
        return result

    def build_weibull_report_docx(self, asset_number: str, payload: dict[str, Any], output_path: str | Path) -> str:
        """Write a Weibull report Word document and return its report number.

        The analysis fields are reloaded from the persisted result for the given asset
        (so the numbered REL report cannot be driven by tampered request data); only the
        chart images, the age to evaluate reliability at and a failure-mode population's
        fallback rationale come from the payload. A result is refused -- before any
        report number is reserved -- when it was saved under an earlier method version,
        was counted on a schedule the asset is no longer on, rests on fewer failure
        lives than the minimum, or is a failure-mode population
        with no written reason a mechanism could not be fitted instead.
        """

        client_result = payload.get("result") or {}
        result_id = self._optional_int_value(payload.get("result_id"))
        if result_id is None:
            result_id = self._optional_int_value(client_result.get("result_id"))
        if result_id is None:
            raise ValueError("A saved Weibull result id is required to generate a report.")
        result = self.load_weibull_result_for_report(result_id, asset_number)
        if not result["method_current"]:
            raise ValueError(
                f"This result was saved by an earlier version of GREMLIN's Weibull method ({result['method_version'] or 'unrecorded'}). "
                "Run the analysis again before reporting it."
            )
        if not result["schedule_current"]:
            counted_on = (result.get("life_basis") or {}).get("schedule_name") or "an earlier schedule"
            raise ValueError(
                f"This result's life hours were counted on the {counted_on} schedule, but asset {asset_number} is now on "
                f"{result['current_schedule_name']}. Run the analysis again before reporting it."
            )
        if not result["meets_minimum"]:
            raise ValueError(
                f"This result rests on {result['failure_count']} lives that end in a failure; a Weibull report needs at least "
                f"{MIN_WEIBULL_FAILURE_LIVES}."
            )
        rationale = self._without_unrepresentable_characters(str(payload.get("fallback_rationale") or "")).strip()
        if result["grouping_level"] == "FAILURE_MODE":
            rationale = rationale or str(result.get("fallback_rationale") or "").strip()
            if not rationale:
                raise ValueError(
                    "A report on a failure mode needs the reason it was fitted rather than one of its mechanisms "
                    "(REL-WBL-PLN-003 §8): failure mode is the fallback grouping, used when the records cannot "
                    "support a single mechanism."
                )
            if rationale != str(result.get("fallback_rationale") or "").strip():
                with self.write_connection() as conn:
                    conn.execute(
                        """
                        UPDATE modeled_population SET fallback_rationale = ?
                        WHERE modeled_population_id = (
                            SELECT ad.modeled_population_id
                            FROM weibull_result wr
                            JOIN weibull_analysis_run war ON war.weibull_analysis_run_id = wr.weibull_analysis_run_id
                            JOIN analysis_dataset ad ON ad.analysis_dataset_id = war.analysis_dataset_id
                            WHERE wr.weibull_result_id = ?
                        )
                        """,
                        (rationale, result_id),
                    )
            result["fallback_rationale"] = rationale
        target_age_hours = None
        raw_age = payload.get("target_age_hours")
        if raw_age not in (None, ""):
            try:
                target_age_hours = float(raw_age)
            except (TypeError, ValueError):
                target_age_hours = None
            if target_age_hours is None or not math.isfinite(target_age_hours) or target_age_hours <= 0:
                raise ValueError("The age to evaluate reliability at has to be a positive number of hours.")
        analysis_label = result["analysis_label"]
        report_number, _ = self.next_weibull_report_number(
            asset_number,
            analysis_label=analysis_label,
            weibull_result_id=result_id,
        )
        charts: list[dict[str, Any]] = []
        for chart in payload.get("charts") or []:
            if not chart:
                continue
            png_bytes = self._decode_data_url_png(chart.get("image"))
            if not png_bytes:
                continue
            charts.append({"title": chart.get("title"), "_png_bytes": png_bytes})
        body = self._weibull_report_body(report_number, asset_number, analysis_label, result, charts, target_age_hours=target_age_hours)
        self._write_docx(output_path, body, charts)
        return report_number

    @staticmethod
    def calendar_weeks_for_life_hours(life_hours: float | None, life_basis: dict[str, Any] | None) -> float | None:
        """How many calendar weeks it takes to build up ``life_hours`` on a result's schedule.

        Life hours are scheduled hours, and a weekend adds none, so a week holds five
        scheduled days of ``hours_per_day``: 100 hours on the 20-hour schedule. None
        when the schedule is not known.
        """

        if life_hours is None or not life_basis or not life_basis.get("hours_per_day"):
            return None
        days_per_week = 5.0 if life_basis.get("exclude_weekends", True) else 7.0
        weekly_hours = float(life_basis["hours_per_day"]) * days_per_week
        if weekly_hours <= 0:
            return None
        return float(life_hours) / weekly_hours

    def _weibull_report_body(
        self,
        report_number: str,
        asset_number: str,
        analysis_label: str,
        result: dict[str, Any],
        charts: list[dict[str, Any]],
        *,
        target_age_hours: float | None = None,
    ) -> str:
        """Build the ``word/document.xml`` body XML for a Weibull report.

        Carries what REL-WBL-MTH-001 §10 asks of an analysis package -- the modeled
        population and grouping level, the life basis and censoring, the fitted values
        and reliability outputs, the recommendation, and the limitations -- plus the lives
        themselves, so the report can be checked without GREMLIN to hand.
        """

        def fmt(value: Any, suffix: str = "") -> str:
            try:
                number = float(value)
            except (TypeError, ValueError):
                return "Not available"
            if not math.isfinite(number):
                return "Not available"
            return f"{number:.4g}{suffix}"

        life_basis = result.get("life_basis") or {}
        zone_name = life_basis.get("time_zone") or "UTC"
        try:
            zone: tzinfo = ZoneInfo(zone_name)
        except (ZoneInfoNotFoundError, ValueError, KeyError):
            zone, zone_name = timezone.utc, "UTC"

        def when(value: Any) -> str:
            parsed = self._parse_datetime(value)
            if parsed is None:
                return "Not recorded"
            return f"{parsed.astimezone(zone).strftime('%Y-%m-%d %H:%M')} {zone_name}"

        def cutoff_text() -> str:
            # An entered cutoff date runs to the end of that plant day, stored as the
            # next day's midnight: read it back as the day it names.
            parsed = self._parse_datetime(result.get("analysis_cutoff"))
            if parsed is None:
                return "Not recorded"
            local = parsed.astimezone(zone)
            if result.get("analysis_cutoff_source") == "USER" and (local.hour, local.minute, local.second) == (0, 0, 0):
                return f"End of {(local - timedelta(minutes=1)).strftime('%Y-%m-%d')} ({zone_name})"
            return when(result.get("analysis_cutoff"))

        def hours_with_weeks(value: Any) -> str:
            text = fmt(value, " hours")
            weeks = self.calendar_weeks_for_life_hours(value, life_basis) if text != "Not available" else None
            return f"{text} (about {weeks:.1f} calendar weeks)" if weeks is not None else text

        generated_on = datetime.now(zone).strftime("%Y-%m-%d %H:%M")
        beta = fmt(result.get("beta_mle"))
        eta = fmt(result.get("eta_mle"), " hours")
        beta_ci = (
            f"{fmt(result.get('beta_lower_ci'))} to {fmt(result.get('beta_upper_ci'))}"
            if result.get("beta_lower_ci") is not None and result.get("beta_upper_ci") is not None
            else "Not available"
        )
        eta_ci = (
            f"{fmt(result.get('eta_lower_ci'))} to {fmt(result.get('eta_upper_ci'), ' hours')}"
            if result.get("eta_lower_ci") is not None and result.get("eta_upper_ci") is not None
            else "Not available"
        )
        total = result.get("total_observation_count")
        failures = result.get("failure_count")
        censored = result.get("censored_count")
        is_mode = result.get("grouping_level") == "FAILURE_MODE"
        cutoff_source = {
            "LAST_IMPORT": "the last completed Limble import",
            "USER": "the cutoff date entered for the run",
            "NOW": "the time of the run",
        }.get(str(result.get("analysis_cutoff_source") or ""), "source not recorded")
        schedule_name = life_basis.get("schedule_name") or "Not recorded"
        if life_basis.get("exclude_weekends"):
            schedule_name += ", weekends excluded"

        parts: list[str] = []
        parts.append(self._docx_heading(report_number, level=1))
        parts.append(self._docx_heading("Weibull Reliability Analysis Report", level=2))
        parts.append(
            self._docx_paragraph(
                "Pre-release result: GREMLIN's Weibull automation has not yet been validated against the manual pilot "
                "baseline (REL-WBL-VAL-001 §7). Use this report for reliability engineering review, not as a stand-alone "
                "maintenance directive or a PM interval change.",
                bold=True,
            )
        )
        parts.append(self._docx_paragraph(f"Asset Number: {asset_number}", bold=True))
        parts.append(self._docx_paragraph(f"Failure population: {analysis_label}"))
        parts.append(
            self._docx_paragraph(
                "Grouping level: "
                + (
                    "Failure mode, the fallback grouping used when the records cannot support one mechanism (REL-WBL-DAT-002 §7.1)"
                    if is_mode
                    else "Failure mechanism"
                )
            )
        )
        if is_mode:
            parts.append(self._docx_paragraph(f"Why a failure mode rather than a mechanism: {result.get('fallback_rationale') or 'Not recorded'}"))
        parts.append(self._docx_paragraph(f"Report generated: {generated_on} {zone_name}"))

        parts.append(self._docx_heading("Life Basis and Analysis Window", level=2))
        basis_rows = [
            ["Life basis", "Schedule-adjusted elapsed hours: an exposure proxy, not run-meter hours"],
            ["Schedule", schedule_name],
            ["Days split at", f"Midnight, {zone_name}"],
        ]
        if life_basis.get("time_zone_warning"):
            basis_rows.append(["Time zone warning", str(life_basis["time_zone_warning"])])
        basis_rows += [
            ["Analysis start", when(result.get("analysis_start")) if result.get("analysis_start") else "All history in GREMLIN"],
            ["Analysis cutoff", f"{cutoff_text()}, {cutoff_source}"],
            [
                "Lives",
                f"{total} in total: {failures} end in a failure, {censored} right-censored "
                f"({result.get('pm_reset_censored_count', 0)} at a PM reset, {result.get('current_life_censored_count', 0)} current life)",
            ],
        ]
        parts.append(self._docx_table(["Item", "Value"], basis_rows))

        parts.append(self._docx_heading("Fitted Weibull Parameters", level=2))
        parameter_rows = [
            ["Shape (beta)", beta],
            ["Scale (eta)", hours_with_weeks(result.get("eta_mle"))],
            ["Mean time to failure (MTTF)", hours_with_weeks(result.get("mean_time_to_failure"))],
            ["B10 life", hours_with_weeks(result.get("b10_life"))],
            ["B50 life (median)", hours_with_weeks(result.get("b50_life"))],
            ["Beta 95% confidence interval", beta_ci],
            ["Eta 95% confidence interval", eta_ci],
            ["Probability plot R²", self._report_r_squared_text(result)],
        ]
        if target_age_hours is not None:
            try:
                reliability = math.exp(-((target_age_hours / float(result["eta_mle"])) ** float(result["beta_mle"])))
            except (TypeError, ValueError, ZeroDivisionError, OverflowError):
                reliability = None
            if reliability is not None:
                parameter_rows.append(
                    [
                        f"Reliability at {target_age_hours:g} hours",
                        f"R = {reliability:.3f}; probability of failing by then F = {1 - reliability:.3f}",
                    ]
                )
        parts.append(self._docx_table(["Parameter", "Value"], parameter_rows))

        if charts:
            parts.append(self._docx_heading("Analysis Graphs", level=2))
            for index, chart in enumerate(charts, start=1):
                title = str(chart.get("title") or f"Figure {index}")
                parts.append(self._docx_paragraph(title, bold=True))
                parts.append(self._docx_image_paragraph(index, chart))

        parts.append(self._docx_heading("Results Interpretation Summary", level=2))
        rows = result.get("interpretation_summary") or []
        table_rows = [
            [str(row.get("metric") or "—"), str(row.get("value") or "—"), str(row.get("recommendation") or "—")]
            for row in rows
        ]
        if not table_rows:
            table_rows = [["—", "—", "No interpretation summary is available for this Weibull result."]]
        parts.append(self._docx_table(["Metric", "Value", "Interpretation / Recommended Action"], table_rows))

        observations = result.get("observations") or []
        flagged = sum(1 for obs in observations if obs.get("data_quality_assumption_flag"))
        limitations = [
            self._weibull_limitations_text(
                life_basis.get("schedule_name") or "the weekday schedule", zone_name, life_basis.get("exclude_weekends") is not False
            ),
            "Confidence intervals are approximate (Fisher matrix, on log beta and log eta) and tend to run too narrow "
            "with few failures, so read beta and eta as directions until more failures accumulate.",
        ]
        if is_mode:
            limitations.append(
                "A failure-mode population pools every mechanism under it, which can blur beta: a wearing-out "
                "mechanism can hide inside a beta near 1. Fit the mechanism once it has enough failures."
            )
        if flagged:
            limitations.append(
                f"{flagged} {'life ends' if flagged == 1 else 'lives end'} within {DUPLICATE_CHECK_RAW_HOURS:g} hour of the "
                "event before, flagged for a duplicate check in the observation data below. A duplicate work order makes a "
                "near-zero life that pulls beta down."
            )
        parts.append(self._docx_heading("Limitations and Assumptions", level=2))
        for limitation in limitations:
            parts.append(self._docx_paragraph(f"• {limitation}"))

        parts.append(self._docx_heading("Observation Data", level=2))
        observation_rows = []
        for index, obs in enumerate(observations, start=1):
            ends_in = {
                "COMPLETED_FAILURE_LIFE": "Failure",
                "PM_RESET_CENSORED_LIFE": "PM reset (censored)",
                "RIGHT_CENSORED_LIFE": "Cutoff (current life)",
            }.get(str(obs.get("observation_type") or ""), str(obs.get("observation_type") or ""))
            note = str(obs.get("weibull_life_note") or "")
            if obs.get("data_quality_assumption_flag"):
                note = f"{note}. {obs['data_quality_assumption_flag']}" if note else str(obs["data_quality_assumption_flag"])
            observation_rows.append(
                [
                    str(index),
                    str(obs.get("source_task_id") or "—"),
                    ends_in,
                    when(obs.get("start_datetime")),
                    when(obs.get("end_datetime") or obs.get("analysis_cutoff_datetime")),
                    fmt(obs.get("life_hours_raw_elapsed")),
                    fmt(obs.get("life_hours_for_weibull")),
                    "1" if obs.get("failure_indicator") else "0",
                    note,
                ]
            )
        if not observation_rows:
            observation_rows = [["—"] * 8 + ["No observations were saved with this result."]]
        parts.append(
            self._docx_table(
                ["#", "Task ID", "Ends in", "Start", "End or cutoff", "Raw h", "Life h", "δ", "Note"],
                observation_rows,
                font_half_points=16,
            )
        )

        parts.append(self._docx_heading("Traceability", level=2))
        parts.append(
            self._docx_paragraph(
                f"Weibull result {result.get('result_id')}, run {when(result.get('run_datetime'))}, method "
                f"{result.get('method_version') or 'not recorded'}, {result.get('software_version') or 'software version not recorded'}."
            )
        )
        parts.append(
            self._docx_paragraph(
                "Recommendations are based on beta, eta, MTTF, and the approximate 95% confidence intervals for the "
                "fitted Weibull parameters. The GREMLIN Perform Analysis workspace shows the event processing table "
                "behind these lives, including every event that was left out and why.",
                italic=True,
            )
        )

        body = "".join(parts) + self._DOCX_SECTPR
        return (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<w:document '
            'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
            'xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing" '
            'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
            'xmlns:pic="http://schemas.openxmlformats.org/drawingml/2006/picture">'
            f"<w:body>{body}</w:body></w:document>"
        )

    _DOCX_SECTPR = (
        '<w:sectPr><w:pgSz w:w="12240" w:h="15840"/>'
        '<w:pgMar w:top="1440" w:bottom="1440" w:left="1440" w:right="1440" '
        'w:header="720" w:footer="720" w:gutter="0"/></w:sectPr>'
    )
    # Usable content width inside 1" margins on US-Letter (6.5 in) in EMU.
    _DOCX_CONTENT_WIDTH_EMU = 5943600

    def _docx_run(self, text: str, *, bold: bool = False, italic: bool = False, size_half_points: int | None = None) -> str:
        run_props = []
        if bold:
            run_props.append("<w:b/>")
        if italic:
            run_props.append("<w:i/>")
        if size_half_points:
            run_props.append(f'<w:sz w:val="{size_half_points}"/>')
        rpr = f"<w:rPr>{''.join(run_props)}</w:rPr>" if run_props else ""
        return f'<w:r>{rpr}<w:t xml:space="preserve">{escape(str(text))}</w:t></w:r>'

    def _docx_paragraph(self, text: str, *, bold: bool = False, italic: bool = False) -> str:
        return f"<w:p>{self._docx_run(text, bold=bold, italic=italic)}</w:p>"

    def _docx_heading(self, text: str, *, level: int = 1) -> str:
        size = {1: 36, 2: 28, 3: 24}.get(level, 24)
        spacing = '<w:spacing w:before="240" w:after="120"/>'
        return f"<w:p><w:pPr>{spacing}</w:pPr>{self._docx_run(text, bold=True, size_half_points=size)}</w:p>"

    def _docx_table(self, headers: list[str], rows: list[list[str]], *, font_half_points: int | None = None) -> str:
        border = '<w:tblBorders>' + "".join(
            f'<w:{edge} w:val="single" w:sz="4" w:space="0" w:color="BFBFBF"/>'
            for edge in ("top", "left", "bottom", "right", "insideH", "insideV")
        ) + '</w:tblBorders>'
        tbl_pr = (
            '<w:tblPr><w:tblStyle w:val="TableGrid"/>'
            '<w:tblW w:w="5000" w:type="pct"/>'
            f'{border}<w:tblLayout w:type="autofit"/></w:tblPr>'
        )

        def cell(text: str, *, header: bool = False) -> str:
            shade = '<w:shd w:val="clear" w:color="auto" w:fill="D9E2F3"/>' if header else ""
            tc_pr = f"<w:tcPr>{shade}</w:tcPr>" if shade else ""
            return f"<w:tc>{tc_pr}<w:p>{self._docx_run(text, bold=header, size_half_points=font_half_points)}</w:p></w:tc>"

        header_row = "<w:tr>" + "".join(cell(h, header=True) for h in headers) + "</w:tr>"
        body_rows = "".join("<w:tr>" + "".join(cell(value) for value in row) + "</w:tr>" for row in rows)
        return f"<w:tbl>{tbl_pr}{header_row}{body_rows}</w:tbl><w:p/>"

    def _docx_image_paragraph(self, index: int, chart: dict[str, Any]) -> str:
        width_px, height_px = self._png_pixel_size(chart.get("_png_bytes") or b"")
        if width_px <= 0 or height_px <= 0:
            width_px, height_px = 1000, 600
        emu_per_px = 9525  # 96 DPI
        cx = width_px * emu_per_px
        cy = height_px * emu_per_px
        if cx > self._DOCX_CONTENT_WIDTH_EMU:
            scale = self._DOCX_CONTENT_WIDTH_EMU / cx
            cx = int(cx * scale)
            cy = int(cy * scale)
        rid = f"rIdImg{index}"
        title = escape(str(chart.get("title") or f"Figure {index}"))
        return (
            "<w:p><w:r><w:drawing>"
            f'<wp:inline distT="0" distB="0" distL="0" distR="0">'
            f'<wp:extent cx="{cx}" cy="{cy}"/>'
            '<wp:effectExtent l="0" t="0" r="0" b="0"/>'
            f'<wp:docPr id="{index}" name="Picture {index}" descr="{title}"/>'
            '<wp:cNvGraphicFramePr><a:graphicFrameLocks noChangeAspect="1"/></wp:cNvGraphicFramePr>'
            '<a:graphic><a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/picture">'
            '<pic:pic>'
            f'<pic:nvPicPr><pic:cNvPr id="{index}" name="Picture {index}" descr="{title}"/><pic:cNvPicPr/></pic:nvPicPr>'
            f'<pic:blipFill><a:blip r:embed="{rid}"/><a:stretch><a:fillRect/></a:stretch></pic:blipFill>'
            '<pic:spPr>'
            f'<a:xfrm><a:off x="0" y="0"/><a:ext cx="{cx}" cy="{cy}"/></a:xfrm>'
            '<a:prstGeom prst="rect"><a:avLst/></a:prstGeom>'
            '</pic:spPr></pic:pic>'
            '</a:graphicData></a:graphic>'
            "</wp:inline></w:drawing></w:r></w:p>"
        )

    @staticmethod
    def _png_pixel_size(data: bytes) -> tuple[int, int]:
        # PNG IHDR holds big-endian width/height at byte offsets 16 and 20.
        if len(data) >= 24 and data[:8] == b"\x89PNG\r\n\x1a\n":
            width = int.from_bytes(data[16:20], "big")
            height = int.from_bytes(data[20:24], "big")
            return width, height
        return 0, 0

    @staticmethod
    def _decode_data_url_png(image: str) -> bytes:
        raw = str(image or "")
        if "," in raw and raw.strip().lower().startswith("data:"):
            raw = raw.split(",", 1)[1]
        try:
            return base64.b64decode(raw, validate=False)
        except (ValueError, TypeError):
            return b""

    def _write_docx(self, output_path: str | Path, document_xml: str, charts: list[dict[str, Any]]) -> None:
        """Assemble a minimal Word .docx package using only the standard library."""

        image_rels: list[str] = []
        image_files: list[tuple[str, bytes]] = []
        for index, chart in enumerate(charts, start=1):
            png_bytes = chart.get("_png_bytes")
            if not png_bytes:
                continue
            filename = f"media/image{index}.png"
            image_files.append((f"word/{filename}", png_bytes))
            image_rels.append(
                f'<Relationship Id="rIdImg{index}" '
                'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" '
                f'Target="{filename}"/>'
            )

        content_types = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Default Extension="png" ContentType="image/png"/>'
            '<Override PartName="/word/document.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
            '</Types>'
        )
        root_rels = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
            'Target="word/document.xml"/></Relationships>'
        )
        document_rels = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            + "".join(image_rels)
            + '</Relationships>'
        )

        with zipfile.ZipFile(output_path, "w", compression=zipfile.ZIP_DEFLATED) as document:
            document.writestr("[Content_Types].xml", content_types)
            document.writestr("_rels/.rels", root_rels)
            document.writestr("word/document.xml", document_xml)
            document.writestr("word/_rels/document.xml.rels", document_rels)
            for arcname, data in image_files:
                document.writestr(arcname, data)

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

        The free text is compared as the workbook can carry it, on both sides. A
        row saved before that cleaning existed still holds a character the sheet
        cannot, so the cell comes back without it and a plain comparison reads
        that as an edit: uploading a workbook nobody touched wrote a fresh
        disposition, dropped the character, and left a version in the history
        attributed to whoever uploaded. Comparing both sides through the same
        cleaning asks what the question means -- would this workbook change
        anything a workbook can express -- and leaves the stored value alone until
        somebody actually edits that row, when the write-time cleaning takes it.
        """

        default_class = "CORRECTIVE_WO" if kind == "wo" else "PM"
        current_category = current_row.get("disposition_category") or "UNKNOWN"
        current_class = current_row.get("effective_record_class") or default_class
        current_notes = self._comparable_text(current_row.get("disposition_notes") or current_row.get("disposition_text"))
        current_include = bool(current_row.get("include_in_weibull_candidate"))
        imported_include = imported.get("include_in_weibull_candidate")
        imported_include_bool = current_include if imported_include is None else bool(imported_include)

        if (
            current_category != imported.get("disposition_category")
            or current_class != imported.get("record_class_final")
            or current_notes != self._comparable_text(imported.get("disposition_text"))
            or current_include != imported_include_bool
        ):
            return False

        if kind == "pm":
            return (
                (current_row.get("pm_reset_inclusion_decision") or "NEEDS_REVIEW") == imported.get("pm_reset_decision")
                and self._comparable_text(current_row.get("pm_reset_renewal_rationale")) == self._comparable_text(imported.get("pm_reset_rationale"))
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

    def _comparable_text(self, value: Any) -> str:
        """``value`` as the workbook carries it, for deciding whether a row changed.

        The same cleaning the export applies, so a value stored before that
        cleaning existed compares equal to the cell it produces. Idempotent, so it
        does not matter which side has already been through it.
        """

        return self._without_unrepresentable_characters(self._excel_text(value))

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

    def _write_xlsx(self, output_path: str | Path, rows: list[list[Any]], sheet_name: str, *, validations: list[ExcelValidation] | None = None, lookup_rows: list[list[Any]] | None = None, column_types: dict[str, str] | None = None, editable_columns: frozenset[str] | None = None) -> None:
        """Write a simple Excel-compatible .xlsx workbook using only the standard library.

        ``column_types`` maps a header name to one of the ``COLUMN_TYPE_*``
        constants and is what makes the sheet sortable: without it every cell is
        text, and Excel orders and filters text as text. The styles part carries
        the date format those typed cells are drawn in, so a date reaches the
        reader as a date rather than as the five-digit number Excel stores it as.

        ``editable_columns`` names the columns the reader is meant to fill in.
        Those are drawn highlighted and the rest under a grey header, so the sheet
        itself shows which cells an upload will read back.
        """

        include_lookup_sheet = bool(lookup_rows)
        with zipfile.ZipFile(output_path, "w", compression=zipfile.ZIP_DEFLATED) as workbook:
            workbook.writestr("[Content_Types].xml", self._xlsx_content_types(include_lookup_sheet=include_lookup_sheet))
            workbook.writestr("_rels/.rels", self._xlsx_root_rels())
            workbook.writestr("xl/workbook.xml", self._xlsx_workbook_xml(sheet_name, include_lookup_sheet=include_lookup_sheet))
            workbook.writestr("xl/_rels/workbook.xml.rels", self._xlsx_workbook_rels(include_lookup_sheet=include_lookup_sheet))
            workbook.writestr("xl/styles.xml", self._xlsx_styles_xml())
            workbook.writestr(
                "xl/worksheets/sheet1.xml",
                # The header row is the filter row, so the sheet opens with
                # Excel's own sort/filter menu on every column rather than
                # leaving the reader to select the range and find it themselves.
                self._xlsx_sheet_xml(rows, validations=validations, column_types=column_types, editable_columns=editable_columns, auto_filter=True),
            )
            if include_lookup_sheet:
                # The dropdown source lists: plain text, and nothing sorts or
                # filters them, so they stay untyped and unfiltered.
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
                        sheet_path = self._xlsx_part_path(rel.attrib.get("Target", "worksheets/sheet1.xml"))
                        break
            if sheet_path not in workbook.namelist():
                raise ValueError("That .xlsx file has no readable first worksheet. Re-download the template and fill that in.")
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
<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>
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
        # rId3 whether or not the lookup sheet is there, so the id a sheet holds
        # never depends on how many sheets came before it.
        return f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>{lookup_rel}
<Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
</Relationships>'''

    def _xlsx_styles_xml(self) -> str:
        """The workbook's number formats and fonts, indexed by the EXCEL_STYLE_* constants.

        A date is a plain number in a sheet; only the format a cell is drawn in
        says it is a date, so without this part every exported date reads as
        46027 -- sortable, and unreadable. numFmtId 164 is the first id reserved
        for custom formats; anything below 163 is one of Excel's own built-ins.

        Fills 0 and 1 are the two Excel reserves whether a workbook uses them or
        not; the three after them are the editable-column yellow (a paler one for
        the cells, a stronger one for the header) and the read-only header grey.
        """

        return '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
<numFmts count="2">
<numFmt numFmtId="164" formatCode="yyyy\\-mm\\-dd\\ hh:mm"/>
<numFmt numFmtId="165" formatCode="0.00"/>
</numFmts>
<fonts count="2"><font><sz val="11"/><name val="Calibri"/></font><font><b/><sz val="11"/><name val="Calibri"/></font></fonts>
<fills count="5">
<fill><patternFill patternType="none"/></fill>
<fill><patternFill patternType="gray125"/></fill>
<fill><patternFill patternType="solid"><fgColor rgb="FFFFF2CC"/><bgColor indexed="64"/></patternFill></fill>
<fill><patternFill patternType="solid"><fgColor rgb="FFFFD966"/><bgColor indexed="64"/></patternFill></fill>
<fill><patternFill patternType="solid"><fgColor rgb="FFD9D9D9"/><bgColor indexed="64"/></patternFill></fill>
</fills>
<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>
<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
<cellXfs count="9">
<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>
<xf numFmtId="0" fontId="1" fillId="0" borderId="0" xfId="0" applyFont="1"/>
<xf numFmtId="164" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>
<xf numFmtId="165" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>
<xf numFmtId="0" fontId="1" fillId="4" borderId="0" xfId="0" applyFont="1" applyFill="1"/>
<xf numFmtId="0" fontId="1" fillId="3" borderId="0" xfId="0" applyFont="1" applyFill="1"/>
<xf numFmtId="0" fontId="0" fillId="2" borderId="0" xfId="0" applyFill="1"/>
<xf numFmtId="164" fontId="0" fillId="2" borderId="0" xfId="0" applyNumberFormat="1" applyFill="1"/>
<xf numFmtId="165" fontId="0" fillId="2" borderId="0" xfId="0" applyNumberFormat="1" applyFill="1"/>
</cellXfs>
<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>
</styleSheet>'''

    def _xlsx_safe_text(self, value: Any) -> str:
        """``value`` as text a workbook can actually carry (see ILLEGAL_XML_CHARACTERS)."""

        return self._without_unrepresentable_characters(str(value))

    @staticmethod
    def _xlsx_inline_text(text: str) -> str:
        """One inline string, with its whitespace marked significant where it is.

        A bare <t> lets a reader fold the whitespace at either end away, which for
        a task id of " 123 " means the workbook hands back the record next to it
        -- the rewrite the parse was changed to stop, undone one layer further out
        by the file format. xml:space says the characters are the value. It is
        only written where it changes something, which is how every other writer
        does it and keeps an ordinary sheet from carrying it on every cell.
        """

        marked = ' xml:space="preserve"' if text != text.strip() else ""
        return f"<t{marked}>{escape(text)}</t>"

    def _excel_serial_datetime(self, value: Any) -> float | None:
        """``value`` as the day count Excel stores a date as, or None if it is not a date.

        Parsed by the same reader the screen's ordering uses, so a workbook and
        the table agree on which values are dates and on what each one says; the
        result is UTC, which is the normalised form the table already renders.

        A date Excel cannot count at all -- anything before 1900 -- is None, and
        the cell keeps the text, the same answer every other value that will not
        convert gets.
        """

        parsed = self._parse_datetime(value)
        if parsed is None or parsed < EXCEL_FIRST_DATE:
            return None
        epoch = EXCEL_DATE_EPOCH if parsed >= EXCEL_PHANTOM_DAY_PASSED else EXCEL_DATE_EPOCH_BEFORE_THE_PHANTOM_DAY
        # A clock time is a fraction of a day, and most of them (17:30 among
        # them) have no exact binary representation, so the serial always lands a
        # fraction of a microsecond off the second it means. Every reader settles
        # that the same way, by rounding to the nearest second -- which is why the
        # 11 decimal places kept here are enough: they shorten the cell text
        # without moving the value far enough to round to a different second.
        return round((parsed - epoch).total_seconds() / 86400.0, 11)

    @staticmethod
    def _parse_number(value: Any) -> int | float | None:
        """``value`` as the number it is, or None when it is not a number.

        The single rule for "is this column's value a number", read by the table's
        ORDER BY (through ``gremlin_sort_number``) and by the workbook writer. One
        rule is the point: the CMMS hands ids and quantities over as text, and if
        the two halves disagreed about which strings are numbers, a value would
        sort among the numbers on one and among the text on the other.

        A value that is already a number is taken as one, bar infinity and NaN --
        neither has a spreadsheet representation, and neither orders sensibly.
        Nor is a string of digits too long for Python to convert at all.
        Text is read as a number only when it is the number's own canonical form,
        which is what keeps an identifier's spelling from being rewritten; the
        integer is parsed with ``int()`` rather than ``float()``, which would
        round a long id to the nearest value a double can hold before anybody
        could notice ("9007199254740993" comes back out of ``float()`` as ...992,
        a different work order).
        """

        if isinstance(value, bool) or value is None:
            return None
        if isinstance(value, int):
            return value
        if isinstance(value, float):
            return value if math.isfinite(value) else None
        # Anything reaching here arrived as text, and on these screens that means
        # a task id -- the one number-typed column the CMMS stores as TEXT, and an
        # identifier rather than a quantity (downtime_hours is REAL, so it is
        # already a float above and never takes this path). What an identifier is
        # written as is part of which record it names, so it is only read as a
        # number when the number writes back as the same characters: "1234" does,
        # while "001234", "+1234", "1e3", "1,042" and "1234.0" each name a record
        # that no number spells the same way. Those keep their own text.
        #
        # Compared against the value exactly as stored, with nothing trimmed off
        # it first: the CMMS field is copied in whole (_get_alias does not trim),
        # so " 123 " is a task id of its own, and stripping it before asking
        # whether the spelling converts cleanly is the rewrite this check exists
        # to refuse -- it would answer for "123", a different record.
        text = str(value)
        if not INTEGER_TEXT.fullmatch(text):
            return None
        try:
            whole = int(text)
            return whole if str(whole) == text else None
        except ValueError:
            # Python refuses to convert an integer this wide, in either direction
            # (sys.get_int_max_str_digits, 4300 by default): the conversion is
            # quadratic, so a long enough string is a way to hang a process. This
            # runs inside the ORDER BY, where raising does not spoil one cell but
            # takes the whole disposition page down with it -- and the export with
            # it. A value no number can be made of is text, which is what every
            # other value that will not convert already becomes.
            return None

    def _number_sort_key(self, value: Any) -> int | float | None:
        """``value`` as a number ORDER BY can compare, or NULL when it is not one.

        Registered on every connection as ``gremlin_sort_number`` (see
        ``connect``). SQLite's own ``CAST(x AS REAL)`` cannot say "not a number":
        it reads "A-14" as 0.0, which sorted a task id that is not a number in
        among the ones that are, as the smallest of them. Returning NULL instead
        puts it with the blanks at the end of the column, which is where the
        column already promises to keep a cell it has no value for, and where a
        spreadsheet puts text in an ascending sort.

        An integer is handed over as an integer. SQLite compares INTEGER values
        exactly, and rounding one to a double here would undo the exactness
        _parse_number just went to the trouble of keeping: -9007199254740993 and
        -9007199254740992 become one key, tie, and are then separated by the text
        key, which orders negative numbers backwards. Past the width SQLite
        stores an integer in there is no exact key left to give it, so those
        compare as doubles and tie with their neighbours; gremlin_sort_integer
        breaks exactly those ties.
        """

        number = self._parse_number(value)
        if number is None:
            return None
        if isinstance(number, int) and -SQLITE_INTEGER_LIMIT <= number < SQLITE_INTEGER_LIMIT:
            return number
        try:
            return float(number)
        except OverflowError:
            # An integer too large for a double at all. It is still bigger than
            # everything else in the column, so it sorts there rather than
            # disappearing into the blanks -- and raising here would take down the
            # whole page, since this runs inside the ORDER BY.
            return math.inf if number > 0 else -math.inf

    def _integer_sort_key(self, value: Any) -> str | None:
        """Every integer as a string that compares in the integer's own order.

        Registered as ``gremlin_sort_integer``, and read after
        ``gremlin_sort_number`` to settle the ties that one cannot. Past SQLite's
        integer width the number key has to become a double, and a double at that
        magnitude cannot tell -9223372036854775809 from -9223372036854775808: they
        tie, and the text key behind them orders negatives backwards, so the pair
        came out reversed in both directions.

        It covers every integer rather than only the ones that were rounded,
        because a tie group can hold one of each -- that pair is exactly such a
        group, -9223372036854775808 being the last value SQLite holds exactly and
        -9223372036854775809 the first it cannot. Keyed only on the rounded ones,
        the first of them would have returned NULL, which sorts ahead of any
        string and put the pair back in the wrong order. Non-integers return NULL:
        a float column returns it for every row, so this decides nothing there.

        The encoding is ordinary text comparison made to agree with numeric
        comparison: a leading digit puts negatives before everything else, then the
        digit count so that longer means larger, then the digits. Both are inverted
        for a negative, where more digits means smaller.
        """

        number = self._parse_number(value)
        if not isinstance(number, int):
            return None
        digits = str(abs(number))
        # The length field is fixed width so that the comparison stays a plain text
        # comparison. An id past what it can count is past what anyone can write.
        length = min(len(digits), 99999)
        if number >= 0:
            return f"2{length:05d}{digits}"
        inverted = "".join(str(9 - int(digit)) for digit in digits)
        return f"0{99999 - length:05d}{inverted}"

    def _excel_number_value(self, value: Any) -> int | float | None:
        """``value`` as a number a cell can hold exactly, or None to keep it as text.

        The same rule as the ordering, with one more question asked of it: a
        spreadsheet stores every number as a double, so a whole number past
        EXACT_INTEGER_LIMIT would be written back rounded. For a quantity that is
        a rounding; for an id it is a different record, so those stay text, which
        is the only form that survives the trip intact.
        """

        number = self._parse_number(value)
        if isinstance(number, int) and abs(number) > EXACT_INTEGER_LIMIT:
            return None
        return number

    def _xlsx_cell_xml(self, reference: str, value: Any, column_type: str | None, *, style: int = EXCEL_STYLE_DEFAULT, editable: bool = False) -> str:
        """One cell, written as the type its column holds.

        A value that will not convert falls through to text rather than being
        dropped: a task id of "A-14", a date the CMMS recorded as "unknown", and a
        downtime somebody typed a note into all still reach the reader. Such a row
        sorts among the text at the end of the column, which is what Excel does
        with a mixed column, and is the visible signal that the cell needs
        looking at.

        ``editable`` draws the cell over the editable-column fill, empty or not --
        a blank disposition cell is exactly the one the reader has to find.
        """

        if editable:
            style = EXCEL_STYLE_EDITABLE
            datetime_style, decimal_style = EXCEL_STYLE_EDITABLE_DATETIME, EXCEL_STYLE_EDITABLE_DECIMAL
        else:
            datetime_style, decimal_style = EXCEL_STYLE_DATETIME, EXCEL_STYLE_DECIMAL
        styled = f' s="{style}"' if style else ""
        # Only an absent value leaves an empty cell. A string of spaces is what the
        # record holds, and blanking it here would be one more quiet rewrite of a
        # value the reader is meant to be checking.
        if value is None or value == "":
            return f'<c r="{reference}"{styled}/>'
        if column_type == COLUMN_TYPE_DATETIME:
            serial = self._excel_serial_datetime(value)
            if serial is not None:
                return f'<c r="{reference}" s="{datetime_style}"><v>{serial}</v></c>'
        elif column_type == COLUMN_TYPE_NUMBER:
            number = self._excel_number_value(value)
            if number is not None:
                # A fractional number is drawn to two places so a column of them
                # lines up (downtime, which the query has already rounded to two);
                # a whole one keeps the general format, so an id reads 1042 rather
                # than 1042.00. The cell holds the full value either way -- the
                # format is what it is drawn as, not what it is.
                cell_style = f' s="{decimal_style}"' if isinstance(number, float) else styled
                return f'<c r="{reference}"{cell_style}><v>{number}</v></c>'
        elif column_type == COLUMN_TYPE_BOOLEAN:
            # Written as the words the Lookup Lists dropdown offers rather than as
            # a boolean cell. A boolean TRUE and the dropdown's "TRUE" are
            # different values to Excel: the column would sort in two blocks, and
            # every untouched row would be flagged by "circle invalid data"
            # against its own dropdown.
            flag = self._excel_optional_bool(value)
            if flag is not None:
                return f'<c r="{reference}"{styled} t="inlineStr"><is><t>{"TRUE" if flag else "FALSE"}</t></is></c>'
        elif column_type is None:
            # Untyped sheets (the lookup lists) keep the old pass-through.
            if isinstance(value, bool):
                return f'<c r="{reference}"{styled} t="b"><v>{1 if value else 0}</v></c>'
            if isinstance(value, int) or (isinstance(value, float) and math.isfinite(value)):
                return f'<c r="{reference}"{styled}><v>{value}</v></c>'
        return f'<c r="{reference}"{styled} t="inlineStr"><is>{self._xlsx_inline_text(self._xlsx_safe_text(value))}</is></c>'

    def _xlsx_sheet_xml(self, rows: list[list[Any]], *, validations: list[ExcelValidation] | None = None, column_types: dict[str, str] | None = None, editable_columns: frozenset[str] | None = None, auto_filter: bool = False) -> str:
        headers = list(rows[0]) if rows else []
        # Resolved from the header row rather than from the caller's column order:
        # the header names the column, so the types follow a column that moves.
        # Matched through the same normalisation the import reads headers with, so
        # the two halves of the round trip agree on what a column is called.
        normalized_types = {self._normalize_excel_header(name): value for name, value in (column_types or {}).items()}
        types_by_index = [normalized_types.get(self._normalize_excel_header(header)) for header in headers]
        # The same for which columns are the reader's to fill in. A sheet told
        # nothing about that (the lookup lists) keeps the plain bold header.
        normalized_editable = {self._normalize_excel_header(name) for name in editable_columns or ()}
        editable_by_index = [self._normalize_excel_header(header) in normalized_editable for header in headers]
        if editable_columns is None:
            header_styles = [EXCEL_STYLE_HEADER] * len(headers)
        else:
            header_styles = [EXCEL_STYLE_EDITABLE_HEADER if editable else EXCEL_STYLE_READ_ONLY_HEADER for editable in editable_by_index]
        xml_rows = []
        for row_index, row in enumerate(rows, start=1):
            is_header = row_index == 1
            cells = []
            for column_index, value in enumerate(row, start=1):
                reference = f"{self._xlsx_column_name(column_index)}{row_index}"
                in_headers = column_index <= len(headers)
                column_type = None if is_header else (types_by_index[column_index - 1] if in_headers else None)
                cells.append(
                    self._xlsx_cell_xml(
                        reference,
                        value,
                        column_type,
                        style=header_styles[column_index - 1] if is_header else EXCEL_STYLE_DEFAULT,
                        editable=not is_header and in_headers and editable_by_index[column_index - 1],
                    )
                )
            xml_rows.append(f'<row r="{row_index}">{"".join(cells)}</row>')
        column_xml = self._xlsx_columns(headers, rows, types_by_index)
        filter_xml = ""
        if auto_filter and headers:
            last_column = self._xlsx_column_name(len(headers))
            filter_xml = f'<autoFilter ref="A1:{last_column}{max(len(rows), 1)}"/>'
        validation_xml = self._xlsx_data_validations(headers, validations or [])
        # Order is fixed by the schema: cols, sheetData, autoFilter, dataValidations.
        return f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
<sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/></sheetView></sheetViews>{column_xml}
<sheetData>{"".join(xml_rows)}</sheetData>{filter_xml}{validation_xml}
</worksheet>'''

    def _xlsx_columns(self, headers: list[Any], rows: list[list[Any]], types_by_index: list[str | None]) -> str:
        """Column widths, so a sheet of default-width columns is not the first thing to fix.

        Measured off the header and the first few rows rather than the whole
        selection -- an asset with thousands of records would otherwise pay for a
        pass over every cell to size a column the same way. A date is measured as
        the sixteen characters it is drawn in, not as its serial number.
        """

        if not headers:
            return ""
        widths = []
        for column_index, header in enumerate(headers):
            longest = len(self._xlsx_safe_text(header))
            if types_by_index[column_index] == COLUMN_TYPE_DATETIME:
                # Its cells hold a serial number; what has to fit is the sixteen
                # characters the date format draws, or the header, whichever is wider.
                widths.append(float(max(18, longest + 2)))
                continue
            for row in rows[1:51]:
                if column_index < len(row) and row[column_index] is not None:
                    longest = max(longest, len(self._xlsx_safe_text(row[column_index])))
            widths.append(float(max(10, min(longest + 2, 60))))
        entries = "".join(
            f'<col min="{index}" max="{index}" width="{width}" customWidth="1"/>'
            for index, width in enumerate(widths, start=1)
        )
        return f"\n<cols>{entries}</cols>"

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

    def _xlsx_part_path(self, target: str) -> str:
        """A workbook relationship Target as the zip entry it names.

        Every writer spells the same sheet differently: Excel saves a relative
        "worksheets/sheet1.xml", while LibreOffice and Google Sheets save the
        package-absolute "/xl/worksheets/sheet1.xml". Reading the second as
        relative built "xl/xl/worksheets/sheet1.xml" and the upload died on a raw
        KeyError, so a workbook that had been through anything but Excel could not
        be dispositioned at all.
        """

        cleaned = str(target or "").lstrip("/")
        return cleaned if cleaned.startswith("xl/") else f"xl/{cleaned}"

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
        # \s does not cover every character XML refuses -- it catches \x0b and \x0c
        # and leaves \x07 -- so a name typed with one in it would reach the sheet
        # cleaned, come back as a different name, and be created a second time.
        # The two steps commute: the separators are spaces by the time this
        # collapses runs of them, whichever ran first.
        return re.sub(r"\s+", " ", self._without_unrepresentable_characters(str(text or "")).strip())

    @staticmethod
    def _without_unrepresentable_characters(text: str) -> str:
        """``text`` without the characters that cannot survive to a workbook.

        See ILLEGAL_XML_CHARACTERS and ILLEGAL_XML_WHITESPACE. Used on the way in
        as well as the way out: a value the database keeps but the workbook cannot
        carry makes an untouched round trip rewrite the record.

        Turning the separators into spaces rather than deleting them is also what
        lets this run either side of a whitespace collapse without changing the
        answer -- deleting them first left _normalize_taxonomy_text with nothing to
        collapse, and a name whose words had run together named nothing.
        """

        return ILLEGAL_XML_CHARACTERS.sub("", ILLEGAL_XML_WHITESPACE.sub(" ", text))

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
        # Cleaned on the way into the database rather than only on the way out to a
        # workbook. These two are editable in Excel, so the import reads them back
        # and compares them against what is stored: sanitising at the export alone
        # made an untouched workbook differ from its own source, and uploading it
        # saved a new disposition that dropped the character. Text the database
        # keeps is text every surface can carry.
        notes = self._without_unrepresentable_characters(str(disposition_text or "")).strip()
        rationale = self._without_unrepresentable_characters(str(pm_reset_rationale or "")).strip()
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
                mechanism_row = conn.execute(
                    "SELECT failure_mode_id FROM failure_mechanism WHERE failure_mechanism_id = ? AND is_active = 1",
                    (failure_mechanism_id,),
                ).fetchone()
                if mechanism_row is None:
                    raise ValueError("The selected failure mechanism does not exist or is inactive.")
                mechanism_mode_id = mechanism_row["failure_mode_id"]
                if (
                    failure_mode_id is not None
                    and mechanism_mode_id is not None
                    and int(mechanism_mode_id) != int(failure_mode_id)
                ):
                    raise ValueError(
                        "The selected failure mechanism belongs to a different failure mode. "
                        "Choose a mechanism under the selected failure mode."
                    )
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
        analysis_start: date | None = None,
        analysis_cutoff: date | None = None,
    ) -> AnalysisResultView:
        """Build a failure group's lives from current dispositions, fit them, and save the result.

        ``analysis_start`` and ``analysis_cutoff`` are dates on the plant's calendar:
        the start counts from that day's first minute and the cutoff to its last, so a
        window names whole days. Without a cutoff the current life is censored at the
        last completed Limble import when that is later than every event, and at the
        moment of the run when it is not -- never at a time the data has not reached.

        A group that cannot be fitted -- fewer than ``MIN_WEIBULL_FAILURE_LIVES`` lives
        ending in a failure, or a likelihood with no root -- raises WeibullFitError
        *after* committing: its events and lives are rebuilt and any result saved for
        it earlier is removed, because the data no longer supports it. Anything else
        that goes wrong rolls back and leaves the saved result as it was.
        """

        if grouping_level not in {"FAILURE_MODE", "FAILURE_MECHANISM"}:
            raise ValueError("Select a failure mode or failure mechanism before performing Weibull analysis.")
        if grouping_level == "FAILURE_MECHANISM" and failure_mechanism_id is None:
            raise ValueError("Failure-mechanism Weibull analysis requires a selected failure mechanism.")
        population_mechanism_id = failure_mechanism_id if grouping_level == "FAILURE_MECHANISM" else None
        refusal: str | None = None
        view: AnalysisResultView | None = None
        with self.write_connection() as conn:
            population_id = self._get_or_create_modeled_population(conn, asset_number, failure_mode_id, population_mechanism_id)
            population = conn.execute(
                "SELECT population_name FROM modeled_population WHERE modeled_population_id = ?",
                (population_id,),
            ).fetchone()
            analysis_label = population["population_name"] if population and population["population_name"] else f"{asset_number} failure group"
            had_saved_result = (
                conn.execute("SELECT 1 FROM analysis_dataset WHERE modeled_population_id = ? LIMIT 1", (population_id,)).fetchone()
                is not None
            )
            life_basis_id = self._life_basis_id(conn)
            schedule_class_id = self._schedule_class_id(conn, asset_number)
            zone, zone_name, zone_warning = self._plant_time_zone(conn)
            rows = self._population_event_rows(
                conn,
                asset_number,
                grouping_level=grouping_level,
                failure_mode_id=failure_mode_id,
                failure_mechanism_id=failure_mechanism_id,
            )
            start, cutoff, cutoff_source = self._resolve_analysis_window(conn, rows, zone, analysis_start, analysis_cutoff)
            event_counts = self._refresh_event_processing(
                conn,
                asset_number,
                population_id,
                rows=rows,
                grouping_level=grouping_level,
                analysis_start=start,
                analysis_cutoff=cutoff,
                cutoff_is_exclusive=cutoff_source == "USER",
                zone=zone,
            )
            observation_ids = self._refresh_observations(conn, asset_number, population_id, life_basis_id, schedule_class_id, cutoff, zone)
            data: list[tuple[float, int]] = []
            if observation_ids:
                data = [
                    (float(row["life_hours_for_weibull"]), int(row["failure_indicator"]))
                    for row in conn.execute(
                        f"""
                        SELECT life_hours_for_weibull, failure_indicator FROM weibull_observation
                        WHERE weibull_observation_id IN ({','.join('?' for _ in observation_ids)}) AND is_usable = 1
                        ORDER BY life_hours_for_weibull, weibull_observation_id
                        """,
                        observation_ids,
                    ).fetchall()
                    if row["life_hours_for_weibull"] and row["life_hours_for_weibull"] > 0
                ]
            failure_lives = sum(failed for _, failed in data)
            removed_note = (
                " The result saved earlier for this failure group has been removed, because these lives no longer support it."
                if had_saved_result
                else ""
            )
            if failure_lives < MIN_WEIBULL_FAILURE_LIVES:
                # What became of the failures that did not end a life: the first event
                # only starts the clock, and a zero-hour interval is left out.
                first_event = conn.execute(
                    """
                    SELECT is_failure_event FROM event_processing_record
                    WHERE modeled_population_id = ? AND event_role IN ('FAILURE_EVENT','PM_RESET_EVENT')
                    ORDER BY weibull_sequence_number LIMIT 1
                    """,
                    (population_id,),
                ).fetchone()
                event_counts["first_is_failure"] = int(bool(first_event and first_event[0]))
                event_counts["zero_hour_failures"] = int(
                    conn.execute(
                        """
                        SELECT COUNT(*) FROM event_processing_record
                        WHERE modeled_population_id = ? AND event_role = 'FAILURE_EVENT'
                          AND weibull_life_note LIKE 'Excluded - no scheduled hours%'
                        """,
                        (population_id,),
                    ).fetchone()[0]
                )
                refusal = self._too_few_failure_lives_message(failure_lives, event_counts) + removed_note
            else:
                try:
                    beta, eta, log_likelihood = self._fit_weibull_2p(data)
                except WeibullFitError as exc:
                    refusal = str(exc) + removed_note
            if refusal is None:
                beta_lo, beta_hi, eta_lo, eta_hi = self._weibull_confidence_intervals(data, beta, eta)
                mean_time_to_failure = eta * math.gamma(1 + 1 / beta)
                km_points = self._kaplan_meier_points(data)
                r_squared = self._probability_plot_r_squared(km_points)
                interpretation_summary = self._weibull_interpretation_summary(
                    beta, eta, mean_time_to_failure, beta_lo, beta_hi, eta_lo, eta_hi, r_squared, failure_lives
                )
                schedule = conn.execute(
                    "SELECT schedule_class_name, exclude_weekends FROM asset_schedule_class WHERE schedule_class_id = ?",
                    (schedule_class_id,),
                ).fetchone()
                dataset_id = conn.execute(
                    """
                    INSERT INTO analysis_dataset(modeled_population_id, asset_number, analysis_name, analysis_cutoff_datetime,
                        analysis_start_datetime, analysis_cutoff_source, schedule_class_id, schedule_time_zone,
                        schedule_time_zone_warning, life_basis_id, notes)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        population_id,
                        asset_number,
                        f"Weibull Analysis - {analysis_label}",
                        cutoff.isoformat(),
                        start.isoformat() if start is not None else None,
                        cutoff_source,
                        schedule_class_id,
                        zone_name,
                        zone_warning,
                        life_basis_id,
                        "Generated from current GREMLIN failure-mode/mechanism dispositions.",
                    ),
                ).lastrowid
                conn.executemany(
                    "INSERT INTO analysis_dataset_member(analysis_dataset_id, weibull_observation_id, included_in_fit) VALUES (?, ?, 1)",
                    [(dataset_id, observation_id) for observation_id in observation_ids],
                )
                run_id = conn.execute(
                    "INSERT INTO weibull_analysis_run(analysis_dataset_id, software_version, code_version, notes) VALUES (?, ?, ?, ?)",
                    (dataset_id, gremlin_code_version(), WEIBULL_METHOD_VERSION, "2P Weibull MLE with right-censored observations for selected failure group."),
                ).lastrowid
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
                censored = len(data) - failure_lives
                result_id = conn.execute(
                    """
                    INSERT INTO weibull_result(weibull_analysis_run_id, beta_mle, eta_mle, beta_lower_ci, beta_upper_ci, eta_lower_ci, eta_upper_ci,
                        log_likelihood, aic, bic, failure_count, censored_count, total_observation_count, mean_time_to_failure, b10_life, b50_life,
                        probability_plot_r_squared, fit_quality_notes, engineering_interpretation, recommended_action, limitations)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                        failure_lives,
                        censored,
                        len(data),
                        mean_time_to_failure,
                        eta * (-math.log(0.90)) ** (1 / beta),
                        eta * (math.log(2)) ** (1 / beta),
                        r_squared,
                        "2P Weibull MLE with right-censored observations; approximate 95% Fisher-matrix intervals; "
                        "probability-plot R² of the Kaplan-Meier failure points.",
                        json.dumps(interpretation_summary),
                        interpretation_summary[0]["recommendation"] if interpretation_summary else "Review the Weibull fit before selecting a maintenance strategy.",
                        self._weibull_limitations_text(
                            schedule["schedule_class_name"] if schedule else "the weekday schedule",
                            zone_name,
                            bool(schedule["exclude_weekends"]) if schedule else True,
                        ),
                    ),
                ).lastrowid
                view = self._load_weibull_view(conn, int(result_id))
        if refusal is not None:
            raise WeibullFitError(refusal)
        if view is None:
            raise ValueError("The Weibull result was saved but could not be read back. Run the analysis again.")
        return view

    @staticmethod
    def _too_few_failure_lives_message(failure_lives: int, event_counts: dict[str, int]) -> str:
        """Why a failure group cannot be fitted yet, in terms of what the user can change."""

        message = (
            f"Weibull analysis needs at least {MIN_WEIBULL_FAILURE_LIVES} lives that end in a failure, "
            f"and this failure group has {failure_lives}."
        )
        included = event_counts.get("included_failures", 0)
        if included > failure_lives:
            why = []
            if event_counts.get("first_is_failure"):
                why.append("the first only starts the clock")
            zero_hour = event_counts.get("zero_hour_failures", 0)
            if zero_hour:
                why.append(f"{zero_hour} closed a life with no scheduled hours, which is left out")
            message += f" It has {included} dated failures in the analysis window" + (f": {', and '.join(why)}." if why else ".")
        left_out = [
            (event_counts.get("missing_date", 0), "with no completed date"),
            (event_counts.get("unparseable_date", 0), "with a completed date that could not be read"),
            (event_counts.get("before_start", 0) + event_counts.get("after_cutoff", 0), "outside the analysis window"),
        ]
        reasons = [f"{count} {why}" for count, why in left_out if count]
        if reasons:
            message += f" Events left out of the timeline: {', '.join(reasons)}."
        message += (
            " Disposition more failures, widen the analysis window, or fit the failure mode the mechanism "
            "belongs to, with the reason a mechanism could not be fitted."
        )
        return message

    @staticmethod
    def _weibull_limitations_text(schedule_name: str, zone_name: str, exclude_weekends: bool = True) -> str:
        """The limitations saved with a result, so its report states what its hours are."""

        if exclude_weekends:
            counted = "weekends excluded"
            not_adjusted = "Holidays and shutdowns are not taken out, Saturday shifts are not added back"
        else:
            counted = "every clock hour counted"
            not_adjusted = "Holidays and shutdowns are not taken out"
        return (
            f"Life is schedule-adjusted elapsed time, an exposure proxy and not run-meter hours: {schedule_name}, "
            f"{counted}, days split at midnight {zone_name}. {not_adjusted}, the first event in the window only "
            "starts the clock, and the confidence intervals are approximate."
        )

    def load_saved_weibull_analysis(
        self,
        asset_number: str,
        *,
        grouping_level: str,
        failure_mode_id: int,
        failure_mechanism_id: int | None = None,
    ) -> AnalysisResultView | None:
        """Read back the latest saved Weibull fit for a failure group, without recomputing.

        :meth:`perform_weibull_analysis` is a write: it rebuilds event processing,
        observations, a dataset, a run, and a result. This is its read-only twin, so a
        viewer (or a signed-out visitor) can open the same Weibull view for any group an
        editor has already run. Returns ``None`` when the group has never been analyzed,
        which the caller reports as "no saved analysis yet" rather than an error.
        """

        if grouping_level not in {"FAILURE_MODE", "FAILURE_MECHANISM"}:
            raise ValueError("Select a failure mode or failure mechanism before viewing a Weibull analysis.")
        if grouping_level == "FAILURE_MECHANISM" and failure_mechanism_id is None:
            raise ValueError("Failure-mechanism Weibull analysis requires a selected failure mechanism.")
        population_mechanism_id = failure_mechanism_id if grouping_level == "FAILURE_MECHANISM" else None
        # One snapshot for every read below. An editor rerunning this same population
        # deletes its dataset, run, KM points, curve points and observations and inserts
        # replacements, so reading them in separate implicit transactions could pair the
        # old result row with the new run's (or no) graph and table data.
        with self.read_transaction() as conn:
            population = conn.execute(
                """
                SELECT modeled_population_id
                FROM modeled_population
                WHERE asset_number = ? AND failure_mode_id = ?
                  AND ((failure_mechanism_id IS NULL AND ? IS NULL) OR failure_mechanism_id = ?)
                ORDER BY modeled_population_id LIMIT 1
                """,
                (asset_number, failure_mode_id, population_mechanism_id, population_mechanism_id),
            ).fetchone()
            if population is None:
                return None
            # Newest run for this population wins, matching how the beta rankings pick
            # "the latest saved Weibull result" for the same populations.
            result = conn.execute(
                """
                SELECT wr.weibull_result_id
                FROM weibull_result wr
                JOIN weibull_analysis_run war ON war.weibull_analysis_run_id = wr.weibull_analysis_run_id
                JOIN analysis_dataset ad ON ad.analysis_dataset_id = war.analysis_dataset_id
                WHERE ad.asset_number = ? AND ad.modeled_population_id = ?
                ORDER BY war.run_datetime DESC, wr.weibull_result_id DESC
                LIMIT 1
                """,
                (asset_number, int(population["modeled_population_id"])),
            ).fetchone()
            if result is None:
                return None
            return self._load_weibull_view(conn, int(result["weibull_result_id"]))

    def _load_weibull_view(self, conn: sqlite3.Connection, result_id: int) -> AnalysisResultView | None:
        """One saved Weibull result as the page and the report show it, read through ``conn``.

        Everything is read back, nothing recomputed, so a fresh run, the read-back a
        viewer opens and the Word report describe the same fit identically. None when
        the result no longer exists (its group was run again since).
        """

        row = conn.execute(
            """
            SELECT wr.weibull_result_id, wr.weibull_analysis_run_id, wr.beta_mle, wr.eta_mle,
                   wr.beta_lower_ci, wr.beta_upper_ci, wr.eta_lower_ci, wr.eta_upper_ci,
                   wr.failure_count, wr.censored_count, wr.total_observation_count,
                   wr.mean_time_to_failure, wr.b10_life, wr.b50_life, wr.probability_plot_r_squared,
                   wr.engineering_interpretation,
                   war.run_datetime, war.software_version, war.code_version,
                   ad.analysis_dataset_id, ad.asset_number, ad.analysis_name, ad.analysis_cutoff_datetime,
                   ad.analysis_start_datetime, ad.analysis_cutoff_source, ad.schedule_class_id,
                   ad.schedule_time_zone, ad.schedule_time_zone_warning,
                   lb.life_basis_code, lb.life_basis_name,
                   mp.modeled_population_id, mp.population_name, mp.grouping_level_used, mp.fallback_rationale
            FROM weibull_result wr
            JOIN weibull_analysis_run war ON war.weibull_analysis_run_id = wr.weibull_analysis_run_id
            JOIN analysis_dataset ad ON ad.analysis_dataset_id = war.analysis_dataset_id
            LEFT JOIN life_basis lb ON lb.life_basis_id = ad.life_basis_id
            LEFT JOIN modeled_population mp ON mp.modeled_population_id = ad.modeled_population_id
            WHERE wr.weibull_result_id = ?
            """,
            (result_id,),
        ).fetchone()
        if row is None:
            return None
        run_id = int(row["weibull_analysis_run_id"])
        dataset_id = int(row["analysis_dataset_id"])
        km_points = [
            dict(point)
            for point in conn.execute(
                """
                SELECT ordered_index, life_hours, at_risk_count, failure_count_at_time,
                       censored_count_at_time, survival_estimate, cdf_estimate, reliability_estimate,
                       weibull_plot_x, weibull_plot_y
                FROM kaplan_meier_point
                WHERE weibull_analysis_run_id = ?
                ORDER BY ordered_index, kaplan_meier_point_id
                """,
                (run_id,),
            ).fetchall()
        ]
        curve_points = [
            dict(point)
            for point in conn.execute(
                """
                SELECT life_hours, cdf, reliability, pdf, hazard_rate
                FROM weibull_curve_point
                WHERE weibull_analysis_run_id = ?
                ORDER BY life_hours, weibull_curve_point_id
                """,
                (run_id,),
            ).fetchall()
        ]
        observations = [
            dict(observation)
            for observation in conn.execute(
                f"""
                {_WEIBULL_OBSERVATION_SELECT}
                JOIN analysis_dataset_member adm ON adm.weibull_observation_id = wo.weibull_observation_id
                WHERE adm.analysis_dataset_id = ? AND wo.is_usable = 1
                ORDER BY wo.life_hours_for_weibull, wo.weibull_observation_id
                """,
                (dataset_id,),
            ).fetchall()
        ]
        for index, observation in enumerate(observations, start=1):
            observation["ordered_index"] = index

        # Datasets saved before the schedule was recorded on them still have it on
        # each of their observations.
        schedule_class_id = row["schedule_class_id"]
        if schedule_class_id is None:
            first = conn.execute(
                """
                SELECT wo.schedule_class_id FROM weibull_observation wo
                JOIN analysis_dataset_member adm ON adm.weibull_observation_id = wo.weibull_observation_id
                WHERE adm.analysis_dataset_id = ? AND wo.schedule_class_id IS NOT NULL
                LIMIT 1
                """,
                (dataset_id,),
            ).fetchone()
            schedule_class_id = first["schedule_class_id"] if first else None
        schedule = (
            conn.execute(
                "SELECT schedule_class_code, schedule_class_name, hours_per_day, exclude_weekends FROM asset_schedule_class WHERE schedule_class_id = ?",
                (schedule_class_id,),
            ).fetchone()
            if schedule_class_id is not None
            else None
        )
        current_schedule_id = self._schedule_class_id(conn, row["asset_number"])
        schedule_current = schedule_class_id is None or int(schedule_class_id) == current_schedule_id
        current_schedule_name = conn.execute(
            "SELECT schedule_class_name FROM asset_schedule_class WHERE schedule_class_id = ?", (current_schedule_id,)
        ).fetchone()[0]
        life_basis = {
            "code": row["life_basis_code"],
            "name": row["life_basis_name"],
            "schedule_code": schedule["schedule_class_code"] if schedule else None,
            "schedule_name": schedule["schedule_class_name"] if schedule else None,
            "hours_per_day": schedule["hours_per_day"] if schedule else None,
            "exclude_weekends": bool(schedule["exclude_weekends"]) if schedule else None,
            # Runs before the zone was recorded split days at midnight UTC.
            "time_zone": row["schedule_time_zone"] or "UTC",
            "time_zone_warning": row["schedule_time_zone_warning"],
        }
        events: list[dict[str, Any]] = []
        if row["modeled_population_id"] is not None:
            events = [
                dict(event)
                for event in conn.execute(
                    """
                    SELECT ep.event_processing_id, ep.event_role, ep.is_failure_event, ep.is_pm_reset_event,
                           ep.weibull_sequence_number, ep.completed_date_raw, ep.completed_date_parsed,
                           ep.date_parse_status, ep.previous_same_population_date, ep.weibull_life_note,
                           ep.data_quality_assumption_flag,
                           m.mapped_record_id, m.task_id, m.task_name
                    FROM event_processing_record ep
                    LEFT JOIN mapped_cmms_record m ON m.mapped_record_id = ep.mapped_record_id
                    WHERE ep.asset_number = ? AND ep.modeled_population_id = ?
                    ORDER BY ep.completed_date_parsed IS NULL, ep.completed_date_parsed, ep.event_processing_id
                    """,
                    (row["asset_number"], int(row["modeled_population_id"])),
                ).fetchall()
            ]

        try:
            interpretation_summary = json.loads(row["engineering_interpretation"]) if row["engineering_interpretation"] else []
        except (ValueError, TypeError):
            interpretation_summary = []
        if not isinstance(interpretation_summary, list):
            interpretation_summary = []
        # Results saved before R² was stored still have the plot points it comes from.
        r_squared = row["probability_plot_r_squared"]
        if r_squared is None:
            r_squared = self._probability_plot_r_squared(km_points)
        saved_failures = int(row["failure_count"] or 0)
        r_squared_threshold = self.r_squared_review_threshold(saved_failures) if saved_failures else None
        if interpretation_summary and not any(
            isinstance(item, dict) and item.get("metric") == R_SQUARED_METRIC for item in interpretation_summary
        ):
            interpretation_summary.append(self._r_squared_interpretation_row(r_squared, saved_failures))

        analysis_name = str(row["analysis_name"] or "")
        label_prefix = "Weibull Analysis - "
        analysis_label = analysis_name[len(label_prefix):] if analysis_name.startswith(label_prefix) else analysis_name
        failure_count = int(row["failure_count"] or 0)
        method_version = row["code_version"]
        return AnalysisResultView(
            run_id=run_id,
            result_id=int(row["weibull_result_id"]),
            beta_mle=row["beta_mle"],
            eta_mle=row["eta_mle"],
            failure_count=failure_count,
            censored_count=int(row["censored_count"] or 0),
            total_observation_count=int(row["total_observation_count"] or 0),
            km_points=km_points,
            curve_points=curve_points,
            observations=observations,
            analysis_label=analysis_label or row["population_name"] or f"{row['asset_number']} failure group",
            grouping_level=row["grouping_level_used"] or "",
            beta_lower_ci=row["beta_lower_ci"],
            beta_upper_ci=row["beta_upper_ci"],
            eta_lower_ci=row["eta_lower_ci"],
            eta_upper_ci=row["eta_upper_ci"],
            mean_time_to_failure=row["mean_time_to_failure"],
            interpretation_summary=interpretation_summary,
            asset_number=str(row["asset_number"] or ""),
            b10_life=row["b10_life"],
            b50_life=row["b50_life"],
            probability_plot_r_squared=r_squared,
            probability_plot_r_squared_threshold=r_squared_threshold,
            probability_plot_review=bool(
                r_squared is not None and r_squared_threshold is not None and r_squared < r_squared_threshold
            ),
            analysis_start=row["analysis_start_datetime"],
            analysis_cutoff=row["analysis_cutoff_datetime"],
            analysis_cutoff_source=row["analysis_cutoff_source"],
            life_basis=life_basis,
            events=events,
            pm_reset_censored_count=sum(1 for obs in observations if obs["observation_type"] == "PM_RESET_CENSORED_LIFE"),
            current_life_censored_count=sum(1 for obs in observations if obs["observation_type"] == "RIGHT_CENSORED_LIFE"),
            run_datetime=row["run_datetime"],
            method_version=method_version,
            software_version=row["software_version"],
            method_current=method_version == WEIBULL_METHOD_VERSION,
            meets_minimum=failure_count >= MIN_WEIBULL_FAILURE_LIVES,
            min_failure_lives=MIN_WEIBULL_FAILURE_LIVES,
            fallback_rationale=row["fallback_rationale"],
            schedule_current=schedule_current,
            current_schedule_name=current_schedule_name,
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
        return int(conn.execute("SELECT life_basis_id FROM life_basis WHERE life_basis_code = 'SCHEDULE_ADJUSTED_ELAPSED_HOURS'").fetchone()[0])

    def _schedule_class_id(self, conn: sqlite3.Connection, asset_number: str) -> int:
        """The schedule an asset's life hours are counted on: its register entry, else the plant default."""

        row = conn.execute(
            "SELECT schedule_class_id FROM asset_schedule_assignment WHERE asset_number = ?",
            (str(asset_number).strip(),),
        ).fetchone()
        if row is not None:
            return int(row[0])
        return int(
            conn.execute(
                "SELECT schedule_class_id FROM asset_schedule_class WHERE schedule_class_code = ?",
                (PLANT_DEFAULT_SCHEDULE_CODE,),
            ).fetchone()[0]
        )

    def weibull_schedule_register(self) -> dict[str, Any]:
        """The Weibull schedule register for the Configuration page.

        The schedules on offer (the plant default first), every asset that is not on
        the default with the one it is on, and the most recent changes, newest first.
        """

        with self.connect() as conn:
            schedules = {
                row["schedule_class_code"]: {
                    "code": row["schedule_class_code"],
                    "name": row["schedule_class_name"],
                    "hours_per_day": row["hours_per_day"],
                    "exclude_weekends": bool(row["exclude_weekends"]),
                    "is_default": row["schedule_class_code"] == PLANT_DEFAULT_SCHEDULE_CODE,
                }
                for row in conn.execute(
                    "SELECT schedule_class_code, schedule_class_name, hours_per_day, exclude_weekends FROM asset_schedule_class"
                )
            }
            assignments = [
                dict(row)
                for row in conn.execute(
                    """
                    SELECT a.asset_number, c.schedule_class_code AS schedule_code, c.schedule_class_name AS schedule_name,
                           a.changed_by, a.changed_at,
                           (SELECT m.asset_name FROM mapped_cmms_record m
                            WHERE m.asset_number = a.asset_number AND m.asset_name IS NOT NULL LIMIT 1) AS asset_name
                    FROM asset_schedule_assignment a
                    JOIN asset_schedule_class c ON c.schedule_class_id = a.schedule_class_id
                    """
                )
            ]
            history = [
                dict(row)
                for row in conn.execute(
                    """
                    SELECT asset_number, from_schedule_class_code AS from_code, to_schedule_class_code AS to_code,
                           reason, changed_by, changed_at
                    FROM asset_schedule_change
                    ORDER BY changed_at DESC, asset_schedule_change_id DESC
                    LIMIT 50
                    """
                )
            ]
        assignments.sort(key=lambda row: self._natural_key(str(row["asset_number"])))
        names = {code: schedule["name"] for code, schedule in schedules.items()}
        for change in history:
            change["from_name"] = names.get(change["from_code"], change["from_code"])
            change["to_name"] = names.get(change["to_code"], change["to_code"])
        return {
            "default_code": PLANT_DEFAULT_SCHEDULE_CODE,
            "schedules": [schedules[code] for code in ASSIGNABLE_SCHEDULE_CODES if code in schedules],
            "assignments": assignments,
            "history": history,
        }

    def set_asset_weibull_schedule(
        self, asset_number: str, schedule_code: str, *, reason: str, changed_by: str | None = None
    ) -> dict[str, Any]:
        """Put an asset on a Weibull schedule, record why, and return the register.

        The plant default is not stored per asset: choosing it takes the asset off the
        register. Every change is kept with who made it, when and why, and a saved
        Weibull result counted on the asset's old schedule says it wants running again.
        """

        asset = str(asset_number or "").strip()
        code = str(schedule_code or "").strip()
        why = self._without_unrepresentable_characters(str(reason or "")).strip()
        if not asset:
            raise ValueError("Enter the Asset Number to change.")
        if code not in ASSIGNABLE_SCHEDULE_CODES:
            raise ValueError("Choose one of the listed schedules.")
        if not why:
            raise ValueError("Say why the schedule is changing; the change record keeps the reason.")
        with self.write_connection() as conn:
            if not conn.execute("SELECT 1 FROM mapped_cmms_record WHERE asset_number = ? LIMIT 1", (asset,)).fetchone():
                raise ValueError(f"GREMLIN has no Limble records for Asset Number {asset}.")
            current = conn.execute(
                """
                SELECT c.schedule_class_code FROM asset_schedule_assignment a
                JOIN asset_schedule_class c ON c.schedule_class_id = a.schedule_class_id
                WHERE a.asset_number = ?
                """,
                (asset,),
            ).fetchone()
            current_code = current[0] if current else PLANT_DEFAULT_SCHEDULE_CODE
            if current_code == code:
                raise ValueError(f"Asset {asset} is already on that schedule.")
            if code == PLANT_DEFAULT_SCHEDULE_CODE:
                conn.execute("DELETE FROM asset_schedule_assignment WHERE asset_number = ?", (asset,))
            else:
                class_id = conn.execute(
                    "SELECT schedule_class_id FROM asset_schedule_class WHERE schedule_class_code = ?", (code,)
                ).fetchone()[0]
                conn.execute(
                    """
                    INSERT INTO asset_schedule_assignment(asset_number, schedule_class_id, changed_by, changed_at)
                    VALUES (?, ?, ?, datetime('now'))
                    ON CONFLICT(asset_number) DO UPDATE SET
                        schedule_class_id = excluded.schedule_class_id,
                        changed_by = excluded.changed_by,
                        changed_at = excluded.changed_at
                    """,
                    (asset, class_id, changed_by),
                )
            conn.execute(
                """
                INSERT INTO asset_schedule_change(asset_number, from_schedule_class_code, to_schedule_class_code, reason, changed_by)
                VALUES (?, ?, ?, ?, ?)
                """,
                (asset, current_code, code, why, changed_by),
            )
        return self.weibull_schedule_register()

    @staticmethod
    def _scheduled_life_hours(
        start: datetime,
        end: datetime,
        hours_per_day: float,
        *,
        exclude_weekends: bool = True,
        tz: tzinfo | None = None,
    ) -> tuple[float, float, float]:
        """Return scheduled life hours plus excluded weekend and non-run hours.

        Schedule-adjusted Weibull life is based on weekday scheduled time. A full
        included weekday contributes ``hours_per_day`` hours; partial weekdays are
        prorated across the calendar day. Weekend time is excluded when requested.

        Days are split at midnight in ``tz`` -- the plant's time zone, because that is
        the clock its machines run by -- and default to UTC. Each segment's length is
        taken between UTC instants, so the day the clocks change is the 23 or 25 hours
        it really was rather than a wall-clock 24.
        """

        zone = tz or timezone.utc
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        if end.tzinfo is None:
            end = end.replace(tzinfo=timezone.utc)
        current = start.astimezone(timezone.utc)
        stop = end.astimezone(timezone.utc)
        if stop <= current:
            return 0.0, 0.0, 0.0
        hours_per_day = max(0.0, min(24.0, float(hours_per_day)))
        scheduled_hours = 0.0
        excluded_weekend_hours = 0.0
        excluded_non_run_hours = 0.0
        while current < stop:
            local = current.astimezone(zone)
            next_midnight = datetime.combine(local.date() + timedelta(days=1), time.min, tzinfo=zone).astimezone(timezone.utc)
            if next_midnight <= current:
                # Only a zone whose clocks skip midnight itself could land here; an
                # hour's step still moves the count on rather than looping forever.
                next_midnight = current + timedelta(hours=1)
            segment_end = min(stop, next_midnight)
            raw_hours = (segment_end - current).total_seconds() / 3600.0
            if exclude_weekends and local.weekday() >= 5:
                excluded_weekend_hours += raw_hours
            else:
                segment_scheduled = raw_hours * (hours_per_day / 24.0)
                scheduled_hours += segment_scheduled
                excluded_non_run_hours += max(0.0, raw_hours - segment_scheduled)
            current = segment_end
        return scheduled_hours, excluded_weekend_hours, excluded_non_run_hours

    def _plant_time_zone(self, conn: sqlite3.Connection) -> tuple[tzinfo, str, str | None]:
        """The zone life-hour days are split in: (zone, its name, why not the plant's if it fell back).

        The plant's zone is the one the Availability card judges months by, stored in
        ``availability_settings`` (America/Chicago until someone changes it). A zone
        that cannot be loaded falls back to UTC, and says so, rather than failing the
        analysis: hours that may be off by the zone's offset at each weekend are worth
        knowing about, not worth refusing to compute.
        """

        name = DEFAULT_TIMEZONE
        if self._table_exists(conn, "availability_settings") and self._column_exists(conn, "availability_settings", "timezone"):
            row = conn.execute("SELECT timezone FROM availability_settings WHERE id = 1").fetchone()
            if row is not None and row[0] and str(row[0]).strip():
                name = str(row[0]).strip()
        try:
            return ZoneInfo(name), name, None
        except (ZoneInfoNotFoundError, ValueError, KeyError):
            pass
        try:
            ZoneInfo("UTC")
        except Exception:
            reason = (
                f"the time zone database is unavailable, so '{name}' could not be loaded "
                "(install the 'tzdata' package: pip install -r requirements.txt)"
            )
        else:
            reason = f"'{name}' is not a recognised time zone (set a valid IANA name such as 'America/Chicago')"
        return timezone.utc, "UTC", f"Days were split at midnight UTC rather than plant time because {reason}."

    def _last_completed_import_at(self, conn: sqlite3.Connection) -> datetime | None:
        """When the last Limble import that finished successfully completed, if one is recorded."""

        if not self._table_exists(conn, "import_batch") or not self._column_exists(conn, "import_batch", "import_completed_at"):
            return None
        if self._column_exists(conn, "import_batch", "status"):
            rows = conn.execute(
                "SELECT import_completed_at FROM import_batch "
                "WHERE UPPER(TRIM(status)) = 'COMPLETED' AND import_completed_at IS NOT NULL"
            ).fetchall()
        else:
            rows = conn.execute("SELECT import_completed_at FROM import_batch WHERE import_completed_at IS NOT NULL").fetchall()
        latest = None
        for row in rows:
            completed = self._parse_datetime(row[0])
            if completed is not None and (latest is None or completed > latest):
                latest = completed
        return latest

    @staticmethod
    def _plant_midnight(day: date, zone: tzinfo) -> datetime:
        """The first instant of ``day`` on the plant's clock, in UTC."""

        return datetime.combine(day, time.min, tzinfo=zone).astimezone(timezone.utc)

    def _parse_event_datetime(self, value: Any, zone: tzinfo) -> datetime | None:
        """A completed date as the instant a life starts or ends, in UTC.

        A date with no time names a day on the plant's calendar, the one the
        analysis window and the life-hour days are counted in, so it is taken as
        the start of that plant day rather than midnight UTC, which on the plant's
        clock is the evening before. A value with a time is read as
        :meth:`_parse_datetime` reads it.
        """

        text = str(value).strip() if value is not None else ""
        for fmt in DATE_ONLY_FORMATS:
            try:
                day = datetime.strptime(text, fmt).date()
            except ValueError:
                continue
            return self._plant_midnight(day, zone)
        return self._parse_datetime(value)

    def _resolve_analysis_window(
        self,
        conn: sqlite3.Connection,
        rows: list[sqlite3.Row],
        zone: tzinfo,
        analysis_start: date | None,
        analysis_cutoff: date | None,
    ) -> tuple[datetime | None, datetime, str]:
        """The (start, cutoff, cutoff source) a run builds its lives in, all in UTC.

        A cutoff date runs to the end of that plant day, or to now if that is sooner.
        Without one, the cutoff is the last completed Limble import, provided it is
        later than every event: censoring at the moment of the run would credit the
        current life with hours the database has no news of yet, and a failure in
        them would be missed. Data newer than the last import means its record is no
        guide, and the moment of the run is used, as before.
        """

        now = datetime.now(timezone.utc).replace(microsecond=0)
        today = now.astimezone(zone).date()
        if analysis_start is not None and analysis_start > today:
            raise ValueError("The analysis start date can't be later than today.")
        if analysis_cutoff is not None:
            if analysis_cutoff > today:
                raise ValueError("The analysis cutoff date can't be later than today.")
            cutoff = min(now, self._plant_midnight(analysis_cutoff + timedelta(days=1), zone))
            source = "USER"
        else:
            latest_event = max(
                (when for when in (self._parse_event_datetime(row["completed_date_final"], zone) for row in rows) if when is not None),
                default=None,
            )
            last_import = self._last_completed_import_at(conn)
            if last_import is not None:
                last_import = last_import.replace(microsecond=0)
            if last_import is not None and last_import <= now and (latest_event is None or last_import >= latest_event):
                cutoff, source = last_import, "LAST_IMPORT"
            else:
                cutoff, source = now, "NOW"
        start = self._plant_midnight(analysis_start, zone) if analysis_start is not None else None
        if start is not None and start >= cutoff:
            raise ValueError("The analysis start date can't be later than the cutoff date.")
        return start, cutoff, source

    def _population_event_rows(
        self,
        conn: sqlite3.Connection,
        asset_number: str,
        *,
        grouping_level: str,
        failure_mode_id: int,
        failure_mechanism_id: int | None,
    ) -> list[sqlite3.Row]:
        """The current dispositions that put an event in this failure group's timeline, in date order.

        Each is dated by its completed date alone, the Weibull chronology field of
        REL-WBL-DAT-001. Unlike the trend and downtime analyses there is no falling back
        to the start or created date: a life restarts when the repair is finished, and a
        work order with no completion date has not restored anything yet. The order is
        by that date read as a date, not as text, with the ones that have none last.

        A PM reset restarts only what it restores. One aimed at a mechanism (a target
        mode and mechanism) restarts that mechanism's lives and nothing else; one aimed
        at the whole mode (a target mode, no mechanism) restarts the mode's lives and
        those of every mechanism under it.
        """

        if grouping_level == "FAILURE_MECHANISM":
            group_filter = """
                AND (
                    (d.disposition_category = 'INCLUDED_FAILURE'
                        AND d.failure_mode_id = :failure_mode_id
                        AND d.failure_mechanism_id = :failure_mechanism_id)
                    OR (d.disposition_category = 'INCLUDED_PM_RESET_EVENT'
                        AND d.reset_target_failure_mode_id = :failure_mode_id
                        AND (d.reset_target_failure_mechanism_id = :failure_mechanism_id
                             OR d.reset_target_failure_mechanism_id IS NULL))
                )
            """
        else:
            group_filter = """
                AND (
                    (d.disposition_category = 'INCLUDED_FAILURE' AND d.failure_mode_id = :failure_mode_id)
                    OR (d.disposition_category = 'INCLUDED_PM_RESET_EVENT'
                        AND d.reset_target_failure_mode_id = :failure_mode_id
                        AND d.reset_target_failure_mechanism_id IS NULL)
                )
            """
        return conn.execute(
            f"""
            SELECT m.*, d.event_disposition_id, d.disposition_category, d.failure_mode_id, d.failure_mechanism_id,
                   d.reset_target_failure_mode_id, d.reset_target_failure_mechanism_id
            FROM mapped_cmms_record m
            JOIN event_disposition d ON d.mapped_record_id = m.mapped_record_id AND d.is_current = 1
            WHERE m.asset_number = :asset_number
              AND d.include_in_event_processing = 1
              AND d.include_in_weibull_candidate = 1
              {group_filter}
            ORDER BY gremlin_sort_datetime(m.completed_date_final) IS NULL,
                     gremlin_sort_datetime(m.completed_date_final),
                     m.mapped_record_id
            """,
            {"asset_number": asset_number, "failure_mode_id": failure_mode_id, "failure_mechanism_id": failure_mechanism_id},
        ).fetchall()

    def _refresh_event_processing(
        self,
        conn: sqlite3.Connection,
        asset_number: str,
        population_id: int,
        *,
        rows: list[sqlite3.Row],
        grouping_level: str,
        analysis_start: datetime | None,
        analysis_cutoff: datetime,
        cutoff_is_exclusive: bool = False,
        zone: tzinfo = timezone.utc,
    ) -> dict[str, int]:
        """Rebuild REL-WBL-DAT-004's event processing table for a failure group.

        Every event the group's dispositions offer gets a row, including the ones left
        out of the timeline -- no completed date, a date that cannot be read, or a date
        outside the analysis window -- each with the DAT-004 §11 note that says why, so
        the fit's inputs can be traced back to every record behind them. Returns how
        many events landed where, for the message that explains a refused fit.

        An entered cutoff date ends at the next plant midnight, which belongs to the
        day after it, so ``cutoff_is_exclusive`` leaves out an event at that instant.
        The last import and the moment of the run are inclusive: the last import is
        only used as the cutoff when no event is later than it.
        """

        self._delete_population_weibull_artifacts(conn, population_id)
        conn.execute("DELETE FROM event_processing_record WHERE asset_number = ? AND modeled_population_id = ?", (asset_number, population_id))
        population_row = conn.execute(
            "SELECT population_name FROM modeled_population WHERE modeled_population_id = ?",
            (population_id,),
        ).fetchone()
        modeled_population_used = (
            population_row["population_name"] if population_row and population_row["population_name"] else f"Asset {asset_number}"
        )
        counts = {"included": 0, "included_failures": 0, "missing_date": 0, "unparseable_date": 0, "before_start": 0, "after_cutoff": 0}
        previous_id = None
        previous_date = None
        sequence = 0
        # Ordered as the dates are read here, a date with no time on the plant
        # calendar, which SQL's sort (midnight UTC) can put on the wrong side of a
        # timed event that evening. Ties and unreadable dates keep the query's order.
        parsed_rows = sorted(
            ((self._parse_event_datetime(row["completed_date_final"], zone), index, row) for index, row in enumerate(rows)),
            key=lambda item: (item[0] is None, item[0] or datetime.min.replace(tzinfo=timezone.utc), item[1]),
        )
        for parsed, _, row in parsed_rows:
            is_failure = row["disposition_category"] == "INCLUDED_FAILURE"
            is_pm_reset = row["disposition_category"] == "INCLUDED_PM_RESET_EVENT"
            event_failure_mode_id = row["failure_mode_id"] or row["reset_target_failure_mode_id"]
            event_failure_mechanism_id = row["failure_mechanism_id"] or row["reset_target_failure_mechanism_id"]
            if event_failure_mode_id is not None and not conn.execute("SELECT 1 FROM failure_mode WHERE failure_mode_id = ? AND is_active = 1", (event_failure_mode_id,)).fetchone():
                raise ValueError(f"A current disposition references deleted failure mode id {event_failure_mode_id}. Re-save the affected disposition before running Weibull analysis.")
            if event_failure_mechanism_id is not None and not conn.execute("SELECT 1 FROM failure_mechanism WHERE failure_mechanism_id = ? AND is_active = 1", (event_failure_mechanism_id,)).fetchone():
                if grouping_level == "FAILURE_MECHANISM":
                    raise ValueError(f"A current disposition references deleted failure mechanism id {event_failure_mechanism_id}. Re-save the affected disposition before running Weibull analysis.")
                event_failure_mechanism_id = None
            raw_date = row["completed_date_final"]
            record = {
                "row": row,
                "population_id": population_id,
                "asset_number": asset_number,
                "failure_mode_id": event_failure_mode_id,
                "failure_mechanism_id": event_failure_mechanism_id,
                "grouping_level": grouping_level,
                "modeled_population_used": modeled_population_used,
                "is_failure": is_failure,
                "is_pm_reset": is_pm_reset,
            }
            excluded_note = None
            if parsed is None:
                blank = not str(raw_date or "").strip()
                counts["missing_date" if blank else "unparseable_date"] += 1
                self._insert_event_processing_record(
                    conn,
                    record,
                    role="EXCLUDED_EVENT",
                    parsed=None,
                    parse_status="MISSING" if blank else "UNPARSEABLE",
                    note="Excluded - missing completed date" if blank else "Excluded - date parse issue",
                )
                continue
            if analysis_start is not None and parsed < analysis_start:
                counts["before_start"] += 1
                excluded_note = "Excluded - before analysis start date"
            elif (parsed >= analysis_cutoff) if cutoff_is_exclusive else (parsed > analysis_cutoff):
                counts["after_cutoff"] += 1
                excluded_note = "Excluded - after analysis cutoff"
            if excluded_note is not None:
                self._insert_event_processing_record(conn, record, role="EXCLUDED_EVENT", parsed=parsed, parse_status="PARSED", note=excluded_note)
                continue
            sequence += 1
            if previous_id is None:
                note = (
                    "Initial occurrence - no prior comparable start point available"
                    if is_failure
                    else "PM reset - starts the first life; no prior comparable start point available"
                )
            elif is_failure:
                note = "Failure - ends the life from the previous event and starts the next"
            else:
                note = "PM reset - censors the running life and starts the next"
            event_id = self._insert_event_processing_record(
                conn,
                record,
                role="FAILURE_EVENT" if is_failure else "PM_RESET_EVENT" if is_pm_reset else "TRACEABILITY_ONLY",
                parsed=parsed,
                parse_status="PARSED",
                note=note,
                sequence=sequence,
                previous_id=previous_id,
                previous_date=previous_date,
                valid_start=is_failure or is_pm_reset,
                # The first event opens the first life and closes none.
                valid_end=is_failure and previous_id is not None,
            )
            counts["included"] += 1
            counts["included_failures"] += int(is_failure)
            previous_id = event_id
            previous_date = parsed.isoformat()
        return counts

    @staticmethod
    def _insert_event_processing_record(
        conn: sqlite3.Connection,
        record: dict[str, Any],
        *,
        role: str,
        parsed: datetime | None,
        parse_status: str,
        note: str,
        sequence: int | None = None,
        previous_id: int | None = None,
        previous_date: str | None = None,
        valid_start: bool = False,
        valid_end: bool = False,
    ) -> int:
        row = record["row"]
        return int(
            conn.execute(
                """
                INSERT INTO event_processing_record(mapped_record_id, event_disposition_id, modeled_population_id, asset_number,
                    asset_name, event_role, completed_date_raw, completed_date_parsed, date_parse_status, failure_mode_id,
                    failure_mechanism_id, grouping_level_used, modeled_population_used, weibull_sequence_number,
                    previous_same_population_event_id, previous_same_population_date, is_failure_event, is_pm_reset_event,
                    is_valid_life_start, is_valid_life_end, weibull_life_note)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row["mapped_record_id"],
                    row["event_disposition_id"],
                    record["population_id"],
                    record["asset_number"],
                    row["asset_name"],
                    role,
                    row["completed_date_final"],
                    parsed.isoformat() if parsed is not None else None,
                    parse_status,
                    record["failure_mode_id"],
                    record["failure_mechanism_id"],
                    record["grouping_level"],
                    record["modeled_population_used"],
                    sequence,
                    previous_id,
                    previous_date,
                    int(record["is_failure"]),
                    int(record["is_pm_reset"]),
                    int(valid_start),
                    int(valid_end),
                    note,
                ),
            ).lastrowid
        )

    @staticmethod
    def _duplicate_check_flag(raw_hours: float) -> str | None:
        """The DAT-004 §12 duplicate-review flag for a life this short, or None."""

        if raw_hours < DUPLICATE_CHECK_RAW_HOURS:
            return f"Check for duplicate - completed within {DUPLICATE_CHECK_RAW_HOURS:g} hour of the event before it"
        return None

    def _refresh_observations(
        self,
        conn: sqlite3.Connection,
        asset_number: str,
        population_id: int,
        life_basis_id: int,
        schedule_class_id: int,
        cutoff: datetime,
        zone: tzinfo | None = None,
    ) -> list[int]:
        """Turn the event processing table into the lives the fit is run on.

        Each life runs from one event to the next, and its note is DAT-004 §11's: a
        complete life from a prior failure or from a PM reset, an interval censored at
        a PM reset, or the current life censored at the cutoff. An interval with no
        scheduled hours in it gets no life; its closing event says so and still starts
        the next one.
        """

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
        schedule = conn.execute(
            "SELECT hours_per_day, exclude_weekends FROM asset_schedule_class WHERE schedule_class_id = ?",
            (schedule_class_id,),
        ).fetchone()
        if schedule is None:
            hours_per_day, exclude_weekends = DEFAULT_WEEKDAY_SCHEDULE_HOURS_PER_DAY, True
        else:
            # A schedule with no hours set (raw elapsed) counts every clock hour.
            hours_per_day = float(schedule["hours_per_day"]) if schedule["hours_per_day"] is not None else 24.0
            exclude_weekends = bool(schedule["exclude_weekends"])
        cutoff_text = cutoff.isoformat()
        ids: list[int] = []
        previous_event = None
        previous_date = None
        for event in events:
            event_date = self._parse_datetime(event["completed_date_parsed"])
            if not event_date:
                continue
            if previous_date is not None and previous_event is not None:
                raw_hours = (event_date - previous_date).total_seconds() / 3600.0
                scheduled_hours, excluded_weekend_hours, excluded_non_run_hours = self._scheduled_life_hours(
                    previous_date,
                    event_date,
                    hours_per_day,
                    exclude_weekends=exclude_weekends,
                    tz=zone,
                )
                duplicate_flag = self._duplicate_check_flag(raw_hours)
                if scheduled_hours > 0:
                    if event["is_failure_event"]:
                        observation_type = "COMPLETED_FAILURE_LIFE"
                        note = (
                            "Valid completed life from PM reset event"
                            if previous_event["is_pm_reset_event"]
                            else "Valid completed life from prior same-population event"
                        )
                    else:
                        observation_type = "PM_RESET_CENSORED_LIFE"
                        note = "Censored interval ended by PM reset event"
                    obs_id = conn.execute(
                        """
                        INSERT INTO weibull_observation(modeled_population_id, asset_number, start_event_processing_id,
                            end_event_processing_id, observation_type, censoring_type, start_datetime, end_datetime,
                            analysis_cutoff_datetime, life_basis_id, schedule_class_id, life_hours_raw_elapsed,
                            excluded_weekend_hours, excluded_schedule_non_run_hours, life_hours_for_weibull,
                            failure_indicator, is_right_censored, is_usable, weibull_life_note, data_quality_assumption_flag)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
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
                            cutoff_text,
                            life_basis_id,
                            schedule_class_id,
                            raw_hours,
                            excluded_weekend_hours,
                            excluded_non_run_hours,
                            scheduled_hours,
                            int(event["is_failure_event"]),
                            int(not event["is_failure_event"]),
                            note,
                            duplicate_flag,
                        ),
                    ).lastrowid
                    ids.append(int(obs_id))
                    if duplicate_flag:
                        conn.execute(
                            "UPDATE event_processing_record SET data_quality_assumption_flag = ? WHERE event_processing_id = ?",
                            (duplicate_flag, event["event_processing_id"]),
                        )
                else:
                    conn.execute(
                        """
                        UPDATE event_processing_record
                        SET weibull_life_note = ?, is_valid_life_end = 0, data_quality_assumption_flag = ?
                        WHERE event_processing_id = ?
                        """,
                        (
                            "Excluded - no scheduled hours since the previous event; still starts the next life",
                            duplicate_flag,
                            event["event_processing_id"],
                        ),
                    )
            previous_event = event
            previous_date = event_date
        if previous_date is not None and previous_event is not None:
            raw_hours = (cutoff - previous_date).total_seconds() / 3600.0
            scheduled_hours, excluded_weekend_hours, excluded_non_run_hours = self._scheduled_life_hours(
                previous_date,
                cutoff,
                hours_per_day,
                exclude_weekends=exclude_weekends,
                tz=zone,
            )
            if scheduled_hours > 0:
                note = (
                    "PM reset censor to analysis cutoff date"
                    if previous_event["is_pm_reset_event"]
                    else "Censored interval to analysis cutoff date"
                )
                obs_id = conn.execute(
                    """
                    INSERT INTO weibull_observation(modeled_population_id, asset_number, start_event_processing_id,
                        observation_type, censoring_type, start_datetime, analysis_cutoff_datetime, life_basis_id,
                        schedule_class_id, life_hours_raw_elapsed, excluded_weekend_hours,
                        excluded_schedule_non_run_hours, life_hours_for_weibull, failure_indicator,
                        is_right_censored, is_usable, weibull_life_note)
                    VALUES (?, ?, ?, 'RIGHT_CENSORED_LIFE', 'RIGHT', ?, ?, ?, ?, ?, ?, ?, ?, 0, 1, 1, ?)
                    """,
                    (population_id, asset_number, previous_event["event_processing_id"], previous_date.isoformat(), cutoff_text, life_basis_id, schedule_class_id, raw_hours, excluded_weekend_hours, excluded_non_run_hours, scheduled_hours, note),
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

    def _datetime_sort_key(self, value: Any) -> str | None:
        """A stored CMMS date as a fixed-width UTC string that sorts chronologically.

        Registered on every connection as ``gremlin_sort_datetime`` (see
        ``connect``) so ORDER BY can treat a TEXT date column as a date. The
        return is padded to a fixed width, which is what lets plain text
        comparison stand in for a date comparison; a value that is not a date at
        all (and a blank one) returns NULL so it can be sorted to the end rather
        than landing in the middle of the run.

        A date that arrived without a clock time keeps midnight, so it sorts
        ahead of that same day's timed records rather than after them.
        """

        parsed = self._parse_datetime(value)
        if parsed is None:
            return None
        return parsed.strftime("%Y-%m-%d %H:%M:%S")

    def _fit_weibull_2p(self, data: list[tuple[float, int]]) -> tuple[float, float, float]:
        """The maximum-likelihood beta and eta for ``data``, and the log-likelihood there.

        Beta is the root of the profile score (equation W8 on the Standards page),
        bracketed on a grid over 0.1 to 20 and then bisected. With no root in that
        range there is no maximum-likelihood beta to report, so this raises rather
        than substitute an estimate of another kind under the MLE's name (REL-WBL-MTH-001
        §5.5 makes beta and eta MLEs). That happens when the lives are all but identical
        -- often duplicate work orders -- or too few to locate a peak.
        """

        failures = [t for t, failed in data if failed]
        all_times = [t for t, _ in data]
        d = len(failures)
        if d == 0:
            raise WeibullFitError("Cannot fit Weibull without failures.")
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
            raise WeibullFitError(
                "The maximum-likelihood fit did not converge: no beta between 0.1 and 20 maximises the "
                "likelihood of these lives. That usually means the lives are nearly identical (check the "
                "data table for duplicate work orders) or too few to fit, so no Weibull result was saved."
            )
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
        r_squared: float | None = None,
        failure_count: int = MIN_WEIBULL_FAILURE_LIVES,
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
        rows.append(self._r_squared_interpretation_row(r_squared, failure_count))
        return rows

    @classmethod
    def _r_squared_interpretation_row(cls, r_squared: float | None, failure_count: int) -> dict[str, str]:
        """The interpretation row for the probability-plot R² (REL-WBL-REQ-001 VV-070, VV-074)."""

        if r_squared is None:
            return {
                "metric": R_SQUARED_METRIC,
                "value": "Not available",
                "recommendation": "There are fewer than three distinct failure points on the probability plot, so how straight they lie cannot be judged. Rely on engineering review of the records behind the fit.",
            }
        threshold = cls.r_squared_review_threshold(failure_count)
        if r_squared < threshold:
            recommendation = (
                f"Below {threshold:.3f}, the R² that 90% of genuine Weibull samples with {failure_count} failures reach: "
                "the plotted failure points are less straight than chance alone explains. Review the population before "
                "acting on beta. Mixed mechanisms, a life missing its real start point, or a duplicate work order are the "
                "usual causes."
            )
        else:
            recommendation = (
                f"At or above {threshold:.3f}, the R² that 90% of genuine Weibull samples with {failure_count} failures "
                "reach, so the plot gives no sign of a mixed population. R² checks the model rather than being part of the "
                "fit; still read it with the plot, where a bend or two different slopes matters even when R² passes."
            )
        return {"metric": R_SQUARED_METRIC, "value": f"{r_squared:.3f}", "recommendation": recommendation}

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
        if beta_hi - beta_lo <= BETA_INTERVAL_STABLE_FRACTION * beta:
            return "The interval is no wider than 70% of beta, which means the beta interpretation is stable and defensible. You can place more confidence in the recommended maintenance direction, while still confirming that the grouping makes physical sense."
        return "The interval is wider than 70% of beta, so the beta interpretation should be treated with caution. Review whether this bucket mixes different mechanisms, whether data quality is weak, or whether there are still too few failures (about 20 are usually needed) to confidently choose a maintenance strategy."

    def _eta_ci_recommendation(self, eta_lo: float, eta_hi: float, eta: float) -> str:
        if eta <= 0:
            return "Do not use eta for decisions until the underlying fit and data are reviewed."
        if (eta_hi - eta_lo) / eta <= ETA_INTERVAL_STABLE_FRACTION:
            return "The interval is reasonably tight, so eta is stable enough to use for planning comparisons, maintenance timing discussions, and communication with stakeholders."
        return "The interval is wide, so avoid pretending there is a precise intervention point. Use eta as directional guidance only, and consider tightening the population or collecting more data before converting it into a hard decision."

    @staticmethod
    def _kaplan_meier_points(data: list[tuple[float, int]]) -> list[dict[str, Any]]:
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

    @staticmethod
    def _probability_plot_r_squared(km_points: list[dict[str, Any]]) -> float | None:
        """How straight the probability plot's points lie: their squared correlation.

        The points are the Kaplan-Meier failure points in Weibull coordinates, x = ln t
        and y = ln(-ln R), exactly the ones the probability plot draws (REL-WBL-MTH-001
        §5.6, §6). Their squared correlation is also the R² of the least-squares line
        through them, the standard probability-plot R². It checks whether the data look
        like one Weibull population; it is not the fit, so it does not depend on beta and
        eta and adjusting them leaves it alone (REL-WBL-REQ-001 VV-070). None with fewer
        than three points, since two always lie on a line.
        """

        points = [
            (float(point["weibull_plot_x"]), float(point["weibull_plot_y"]))
            for point in km_points
            if point.get("weibull_plot_x") is not None and point.get("weibull_plot_y") is not None
        ]
        if len(points) < 3:
            return None
        mean_x = sum(x for x, _ in points) / len(points)
        mean_y = sum(y for _, y in points) / len(points)
        sxx = sum((x - mean_x) ** 2 for x, _ in points)
        syy = sum((y - mean_y) ** 2 for _, y in points)
        sxy = sum((x - mean_x) * (y - mean_y) for x, y in points)
        if sxx <= 0 or syy <= 0:
            return None
        return min(1.0, (sxy * sxy) / (sxx * syy))

    @staticmethod
    def _report_r_squared_text(result: dict[str, Any]) -> str:
        """The report's R² cell: the value and how it compares with the review threshold."""

        r_squared = result.get("probability_plot_r_squared")
        if r_squared is None:
            return "Not available (fewer than three distinct failure points)"
        threshold = result.get("probability_plot_r_squared_threshold")
        text = f"{float(r_squared):.3f}: how straight the plotted failure points lie, a check on the model rather than the fit."
        if threshold is None:
            return text
        failures = result.get("failure_count")
        if result.get("probability_plot_review"):
            return (
                f"{text} Below {float(threshold):.3f}, the review threshold for {failures} failures: review the "
                "population before acting on beta."
            )
        return f"{text} Meets {float(threshold):.3f}, the review threshold for {failures} failures."

    @staticmethod
    def r_squared_review_threshold(failure_count: int) -> float:
        """The R² a fit with this many failure lives is flagged for review below.

        From R_SQUARED_REVIEW_THRESHOLDS, interpolated between its rows; counts past
        either end take that end's value.
        """

        table = R_SQUARED_REVIEW_THRESHOLDS
        if failure_count <= table[0][0]:
            return table[0][1]
        for (low_n, low_r2), (high_n, high_r2) in zip(table, table[1:]):
            if failure_count <= high_n:
                return low_r2 + (high_r2 - low_r2) * (failure_count - low_n) / (high_n - low_n)
        return table[-1][1]

    @staticmethod
    def simulated_r_squared_threshold(failures: int, *, samples: int, seed: int) -> float:
        """The R² that 90% of genuine Weibull samples with ``failures`` failures reach.

        How R_SQUARED_REVIEW_THRESHOLDS was made: ``samples`` samples of ``failures``
        failure lives plus one current life cut off at a random point along its own
        life, all from one Weibull, plotted as the probability plot is (Kaplan-Meier
        points in Weibull coordinates); the 10th percentile of their R². Drawn with
        beta = eta = 1, which loses nothing: x = ln t is a straight-line function of
        the standard variate for any beta and eta, and y depends only on the order.
        """

        rng = random.Random(seed)
        values = []
        for _ in range(samples):
            lives = [(rng.expovariate(1.0), 1) for _ in range(failures)]
            lives.append((rng.random() * rng.expovariate(1.0), 0))
            r_squared = LifeDataService._probability_plot_r_squared(LifeDataService._kaplan_meier_points(lives))
            if r_squared is not None:
                values.append(r_squared)
        values.sort()
        return values[int(0.10 * len(values))]

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
