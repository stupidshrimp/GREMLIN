"""Service layer for the PM Calendar page.

Bridges Limble and the local PM calendar database: pulls PM tasks from
Limble on a background thread and stores them via PmCalendarRepository, and
serves the reads the calendar page needs (asset list, events for a date
range, YTD summary counts).
"""

from __future__ import annotations

import calendar
import hashlib
import re
import statistics
import threading
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from integrations.limble import LimbleClient, LimbleConfig
from repositories.pm_calendar_repo import DEFAULT_PM_CALENDAR_DB_PATH, PmCalendarRepository
from services.ingestion_service import _asset_parent_id, _unix_to_iso_utc
from services.sync_service import LIMBLE_ENV_PREFIX, load_dotenv_files

# Job states, same vocabulary as services/sync_service.py's LimbleSyncRunner.
STATE_IDLE = "idle"
STATE_RUNNING = "running"
STATE_SUCCEEDED = "succeeded"
STATE_FAILED = "failed"

# Per docs/availability-dashboard-design.md's analysis of this account's Limble
# data: task type 1 is Preventive Maintenance. Not using is_pm_candidate here
# on purpose -- that flag has a documented false-positive bug on this dataset.
_PM_TYPE_VALUE = "1"

# Some PM names in this account follow "<asset> - <code> - <description>",
# e.g. "3103 - M - Salvagnini Laser" for that asset's monthly line -- the
# code is the recurrence cadence, spelled out by hand here rather than read
# from Limble. Reading it from Limble is not on offer: a live pull of this
# account's entire task history (244k+ rows) came back with zero rows where
# template=true, for an asset whose "Manage PM Templates" page lists five --
# so whatever drives Limble's own recurrence, the /tasks endpoint this app
# syncs from does not expose it.
#
# How far that convention reaches, measured against a real sync (76,463 PM
# rows, September 2026): 32.5% of names carry a code _cadence_code can read.
# Names like "HYDMECH BAND SAW PM INSPECTION" make up much of the rest and
# were never going to parse. That is why this table is the fallback and not
# the source -- _observed_interval reads a line's cadence from the gaps
# between its own completions first, which needs no naming convention at all.
#
# Together they take the same sync from 1,593 projectable PM lines to 5,400,
# and from 563 of 3,899 assets to 2,998. History carries 3,345 of those
# lines and the name carries 2,055; 2,717 lines have neither and draw
# nothing, which is the same blank the calendar showed before any of this
# existed.
#
# So the table earns its place twice over, and neither reason is "how this
# account names things". It carries the lines whose history is too thin or
# too erratic to read but whose name still says what they are; and its
# values are what an observed interval snaps to, so a line seen repeating on
# 30-ish days is recorded as monthly and stays on its day of the month
# rather than drifting a little earlier every time.
#
# Where the table does apply, it is accurate. Median observed gap between
# completions, same sync: M 30d (table 30), Q 85d (90), SA 178d (180),
# A 352d (360), 2W 14d (14). 3Y has a single series on the books and cannot
# be checked either way.
#
# One fixed interval per code, on purpose, even where a particular template
# in Limble repeats on a slightly different one (1435's SA is set to 24
# weeks there). The projection is meant to show the standard cadence each
# code stands for, not to reverse-engineer each template's own setting --
# and a fixed table is predictable: the same code always projects the same
# way. A new code only needs one line added here.
#
# Held as (months, days) rather than a week count so the codes that name a
# calendar period advance by one. Four weeks is not a month: it puts 13
# occurrences in a year instead of 12, and the gap compounds the further a
# series runs from its anchor -- 52 weeks is 364 days, so an annual PM drifts
# a day and a quarter earlier every year until a projection several years out
# is visibly in the wrong week. 2W is the one code that really does mean a
# number of weeks, so it keeps a day count.
_CADENCE: dict[str, tuple[int, int]] = {
    "2W": (0, 14),  # Every two weeks
    "M": (1, 0),  # Monthly
    # No PM in this account is named "2M", but inferring intervals from
    # completion history turns up ~190 lines repeating on 59-62 days. Without
    # an entry here they snap to nothing and step by raw days, drifting off
    # their day of the month; with one they behave like every other cadence
    # that names a calendar period.
    "2M": (2, 0),  # Every two months
    "Q": (3, 0),  # Quarterly
    "SA": (6, 0),  # Semi-annual
    "A": (12, 0),  # Annual
    "3Y": (36, 0),  # Every three years
}

# How many intervals an anchor may fall behind before this stops projecting
# from it. A line whose last completion is this many cycles back has missed
# that many in a row, which in practice means it is no longer running: the
# asset was scrapped, the template was deleted in Limble, or the line was
# renamed and its new occurrences file under a different name. Left
# unchecked, one stale row keeps drawing confident pills into any window
# someone navigates to -- indistinguishable from an estimate anchored on
# last month's work, and far likelier to be wrong. It also bounds the loop
# below, which would otherwise grind through a decade of intervals to emit
# a handful of in-window rows.
_MAX_STALE_INTERVALS = 3

# Reading a line's cadence out of its own completion history needs enough
# completions to see more than one gap -- two dates make a single gap, which
# could be any two unrelated visits -- and gaps that agree with each other.
# A PM done twice in a week and then not again for a year has no cadence to
# speak of, and a median taken over that would invent one. The spread test is
# a median absolute deviation rather than a standard deviation, so one
# outlying gap (a shutdown, a holiday) cannot drag a real cadence out of
# range. The floor keeps short cadences from being held to an implausible
# few days of precision.
# Other spellings of a cadence already in the table. Kept apart from
# _CADENCE so that stays one entry per interval -- it doubles as the set of
# targets an observed interval snaps to, and duplicates there would be noise.
_CADENCE_ALIASES: dict[str, str] = {
    "BIM": "2M",  # bi-monthly; 35 lines carry it and 97% really do repeat on two months
    "BIW": "2W",  # bi-weekly
}

_MIN_COMPLETIONS_FOR_INTERVAL = 3
_INTERVAL_SPREAD_FRACTION = 0.25
_INTERVAL_SPREAD_FLOOR_DAYS = 7

# How close an observed gap has to be to a standard cadence to be called that
# cadence. Measured against a real sync of this account, the codes land well
# inside this: M 30d against 30, Q 85 against 90, SA 178 against 180, A 352
# against 360. Snapping matters because a cadence expressed in months steps
# by calendar months and stays on its day of the month, where a raw day count
# walks backwards through the year.
_CADENCE_SNAP_TOLERANCE = 0.15


def _cadence_code(task_name: str | None) -> str | None:
    """Pull the recurrence code out of a PM name, e.g. "M" from "3103 - M - ...".

    Split on a hyphen with or without spaces around it, because this account
    writes the same convention both ways -- "3209 - Q - Schmidt Scribing
    Machine" and "8945-Q-DEHUMIDIFIER FANTECH" are the same shape, and a
    splitter that only accepted " - " rejected 11,445 rows carrying a code
    already in this table.

    Scans the fields between the first and the last rather than taking the
    second, because a sub-asset brings its own hyphen: "4002-S19-M-Chain"
    puts S19 where a positional rule would look. Only an exact match against
    _CADENCE or _CADENCE_ALIASES counts, so the sub-asset designators,
    equipment abbreviations and stray numbers that also land in those fields
    read as no code at all -- which is what they are.

    The first field is skipped because it is the asset number. The last is
    not, because 14 rows here name the cadence at the end ("11000 HVAC 103-1
    - Q") and a rule that stopped short of it would lose them. Scanning left
    to right means an earlier field wins where both could match, which is the
    conventional position.

    Note what a None actually is here. _map_pm_task already filters the sync
    to type == "1" and drops templates, and this only ever runs over rows
    read back out of pm_task, so every name reaching this function is a real
    PM occurrence -- never an ordinary work order harmlessly declining to
    parse. A None is always a PM that will silently never be projected: a
    stray "Mo", an em dash where " - " was expected, a description that
    itself contains " - " and shifts the segments along. That asset simply
    stops showing future PMs and the calendar looks exactly as it did before
    this feature existed.

    Returning None per row is still the right behaviour -- there is no flag
    or type to fall back on, so an unparseable name genuinely cannot be
    projected. But it is a convention drift worth seeing, which is why the
    sync reports how many rows it hit (pm_tasks_without_cadence).
    """

    if not task_name:
        return None
    fields = [f.strip().upper() for f in re.split(r"\s*-\s*", " ".join(task_name.split()))]
    for field in fields[1:]:
        if field in _CADENCE:
            return field
        if field in _CADENCE_ALIASES:
            return _CADENCE_ALIASES[field]
    return None


def _series_name(task_name: str | None) -> str | None:
    """The identity of one recurring PM line, normalised for grouping.

    Whitespace collapsed and upper-cased so "3103 - M - Laser Optics" and
    "3103 -  m  - laser optics" are recognised as the same line. The whole
    name is the identity on purpose: the description is the only thing
    telling two same-cadence PMs on one asset apart.
    """

    if not task_name:
        return None
    collapsed = " ".join(task_name.split()).upper()
    return collapsed or None


def _series_slug(series_name: str) -> str:
    """A short stable stand-in for a PM line's name, for synthetic task ids.

    Projected rows need ids unique per line, not just per (asset, cadence),
    now that one asset can carry several series of the same cadence. Never
    shown: the day dialog prints the Limble task number for real rows only.
    """

    return hashlib.blake2b(series_name.encode(), digest_size=4).hexdigest()


def _add_days(iso_date: str, days: int) -> str:
    """Add whole days to an ISO date (or datetime) string, returning "YYYY-MM-DD"."""

    return (date.fromisoformat(iso_date[:10]) + timedelta(days=days)).isoformat()


def _add_months(iso_date: str, months: int) -> str:
    """Add whole calendar months, clamping onto the end of a shorter month."""

    anchor = date.fromisoformat(iso_date[:10])
    total = anchor.month - 1 + months
    year = anchor.year + total // 12
    month = total % 12 + 1
    # The 31st of a month followed by a 30-day one lands on the 30th, not on
    # the 1st of the month after.
    day = min(anchor.day, calendar.monthrange(year, month)[1])
    return date(year, month, day).isoformat()


def _advance(iso_date: str, interval: tuple[int, int], steps: int = 1) -> str:
    """Move ``steps`` whole intervals forward from an ISO date.

    An interval is (months, days) with exactly one side set. Always called
    against the series anchor rather than against the previous projected
    date: stepping one month at a time from a clamped date would walk a 31st
    down to the 28th and leave it there, where anchor + n months returns to
    the 31st in every month long enough to have one.
    """

    months, days = interval
    return _add_months(iso_date, months * steps) if months else _add_days(iso_date, days * steps)


def _nominal_days(interval: tuple[int, int]) -> int:
    """Roughly how long one interval is, in days.

    Only used where a couple of days either way changes nothing: the
    half-interval window deciding whether a real row already covers a
    projected slot, and the staleness cutoff. Actual projected dates come
    from _advance, which counts calendar months where the interval has them.
    """

    months, days = interval
    return months * 30 + days


def _snap_to_cadence(days: int) -> tuple[int, int]:
    """Express an observed gap as a calendar cadence where one fits.

    A line repeating every thirty-ish days is monthly, and saying so keeps it
    on the same day of each month instead of drifting a day or two earlier
    every time. A gap matching no standard cadence keeps its measured day
    count, which is the honest answer for a PM that genuinely runs on, say,
    forty-five days.
    """

    for interval in _CADENCE.values():
        nominal = _nominal_days(interval)
        if abs(days - nominal) <= nominal * _CADENCE_SNAP_TOLERANCE:
            return interval
    return (0, days)


def _observed_interval(completed_dates: list[str]) -> tuple[int, int] | None:
    """The interval a PM line actually repeats on, read from its own history.

    Preferred over the code in the name, for two reasons. Coverage: measured
    against a real sync of this account, 84% of PM names carry no readable
    code, so a name-only projection leaves five assets in six with a blank
    future -- while 41% of all lines have a completion history consistent
    enough to read. And accuracy: where a template's real cadence differs
    from what its code implies (an "SA" line set to 24 weeks rather than 26),
    what actually happened is the better authority than what it was called.

    None when the history cannot support a cadence -- too few completions, or
    gaps that disagree -- which sends the caller back to the name.
    """

    days = sorted({value[:10] for value in completed_dates})
    if len(days) < _MIN_COMPLETIONS_FOR_INTERVAL:
        return None

    # Same-day repeats would otherwise contribute a zero gap and drag the
    # median down; duplicate dates are already collapsed by the set above.
    gaps = [gap for gap in (_days_between(a, b) for a, b in zip(days, days[1:])) if gap > 0]
    if len(gaps) < 2:
        return None

    middle = statistics.median(gaps)
    spread = statistics.median([abs(gap - middle) for gap in gaps])
    if spread > max(_INTERVAL_SPREAD_FLOOR_DAYS, middle * _INTERVAL_SPREAD_FRACTION):
        return None
    return _snap_to_cadence(round(middle))


def _today() -> str:
    """Today as an ISO date string.

    One seam for the two places that need the current date -- the staleness
    cutoff and the history bound it implies -- so a test can pin both
    together without patching datetime for the whole module.
    """

    return date.today().isoformat()


def _days_between(earlier: str, later: str) -> int:
    """Whole days from ``earlier`` to ``later``; negative if ``later`` is first."""

    return (date.fromisoformat(later[:10]) - date.fromisoformat(earlier[:10])).days


def _history_since() -> str:
    """The oldest due date worth reading when hunting for series anchors.

    _project_future_events refuses an anchor more than _MAX_STALE_INTERVALS
    intervals old, so nothing older than that -- measured against the
    longest cadence on the books -- can produce a pill however far back it
    sits. The extra year of slack is because this bounds due_date while the
    anchor is a completed_date, and a PM can be finished well after it came
    due.
    """

    longest = max(_nominal_days(interval) for interval in _CADENCE.values())
    return _add_days(_today(), -(_MAX_STALE_INTERVALS * longest + 366))


class PmCalendarService:
    """Owns the PM calendar database and the Limble sync that fills it."""

    def __init__(self, db_path: str | Path = DEFAULT_PM_CALENDAR_DB_PATH) -> None:
        self.repo = PmCalendarRepository(db_path)
        # Guards self._job, which the background sync thread writes to and
        # web requests (status polls) read from at the same time.
        self._lock = threading.Lock()
        self._job: dict[str, Any] = {"state": STATE_IDLE}
        # The schema is created on first use, not here. This service is built
        # while app.py is still importing, and the calendar page may never be
        # opened in a given run -- so a database path this machine cannot write
        # to has to degrade to one page reporting it, not to the app failing to
        # start. Same reasoning, and the same first-use pattern, as
        # BugReportStore; app.py builds both and says so there.
        self._schema_ready = False

    def _ensure_schema(self) -> None:
        """Create the table the first time something actually needs it.

        Only success is remembered, so a path that was unreachable at startup
        is retried on the next request rather than being written off for the
        life of the process.

        Not guarded by self._lock: that lock exists to keep status polls cheap
        while a sync runs, and holding it across a disk write would block them.
        Two threads racing here both run CREATE TABLE IF NOT EXISTS, which is
        idempotent, so the race costs a redundant statement and nothing else.
        """

        if self._schema_ready:
            return
        self.repo.ensure_schema()
        self._schema_ready = True

    # ------------------------------------------------------------------
    # Sync (background)
    # ------------------------------------------------------------------
    def start_sync(self) -> dict[str, Any]:
        """Start a Limble pull in the background, if one isn't already running."""

        with self._lock:
            if self._job.get("state") == STATE_RUNNING:
                return dict(self._job)
            self._job = {"state": STATE_RUNNING, "fetched": 0, "error": None}

        thread = threading.Thread(target=self._run_sync, daemon=True)
        thread.start()
        return self.status()

    def status(self) -> dict[str, Any]:
        """A snapshot of the current (or most recent) sync's status."""

        with self._lock:
            return dict(self._job)

    def _run_sync(self) -> None:
        try:
            # First, before anything slow: a database this process cannot write
            # to should be reported in seconds, not after a full Limble pull has
            # spent several minutes earning a row it has nowhere to put.
            self._ensure_schema()

            # Only reads LIMBLE_* variables, the same restricted load the main
            # sync dashboard uses -- see services/sync_service.py's own notes
            # on why the web process must not pick up GREMLIN_DB_PATH here.
            load_dotenv_files(only_prefix=LIMBLE_ENV_PREFIX)
            config = LimbleConfig.from_env()
            client = LimbleClient(config)

            def on_task_page(items_so_far: int, _pages_read: int) -> None:
                with self._lock:
                    if self._job.get("state") == STATE_RUNNING:
                        self._job["fetched"] = items_so_far

            tasks = client.get_tasks(on_page=on_task_page)
            assets = client.get_assets()
            asset_names = {
                str(asset.get("assetID")): asset.get("name")
                for asset in assets
                if asset.get("assetID") not in (None, "")
            }
            # Limble's /assets carries the hierarchy and this call was already
            # making it -- it just threw everything but the name away. Reuses
            # ingestion_service's reader so the several spellings Limble has
            # used for the parent field stay described in one place.
            asset_parents = {
                str(asset.get("assetID")): _asset_parent_id(asset)
                for asset in assets
                if asset.get("assetID") not in (None, "")
            }

            rows = []
            for task in tasks:
                row = self._map_pm_task(task, asset_names, asset_parents)
                if row is not None:
                    rows.append(row)

            result = self.repo.upsert_tasks(rows)

            # Two different questions, both worth an answer on the page.
            # How far the naming convention still reaches, which is what a
            # drift in it looks like from here:
            uncoded = sum(1 for row in rows if _cadence_code(row.get("task_name")) is None)
            # And how much of the calendar can actually draw estimates, which
            # since projection learned to read completion history is no longer
            # the same question -- a line with no code in its name projects
            # perfectly well if it has repeated often enough to show a
            # cadence.
            not_projectable = self._count_unprojectable_lines(rows)

            with self._lock:
                self._job = {
                    "state": STATE_SUCCEEDED,
                    "fetched": len(tasks),
                    "matched_pm_tasks": len(rows),
                    "pm_tasks_without_cadence": uncoded,
                    "pm_lines_not_projectable": not_projectable,
                    "upserted": result["upserted"],
                    "error": None,
                }
        except Exception as exc:  # noqa: BLE001 - reported to the page, not swallowed
            with self._lock:
                self._job = {"state": STATE_FAILED, "error": str(exc)}

    # ------------------------------------------------------------------
    # Mapping a raw Limble task into a pm_task row
    # ------------------------------------------------------------------
    @staticmethod
    def _is_template(task: dict[str, Any]) -> bool:
        """True when this task is a PM's recurring definition, not a real occurrence."""

        value = task.get("template")
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "y", "t")
        return bool(value)

    def _map_pm_task(
        self,
        task: dict[str, Any],
        asset_names: dict[str, Any],
        asset_parents: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Return a pm_task row for a real PM occurrence, or None to skip this task."""

        if str(task.get("type")) != _PM_TYPE_VALUE:
            return None
        if self._is_template(task):
            return None

        task_id = task.get("taskID")
        if task_id in (None, ""):
            return None

        asset_id = task.get("assetID")
        asset_id_str = str(asset_id) if asset_id not in (None, "") else None

        due_raw = task.get("dueDate") if task.get("dueDate") not in (None, "", 0) else task.get("due")
        due_date = _unix_to_iso_utc(due_raw)
        completed_date = _unix_to_iso_utc(task.get("dateCompleted"))

        return {
            "task_id": str(task_id),
            "asset_id": asset_id_str,
            "asset_number": asset_id_str,
            "asset_name": asset_names.get(asset_id_str),
            "parent_asset_id": (asset_parents or {}).get(asset_id_str),
            "task_name": task.get("name"),
            "status_raw": task.get("status") or task.get("statusID"),
            "due_date": due_date,
            "completed_date": completed_date,
            "is_completed": 1 if completed_date else 0,
        }

    @staticmethod
    def _count_unprojectable_lines(rows: list[dict[str, Any]]) -> int:
        """PM lines that will draw no estimates, for the sync to report.

        Counted per line rather than per row: one line with two hundred
        completions is one entry on the calendar, and a row count would let a
        single busy PM drown out fifty silent ones. Mirrors the choice
        _project_future_events makes -- history first, name second -- so the
        number means "this many lines show nothing in a future month".
        """

        by_series: dict[tuple[Any, str], list[dict[str, Any]]] = {}
        for row in rows:
            name = _series_name(row.get("task_name"))
            if name and row.get("asset_id"):
                by_series.setdefault((row["asset_id"], name), []).append(row)

        unprojectable = 0
        for line in by_series.values():
            completed = [r["completed_date"] for r in line if r.get("completed_date")]
            if _observed_interval(completed) is not None:
                continue
            if _cadence_code(line[0].get("task_name")) is None:
                unprojectable += 1
        return unprojectable

    # ------------------------------------------------------------------
    # Projecting PMs beyond whatever Limble has already generated
    # ------------------------------------------------------------------
    def _project_future_events(
        self,
        history: list[dict[str, Any]],
        start_date: str | None,
        end_date: str | None,
        *,
        today: str | None = None,
    ) -> list[dict[str, Any]]:
        """Estimate PM occurrences beyond whatever Limble has already generated.

        Limble only keeps one real work order alive per recurring PM at a
        time -- completing it is what makes the next one appear -- so a
        calendar that only ever shows real rows goes blank a cycle or two out
        for every asset. This fills that gap with estimates, built entirely
        from what's already synced locally: no extra Limble call, and no
        dependency on Limble's own recurrence data, which (see
        _CADENCE above) this account's /tasks endpoint doesn't expose.

        Each line's interval comes from the gaps between its own completions
        where its history can support one, and from the code in its name
        where it can't -- see _observed_interval for why that order and not
        the other. A line with neither is not projected, which is the same
        blank the calendar showed before this existed.

        Anchored on the last COMPLETED occurrence of each PM line, never on
        the last due date. A due date on a task that hasn't happened yet is
        just a plan -- it gets dragged around in Limble routinely (a tech is
        out, a part is late, a shutdown moves) and none of that should
        ripple into next year's estimate. What actually happened, once it's
        happened, doesn't move. So every projected date is
        completed-anchor + k * interval, and the whole series is blind to
        whatever the due date on the next real occurrence says today --
        rescheduling that one open task can't shift a single projected pill.

        The one case with no better anchor: a PM line that has never once
        been completed in the synced history. There's no completed_date to
        build on yet, so the latest known due_date stands in until a first
        completion gives this something sturdier to anchor on.

        An anchor more than _MAX_STALE_INTERVALS intervals old is refused
        outright: see that constant for why a line that far behind is read
        as no longer running rather than as very overdue. ``today`` exists
        so tests can pin that judgement to a fixed date.
        """

        today = today or _today()

        # Keyed on the PM line itself -- asset plus full name -- not on
        # (asset, cadence). An asset with two monthly PMs is two independent
        # series: different work, same "M". Keying on the cadence alone
        # merged them into one stream, so one line vanished from every future
        # month and the survivor was labelled with whichever row sorted
        # first, while both lines' due dates pooled into a single collision
        # check. The naming convention already carries the discriminator --
        # the description -- so the whole name is the identity.
        by_series: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for row in history:
            name = _series_name(row.get("task_name"))
            asset_id = row.get("asset_id")
            if not name or not asset_id:
                continue
            by_series.setdefault((asset_id, name), []).append(row)

        projected: list[dict[str, Any]] = []
        for (asset_id, series_name), rows in by_series.items():
            sample = rows[0]

            due_dates = [r["due_date"][:10] for r in rows if r.get("due_date")]
            if not due_dates:
                continue
            completed_dates = [r["completed_date"][:10] for r in rows if r.get("completed_date")]

            # What this line actually does, then what it was called. The name
            # is the fallback rather than the source: it covers a sixth of
            # this account's PMs, and where both are available the history is
            # the better authority on a template whose real cadence drifted
            # from its code.
            interval = _observed_interval(completed_dates)
            if interval is None:
                code = _cadence_code(sample.get("task_name"))
                interval = _CADENCE[code] if code else None
            if interval is None:
                continue
            interval_days = _nominal_days(interval)

            # The anchor: last completed, or -- only for a line that has
            # never once been completed -- its own latest known due date.
            # That fallback is the one gap in the "immune to due-date
            # manipulation" promise below: with zero completion history
            # there is nothing sturdier to build the very first estimate on.
            anchor = max(completed_dates) if completed_dates else max(due_dates)

            # A line this far behind has missed _MAX_STALE_INTERVALS cycles
            # in a row and is read as retired, not as overdue. Whatever real
            # rows it still has keep showing; it just stops generating new
            # estimates off a dead anchor.
            if _days_between(anchor, today) > _MAX_STALE_INTERVALS * interval_days:
                continue

            step = 1
            while True:
                next_due = _advance(anchor, interval, step)
                if end_date is not None and next_due > end_date:
                    break
                if end_date is None and step > 500:
                    # No end date means "project forever" -- refuse to loop
                    # unbounded. The real caller (the calendar page) always
                    # sends a month's start/end, so this only guards
                    # something calling events() open-ended.
                    break
                # A slot this series already has a real row sitting in --
                # completed or still open -- shouldn't also get an estimate
                # stacked next to it. "Real, and within half a cycle of this
                # estimate" is deliberately a *local* check against that one
                # candidate date, not a global "skip everything up to the
                # furthest due date on file" rule -- so dragging the still-
                # open task's due date around only ever affects whether the
                # one nearby estimate is shown, never the rest of the
                # series. The sequence itself always advances a fixed
                # interval from the completed-anchor, full stop.
                collides = any(
                    abs((date.fromisoformat(next_due) - date.fromisoformat(known)).days)
                    <= interval_days / 2
                    for known in due_dates
                )
                if not collides and (start_date is None or next_due >= start_date):
                    projected.append(
                        {
                            "task_id": (
                                f"projected-{asset_id}"
                                f"-{_series_slug(series_name)}-{next_due}"
                            ),
                            "asset_id": asset_id,
                            "asset_number": sample.get("asset_number"),
                            "asset_name": sample.get("asset_name"),
                            "task_name": sample.get("task_name"),
                            "status_raw": None,
                            "due_date": next_due,
                            "completed_date": None,
                            "is_completed": 0,
                            "is_projected": True,
                        }
                    )
                step += 1

        return projected

    # ------------------------------------------------------------------
    # Reads for the page
    # ------------------------------------------------------------------
    def asset_options(self) -> list[dict[str, Any]]:
        self._ensure_schema()
        return self.repo.asset_options()

    def _with_descendants(self, asset_ids: list[str]) -> list[str]:
        """Expand a selection so picking a parent picks everything under it.

        An asset whose PMs are filed against it directly stays in the set --
        4002 has its own lines as well as twenty sub-assets, and picking it
        should show both. Assets with no PMs never appear in the map, so a
        parent nobody can select is simply not a parent here.

        Walks breadth-first with a seen set: Limble's hierarchy is a tree in
        practice, but a cycle in the data would otherwise hang the request,
        and this runs on a plain GET.
        """

        children: dict[str, list[str]] = {}
        for asset_id, parent_id in self.repo.asset_parent_map().items():
            if parent_id:
                children.setdefault(str(parent_id), []).append(asset_id)

        expanded: list[str] = []
        seen: set[str] = set()
        queue = [str(asset_id) for asset_id in asset_ids]
        while queue:
            asset_id = queue.pop(0)
            if asset_id in seen:
                continue
            seen.add(asset_id)
            expanded.append(asset_id)
            queue.extend(children.get(asset_id, ()))
        return expanded

    def events(
        self,
        asset_ids: list[str] | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> list[dict[str, Any]]:
        if asset_ids is not None and len(asset_ids) == 0:
            return []
        self._ensure_schema()
        if asset_ids is not None:
            asset_ids = self._with_descendants(asset_ids)
        real = self.repo.fetch_tasks(asset_ids=asset_ids, due_since=start_date, due_until=end_date)

        if asset_ids is None:
            # No asset filter means the history read below would be
            # SELECT * FROM pm_task with no date bound -- every row in the
            # table materialised into dicts, then grouped into a series per
            # PM line in the account, on a plain GET. The page never asks
            # for that: it always sends its chipped-in assets, and nothing
            # selected sends ?assets= empty, which returned above. So the
            # unscoped call keeps working and stays cheap, it just doesn't
            # project.
            return real

        # Projecting needs each series' full history to find its last
        # completed occurrence, not just whatever falls in the visible
        # month -- that anchor is very often months before the window
        # someone is currently looking at. Bounded below by the staleness
        # cutoff, since an anchor older than that is refused anyway.
        history = self.repo.fetch_tasks(asset_ids=asset_ids, due_since=_history_since())
        projected = self._project_future_events(history, start_date, end_date)

        combined = real + projected
        combined.sort(key=lambda row: row["due_date"] or "")
        return combined

    def summary(self, asset_ids: list[str] | None = None) -> dict[str, Any]:
        empty = {"scheduled": 0, "completed": 0, "overdue": 0, "compliance": 0.0}
        if asset_ids is not None and len(asset_ids) == 0:
            return empty

        self._ensure_schema()
        if asset_ids is not None:
            asset_ids = self._with_descendants(asset_ids)
        # Through _today() rather than date.today() so the whole page reads
        # one clock: the tiles and the grid disagreeing about where "now" is
        # would be a genuinely confusing bug to chase.
        today = _today()
        year_start = date.fromisoformat(today).replace(month=1, day=1).isoformat()
        due_ytd = self.repo.fetch_tasks(asset_ids=asset_ids, due_since=year_start, due_until=today)

        scheduled = len(due_ytd)
        completed = sum(1 for task in due_ytd if task["completed_date"])
        overdue = sum(
            1
            for task in due_ytd
            if not task["completed_date"] and task["due_date"] and task["due_date"] < today
        )
        compliance = round((completed / scheduled) * 100, 1) if scheduled else 0.0

        return {
            "scheduled": scheduled,
            "completed": completed,
            "overdue": overdue,
            "compliance": compliance,
        }