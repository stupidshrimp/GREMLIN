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
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone, tzinfo
from pathlib import Path
from contextlib import contextmanager
from typing import Any, Iterator
from xml.sax.saxutils import escape
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
import xml.etree.ElementTree as ET

from services.availability_config import DEFAULT_TIMEZONE
from services.standard_disposition_schema import ensure_standard_disposition_schema
from services.wo_narrative import NARRATIVE_KEYS, extract_narrative

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
    """The lives on hand cannot be fitted: too few failures, or no likelihood root.

    ``result_removed`` says whether the refusal also removed the result saved for
    the group, so the page knows whether a fit of it still on screen is gone.
    """

    def __init__(self, message: str, *, result_removed: bool = False) -> None:
        super().__init__(message)
        self.result_removed = result_removed

# The one and only location for GREMLIN.db. GREMLIN no longer probes mapped
# drive letters, UNC shares, or user folders for the database; it opens this
# single path. Override with the GREMLIN_DB_PATH environment variable to point
# at a different file.
DEFAULT_DB_PATH = Path(r"C:\GREMLIN\GREMLIN.db")
DB_WRITE_TIMEOUT_SECONDS = 30
_DEFAULT_DB_PATH_SENTINEL = object()
_LOCK_WAIT_CONTEXT = threading.local()

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

class _PlantDays:
    """Every plant day's real length, as running totals of weekday and weekend seconds.

    A day runs from one midnight on the plant's clock to the next, taken between
    UTC instants, so the day the clocks change is the 23 or 25 hours it was. The
    totals let the days wholly inside a life be summed in one subtraction rather
    than walked. Built for a span of days and widened when a life falls outside it.
    """

    _MARGIN_DAYS = 400

    def __init__(self, zone: tzinfo) -> None:
        self.zone = zone
        self.first = None  # type: date | None
        self.midnights: list[datetime] = []
        self.weekday: list[int] = [0]
        self.weekend: list[int] = [0]
        self.sound = True
        # Widening replaces the lists while another request may be reading them.
        self.lock = threading.Lock()

    def _midnight(self, day: date) -> datetime:
        return datetime.combine(day, time.min, tzinfo=self.zone).astimezone(timezone.utc)

    def _cover(self, low: date, high: date) -> None:
        if self.first is not None and low >= self.first and (high - self.first).days + 1 < len(self.midnights):
            return
        first = low - timedelta(days=self._MARGIN_DAYS)
        last = high + timedelta(days=self._MARGIN_DAYS)
        if self.first is not None:
            first = min(first, self.first)
            last = max(last, self.first + timedelta(days=len(self.midnights) - 1))
        count = (last - first).days + 1
        midnights = [self._midnight(first + timedelta(days=offset)) for offset in range(count)]
        weekday, weekend = [0], [0]
        for offset in range(count - 1):
            day = first + timedelta(days=offset)
            seconds = int(round((midnights[offset + 1] - midnights[offset]).total_seconds()))
            # A midnight that is not the day's first instant on the plant's clock,
            # or a day of no length, is a zone the walk handles and the totals do not.
            if seconds <= 0 or midnights[offset].astimezone(self.zone).date() != day:
                self.sound = False
            is_weekend = day.weekday() >= 5
            weekday.append(weekday[-1] + (0 if is_weekend else seconds))
            weekend.append(weekend[-1] + (seconds if is_weekend else 0))
        self.first, self.midnights, self.weekday, self.weekend = first, midnights, weekday, weekend

    def full_days(self, first_day: date, last_day: date, start: datetime) -> tuple[int, int, datetime, datetime] | None:
        """Weekday and weekend seconds of the days after ``first_day`` and before ``last_day``,
        with the midnights that end the first and begin the last; None to walk instead."""

        if last_day < first_day:
            return None
        with self.lock:
            self._cover(first_day, last_day)
            if not self.sound:
                return None
            low = (first_day - self.first).days + 1
            high = (last_day - self.first).days
            first_midnight = self.midnights[low]
            if first_midnight <= start:
                return None
            if high < low:
                return 0, 0, first_midnight, first_midnight
            return (
                self.weekday[high] - self.weekday[low],
                self.weekend[high] - self.weekend[low],
                first_midnight,
                self.midnights[high],
            )


_PLANT_DAYS: dict[Any, _PlantDays] = {}
_PLANT_DAYS_LOCK = threading.Lock()


def _plant_days(zone: tzinfo) -> _PlantDays | None:
    """The running day totals for one zone, shared by every request; None for a zone that cannot key them."""

    try:
        key = (type(zone), getattr(zone, "key", None) or str(zone))
    except Exception:  # noqa: BLE001 - an odd tzinfo is walked rather than refused
        return None
    with _PLANT_DAYS_LOCK:
        days = _PLANT_DAYS.get(key)
        if days is None:
            days = _PLANT_DAYS[key] = _PlantDays(zone)
        return days


# What a workbook column holds. Every value reaches a sheet as text -- SQLite has
# no date type, and the CMMS hands over task ids as strings -- so a column that is
# not told its real type is validated and compared as text.
COLUMN_TYPE_TEXT = "text"
COLUMN_TYPE_NUMBER = "number"
COLUMN_TYPE_DATETIME = "datetime"
COLUMN_TYPE_BOOLEAN = "boolean"

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

    def change_token(self) -> tuple[Any, ...] | None:
        """A value that changes whenever a write to GREMLIN.db is committed, by any process.

        SQLite counts every committed write in the file's header -- the file change
        counter, bytes 24-27, which the rollback-journal mode every write here runs
        in keeps -- and the file's size and modification time sit beside it, as
        does the WAL file's should the database ever be switched to one. Two reads
        that return the same token read the same database, so what was worked out
        from it can be kept until the token moves.

        Read straight from the file rather than through SQLite, so it costs one
        small read and holds nothing open. Read it before a transaction's first
        query: what that transaction then reads is at least as new as the token it
        is kept under. None when the file cannot be read, and then nothing is kept.
        """

        try:
            with open(self.db_path, "rb") as handle:
                header = handle.read(28)
                stat = os.fstat(handle.fileno())
        except OSError:
            return None
        if len(header) < 28:
            return None
        try:
            wal = os.stat(f"{self.db_path}-wal")
            wal_state = (wal.st_size, wal.st_mtime_ns)
        except OSError:
            wal_state = None
        return (str(self.db_path), stat.st_ino, stat.st_size, stat.st_mtime_ns, header[24:28], wal_state)

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
                -- The asset tree's read and a population's: both name an Asset Number as
                -- TRIM(asset_number), which the plain index above cannot answer, and the
                -- tree reads these four columns of every work order, which this index
                -- holds without touching the (wide) rows.
                CREATE INDEX IF NOT EXISTS idx_mapped_cmms_asset_tree
                    ON mapped_cmms_record(TRIM(asset_number), asset_id_raw, asset_name, immediate_parent_asset_id);
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
            # The work-order disposition standard's tables, after the REL ones it
            # migrates from (services/standard_disposition_schema.py).
            ensure_standard_disposition_schema(conn)

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

    # ---- Weibull report (Word .docx) -----------------------------------------
    @staticmethod
    def _safe_asset_token(asset_number: str) -> str:
        token = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in str(asset_number)).strip("_")
        return token or "asset"

    def next_weibull_report_number(
        self,
        asset_number: str,
        *,
        analysis_label: str = "",
        weibull_result_id: int | None = None,
        population_weibull_analysis_id: int | None = None,
    ) -> tuple[str, int]:
        """Reserve the next ``REL-WBL-RPT-<asset>-00x`` report number for an asset.

        The sequence increments per asset so repeat reports get -001, -002, … The
        reservation is recorded in ``weibull_report_log`` so numbers are never reused,
        with the run it reports.
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
                INSERT INTO weibull_report_log(asset_number, report_number, sequence_number, analysis_label, weibull_result_id,
                    population_weibull_analysis_id)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (asset_number, report_number, sequence_number, analysis_label or None, weibull_result_id,
                 population_weibull_analysis_id),
            )
        return report_number, sequence_number

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
        population: dict[str, Any] | None = None,
    ) -> str:
        """Build the ``word/document.xml`` body XML for a Weibull report.

        Carries what REL-WBL-MTH-001 §10 asks of an analysis package -- the modeled
        population and grouping level, the life basis and censoring, the fitted values
        and reliability outputs, the recommendation, and the limitations -- plus the lives
        themselves, so the report can be checked without GREMLIN to hand.

        ``population`` is a renewal-stream run of the disposition standard: the report
        then states its population definition string, the streams it pooled and its
        validity warnings in place of a grouping level.
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
        if population is not None:
            parts.append(self._docx_paragraph(f"Population definition: {population.get('population_definition') or 'Not recorded'}"))
            streams = population.get("streams") or []
            parts.append(self._docx_paragraph(
                "Renewal streams pooled: "
                + ("; ".join(
                    f"{stream.get('location')} · {stream.get('component_name') or stream.get('component')} "
                    f"({stream.get('failures', 0)} failures, {stream.get('suspensions', 0)} suspensions)"
                    for stream in streams
                ) or "None")
            ))
        if population is None:
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
                f"{total} in total: {failures} end in a failure, {censored} suspended "
                + (
                    f"({result.get('suspension_count', 0)} renewed by other work, "
                    f"{result.get('current_life_censored_count', 0)} still in service at the cutoff)"
                    if population is not None
                    else f"({result.get('pm_reset_censored_count', 0)} at a PM reset, {result.get('current_life_censored_count', 0)} current life)"
                ),
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

        if population is not None:
            sensitivity = population.get("sensitivity_fit") or {}
            if sensitivity.get("fitted"):
                parts.append(self._docx_paragraph(
                    f"Sensitivity: with failures of unknown mechanism counted as failures rather than suspensions, "
                    f"beta = {fmt(sensitivity.get('beta'))} and eta = {fmt(sensitivity.get('eta'), ' hours')} "
                    f"({sensitivity.get('failure_count')} failures)."
                ))
            warnings = population.get("warnings") or []
            if warnings:
                parts.append(self._docx_heading("Validity Warnings", level=2))
                for warning in warnings:
                    parts.append(self._docx_paragraph(f"• {warning.get('title')}: {warning.get('message')}"))

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
                "SUSPENDED_LIFE": "Renewed by other work (suspension)",
                "RIGHT_CENSORED_LIFE": "Cutoff (current life)",
            }.get(str(obs.get("observation_type") or ""), str(obs.get("observation_type") or ""))
            note = str(obs.get("weibull_life_note") or "")
            if obs.get("data_quality_assumption_flag"):
                note = f"{note}. {obs['data_quality_assumption_flag']}" if note else str(obs["data_quality_assumption_flag"])
            observation_rows.append(
                ([str(obs.get("stream") or "—")] if population is not None else [])
                + [
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
        observation_headers = ["#", "Task ID", "Ends in", "Start", "End or cutoff", "Raw h", "Life h", "δ", "Note"]
        if population is not None:
            observation_headers = ["Stream", *observation_headers]
        if not observation_rows:
            observation_rows = [["—"] * (len(observation_headers) - 1) + ["No observations were saved with this result."]]
        parts.append(self._docx_table(observation_headers, observation_rows, font_half_points=16))

        parts.append(self._docx_heading("Traceability", level=2))
        parts.append(
            self._docx_paragraph(
                f"{'Population analysis' if population is not None else 'Weibull result'} {result.get('result_id')}, "
                f"run {when(result.get('run_datetime') or result.get('run_at'))}"
                + (f" by {result['run_by']}" if population is not None and result.get("run_by") else "")
                + ", method "
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

    def _excel_optional_bool(self, value: Any) -> bool | None:
        if value in (None, ""):
            return None
        if isinstance(value, bool):
            return value
        text = self._excel_text(value).lower()
        return text in {"1", "true", "yes", "y", "checked", "include"}

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

        The days wholly inside the life are summed from running totals of each
        plant day's real length (``_PlantDays``), so a life of years costs what a
        life of hours does; only its first and last part-days are measured here.
        The answer is the day-by-day walk's (``_scheduled_life_hours_by_day``),
        which is still what runs for a zone whose midnights do not behave.
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
        first_day = current.astimezone(zone).date()
        last_day = stop.astimezone(zone).date()
        days = _plant_days(zone)
        totals = days.full_days(first_day, last_day, current) if days is not None else None
        if totals is None:
            return LifeDataService._scheduled_life_hours_by_day(current, stop, hours_per_day, exclude_weekends, zone)
        weekday_seconds, weekend_seconds, first_midnight, last_midnight = totals
        if not exclude_weekends:
            weekday_seconds, weekend_seconds = weekday_seconds + weekend_seconds, 0
        share = hours_per_day / 24.0
        scheduled_hours = 0.0
        excluded_weekend_hours = 0.0
        excluded_non_run_hours = 0.0

        def add(segment_start: datetime, segment_end: datetime, day: date) -> None:
            nonlocal scheduled_hours, excluded_weekend_hours, excluded_non_run_hours
            raw_hours = (segment_end - segment_start).total_seconds() / 3600.0
            if raw_hours <= 0:
                return
            if exclude_weekends and day.weekday() >= 5:
                excluded_weekend_hours += raw_hours
            else:
                segment_scheduled = raw_hours * share
                scheduled_hours += segment_scheduled
                excluded_non_run_hours += max(0.0, raw_hours - segment_scheduled)

        if first_day == last_day:
            add(current, stop, first_day)
            return scheduled_hours, excluded_weekend_hours, excluded_non_run_hours
        add(current, first_midnight, first_day)
        weekday_hours = weekday_seconds / 3600.0
        scheduled_hours += weekday_hours * share
        excluded_non_run_hours += max(0.0, weekday_hours - weekday_hours * share)
        excluded_weekend_hours += weekend_seconds / 3600.0
        add(last_midnight, stop, last_day)
        return scheduled_hours, excluded_weekend_hours, excluded_non_run_hours

    @staticmethod
    def _scheduled_life_hours_by_day(
        current: datetime, stop: datetime, hours_per_day: float, exclude_weekends: bool, zone: tzinfo
    ) -> tuple[float, float, float]:
        """``_scheduled_life_hours`` one plant day at a time, between two UTC instants."""

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
        # The two shapes nearly every stored date has, read without trying each
        # format in turn (a failed strptime costs as much as a parse): a value with
        # a time cannot be a date-only one, and an ISO date reads directly. An
        # empty one (a work order never started) is no date at all.
        if not text:
            return None
        if ":" in text:
            return self._parse_datetime(value)
        if len(text) == 10 and text[4] == "-" and text[7] == "-":
            try:
                return self._plant_midnight(date.fromisoformat(text), zone)
            except ValueError:
                pass
        for fmt in DATE_ONLY_FORMATS:
            try:
                day = datetime.strptime(text, fmt).date()
            except ValueError:
                continue
            return self._plant_midnight(day, zone)
        return self._parse_datetime(value)

    @staticmethod
    def _duplicate_check_flag(raw_hours: float) -> str | None:
        """The DAT-004 §12 duplicate-review flag for a life this short, or None."""

        if raw_hours < DUPLICATE_CHECK_RAW_HOURS:
            return f"Check for duplicate - completed within {DUPLICATE_CHECK_RAW_HOURS:g} hour of the event before it"
        return None

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

        Beta is the root of the profile score (equation W8 on the Documentation page),
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

