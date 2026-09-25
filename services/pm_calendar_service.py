"""Service layer for the PM Calendar page.

Bridges Limble and the local PM calendar database: pulls PM tasks from
Limble on a background thread and stores them via PmCalendarRepository, and
serves the reads the calendar page needs (asset list, events for a date
range, YTD summary counts).
"""

from __future__ import annotations

import hashlib
import itertools
import re
import threading
from bisect import bisect_left
from collections import deque
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from integrations.limble import LimbleClient, LimbleConfig
from repositories.pm_calendar_repo import DEFAULT_PM_CALENDAR_DB_PATH, PmCalendarRepository
from services.ingestion_service import _unix_to_iso_utc
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

# Every PM name in this account is "<asset> - <code> - <description>", e.g.
# "3103 - M - Salvagnini Laser" for that asset's monthly line -- the code is
# the recurrence cadence, spelled out by hand here rather than read from
# Limble. That's deliberate, not a shortcut: a live pull of this account's
# entire task history (244k+ rows) came back with zero rows where
# template=true, for an asset whose "Manage PM Templates" page lists five --
# so whatever Limble uses to drive its own recurrence, the /tasks endpoint
# this app syncs from does not expose it. This table is the substitute, and
# it only holds because the account's PM names keep following the same
# convention; see _cadence_code below for what happens when one doesn't.
#
# One fixed interval per code, on purpose, even where a particular template
# in Limble repeats on a slightly different one (1435's SA is set to 24
# weeks there). The projection is meant to show the standard cadence each
# code stands for, not to reverse-engineer each template's own setting --
# and a fixed table is predictable: the same code always projects the same
# way. A new code only needs one line added here.
#
# These are whole weeks (bar the daily one), not calendar months or years, and that is also on
# purpose: it's how this account's templates are set up in Limble
# ("Repeats Every 4 Weeks on Fri", "Every 52 Weeks", "Every 156 Weeks"), so
# it's what Limble will actually generate. It does mean M comes round 13
# times a year rather than 12, and A lands about a day and a quarter
# earlier each year (52 weeks is 364 days); 3Y is 1,092 days against a
# calendar ~1,096. Calendar-month arithmetic would look tidier and disagree
# with Limble. The staleness bound below keeps any projection within a few
# cycles of real activity, so the drift never has years to build up.
# In days rather than weeks, because this account has a daily PM ("1435 - D
# - Stokes Tablet") and a day is not a whole number of weeks. Everything
# else is still the week count it always was, times seven.
#
# Every code below was counted in this account's own PM names; each comment
# gives how many distinct PMs carry it. Codes not listed here get no
# estimates at all -- see _cadence_code.
_CADENCE_DAYS: dict[str, int] = {
    "D": 1,  # Daily (3)
    "W": 7,  # Weekly (15)
    "2W": 14,  # Every two weeks (3)
    "M": 28,  # Monthly, 4 weeks (381)
    "2M": 56,  # Every two months, 8 weeks (1)
    "3M": 84,  # Every three months, 12 weeks -- the same as Q (1)
    "Q": 84,  # Quarterly, 12 weeks (445)
    "SA": 182,  # Semi-annual, 26 weeks (403)
    "A": 364,  # Annual, 52 weeks (307)
    "2Y": 728,  # Every two years, 104 weeks (22)
    "3Y": 1092,  # Every three years, 156 weeks (20)
    "4Y": 1456,  # Every four years, 208 weeks (1)
    "5Y": 1820,  # Every five years, 260 weeks (12)
}


def _cadence_code(task_name: str | None) -> str | None:
    """Pull the recurrence code out of a PM name, e.g. "M" from "3103 - M - ...".

    The second " - "-delimited segment, upper-cased so a stray "m" or "sa"
    still matches. Anything that doesn't split that way, or whose middle
    segment isn't one of _CADENCE_DAYS, returns None -- silently, on
    purpose. Most rows this runs against are ordinary work orders and work
    requests that were never going to be cadence-coded in the first place,
    and even among PMs this account's naming convention is the one thing
    tying a code to a cadence -- there is no flag or type this can check
    instead. A name that doesn't parse is a PM this can't project, not a
    bug to raise about.
    """

    if not task_name:
        return None
    parts = [part.strip().upper() for part in task_name.split(" - ")]
    if len(parts) < 2:
        return None
    code = parts[1]
    return code if code in _CADENCE_DAYS else None


# How long the job takes, written on the end of a PM's name: "- 3hrs.",
# "- 30 min.", "- 2 Hours Req.". Limble users edit these as estimates get
# revised, and the same PM then arrives under two names -- which split it
# into two lines, each projecting its own estimates, and stopped an estimate
# from yielding to the real work order it duplicates. A duration is how long
# a job takes, not which job it is, so it isn't part of the line's identity.
#
# Hours and minutes only, and a plausible number of them. Everything else a
# name ends in is left alone, because on this account those numbers mean
# something: "(6D)" on a boiler, "4002-S05 211d", meter readings like
# "501 hr", street addresses ("10165 W. 52nd street"), and -- the one that
# matters most -- how often the PM runs. A weekly and a two-weekly PM are
# told apart by their code (W against 2W), and nothing here touches the
# code. An earlier, looser version of this also swallowed bare "m" and "d",
# which turned "PM CNC Okuma Genos L300M" into "PM CNC Okuma Genos L".
_DURATION_TAIL = re.compile(
    r"""[\s\-–—]*\(?\s*(?:\d|1\d|2[0-4])(?:[.,]\d+)?\s*
            (?:h|hr|hrs|hour|hours)\b\s*(?:req(?:uired|\.)?)?[\s.)]*$
      | [\s\-–—]*\(?\s*\d{1,3}\s*
            (?:min|mins|minute|minutes)\b\s*(?:req(?:uired|\.)?)?[\s.)]*$""",
    re.IGNORECASE | re.VERBOSE,
)


def _strip_duration(text: str) -> str:
    """Drop a trailing duration from one segment of a PM name."""

    return _DURATION_TAIL.sub("", text).strip(" -–—").strip()


def _series_key(task_name: str) -> str:
    """One PM line's identity: its name, minus how long the job takes.

    The line, not the cadence code, is what repeats. One asset can carry two
    PMs with the same code -- a monthly on the machine and a monthly on its
    chiller -- and keyed on the code alone their histories were pooled into
    one stream: one anchor, one name, and the other line gone from every
    future month. The name is what tells them apart.

    Case and runs of spaces are folded, so a stray capital or a double space
    in Limble doesn't split one line in two, and so is a trailing duration
    (see _DURATION_TAIL): "3103 - M - Salvagnini Laser - 3hrs." and
    "3103 - M - Salvagnini Laser" are one PM whose estimate got revised, not
    two monthly PMs on one laser.

    Anything else after the description is kept, because it usually says
    *which* thing is being maintained: "- A side" and "- B side" stay two
    lines.
    """

    parts = [part.strip() for part in task_name.split(" - ")]
    # The last segment is the only place a duration belongs; a duration
    # written without the spacing convention ("Salvagnini ACN- 30 min") is
    # inside that segment rather than a segment of its own.
    if parts:
        parts[-1] = _strip_duration(parts[-1])
    kept = [part for part in parts if part]
    return " ".join(" - ".join(kept).split()).casefold()


# A PM line with nothing open in Limble, whose newest occurrence on file is
# more than this many of its own intervals old, is treated as no longer
# running and isn't projected.
#
# A scrapped asset, a template deleted in Limble, a line renamed so new
# occurrences land under a different name: all of them leave their last
# completion sitting in pm_task, and without a bound every one of those
# would keep drawing estimates into any year someone navigated to --
# indistinguishable on the calendar from an estimate anchored on last
# month's completion, and far more likely to be wrong. What they have in
# common is that everything on file is finished and nothing new has
# appeared. For a renamed line, three cycles is how long the old name's
# estimates overlap the new one's.
#
# An open work order always keeps a line alive, however overdue. A PM
# that's months late on a machine that's still running is exactly what the
# calendar should keep showing, and Limble won't necessarily create newer
# occurrences while one sits undone. The cost: a scrapped asset whose last
# work order was left open keeps projecting -- but that open work order is
# also a red overdue pill on the calendar, so it's visible, and closing it
# in Limble is what retires the line here too.
_STALE_AFTER_INTERVALS = 3

# While a line has an open (or overdue) work order, projection only ever
# looks three cycles past the last completion, then stops -- not per month
# viewed, a hard ceiling on the line itself. Limble only keeps one real
# occurrence open at a time, so a run of estimates stacking up past it is
# guesswork on top of guesswork: the open task is already the calendar's
# best information about what's next, and it may get rescheduled, split, or
# turned into something else entirely before it's done. Three cycles is
# enough to fill in a bit of runway without pretending to know the shape of
# a series that hasn't been resolved yet. Completing that work order clears
# it -- has_open goes false, the anchor moves to the new completion, and
# projection resumes at its normal, window-bounded pace.
_OPEN_LINE_PROJECTION_LIMIT = 3


# How far a walk up the tree will go before giving up. Same guard, and the
# same number, as the Excel hierarchy macro this mirrors: real hierarchies are
# a handful of levels deep, so anything longer is bad data, not a deep tree.
_MAX_HIERARCHY_HOPS = 100


def _normalize_id(value: Any) -> str | None:
    """An id as a plain string: "4002", never "4002.0" or " 4002 ".

    Ids arrive as numbers, as strings, and occasionally as whole numbers that
    have been through a float somewhere. Two spellings of the same id would
    quietly break every parent link between them, so they are folded here,
    once, the way the Excel macro's NormalizeId does it.
    """

    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    text = str(value).strip()
    if not text:
        return None
    try:
        # "4002.0" and "4002" are the same asset.
        number = float(text)
    except ValueError:
        return text
    return str(int(number)) if number.is_integer() else text


def _parent_asset_id(asset: dict[str, Any] | None) -> str | None:
    """The asset this one hangs under in Limble's hierarchy, or None.

    Only `parentAssetID`, and only when it names a different asset. Not the
    wider set of spellings services/ingestion_service.py accepts: that
    function also falls back to `parentID`, which on this account's /assets
    payload is *not* the parent asset -- assets that sit at the top of the
    hierarchy carry a `parentID` of their own, and reading it as a parent
    hung dozens of unrelated machines (air compressors, boilers, dryers)
    under whichever asset's ID happened to match, and made 4002 Panel
    Finishing System a "sub-asset" of a decommissioned water system.
    A wrong parent is worse than no parent here: it silently drags other
    assets' PMs onto the calendar when someone picks one machine.
    """

    if not asset:
        return None

    value = None
    for key, candidate in asset.items():
        if str(key).lower() == "parentassetid":
            value = candidate
            break
    # Limble has been seen to send an id as a nested object elsewhere in this
    # payload; the macro unpicks that case too, so this does the same rather
    # than storing a dict's repr as an id.
    if isinstance(value, dict):
        value = value.get("assetID", value.get("id"))

    parent_id = _normalize_id(value)
    if parent_id in (None, "0"):
        return None
    return None if parent_id == _normalize_id(asset.get("assetID")) else parent_id


def _today() -> date:
    """Today, as projection sees it. A function so tests can pin it."""

    return date.today()


def _near_any(candidate: date, known: list[date], tolerance_days: float) -> bool:
    """Is any date in `known` (sorted) within `tolerance_days` of `candidate`?

    Only the neighbours either side of where `candidate` would sit can be the
    closest, so this is a binary search rather than a scan of the whole
    history for every candidate.
    """

    i = bisect_left(known, candidate)
    return any(
        abs((known[j] - candidate).days) <= tolerance_days
        for j in (i - 1, i)
        if 0 <= j < len(known)
    )


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

            rows = []
            for task in tasks:
                row = self._map_pm_task(task, asset_names)
                if row is not None:
                    rows.append(row)

            result = self.repo.upsert_tasks(rows)

            # Only the part of the hierarchy that leads to a PM: anything
            # else could never show a thing on this calendar. Built from the
            # whole table rather than just this pull's rows, so an asset whose
            # PMs are already stored keeps its place in the tree.
            with_pms = {
                str(option["asset_id"]) for option in self.repo.asset_options() if option.get("asset_id")
            }
            hierarchy = self._asset_hierarchy(assets, with_pms)
            self.repo.replace_assets(hierarchy)

            with self._lock:
                self._job = {
                    "state": STATE_SUCCEEDED,
                    "fetched": len(tasks),
                    "matched_pm_tasks": len(rows),
                    "upserted": result["upserted"],
                    "assets_in_hierarchy": len(hierarchy),
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
        self, task: dict[str, Any], asset_names: dict[str, Any]
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
            "task_name": task.get("name"),
            "status_raw": task.get("status") or task.get("statusID"),
            "due_date": due_date,
            "completed_date": completed_date,
            "is_completed": 1 if completed_date else 0,
        }

    @staticmethod
    def _asset_hierarchy(
        assets: list[dict[str, Any]], with_pms: set[str]
    ) -> list[dict[str, Any]]:
        """The pm_asset rows: the hierarchy, worked out once, at sync time.

        Modelled on this account's Excel hierarchy macro (BuildHierarchyOutput
        and its lookups), which answers the same questions in the same order:
        build one id -> asset lookup, then for each asset record its immediate
        parent, the root of its branch, how many steps below that root it sits,
        and whether anything hangs under it. Materialising those four means no
        read has to walk the tree again.

        Three differences from the macro, all deliberate:

        * It keeps only assets that lead to a PM -- every asset with PMs, plus
          every asset above one of those. A parent with no PMs of its own (the
          machine, when every PM is filed against its sub-assets) is kept, so
          it stays pickable; a branch with no PMs anywhere could never show a
          thing on this calendar and is left out.
        * Each walk is memoised, so an asset's root and level are computed once
          however many assets sit under it, rather than re-walked per asset.
        * A parent that /assets doesn't contain leaves its child at the top
          level rather than pointing at something the page can't show.

        Loops are cut rather than followed: both the memoised walk and the
        macro's own hop cap stop at an asset already on the path, so bad data
        costs one wrong-looking parent instead of a hung request.
        """

        by_id: dict[str, dict[str, Any]] = {}
        for asset in assets:
            asset_id = _normalize_id(asset.get("assetID"))
            if asset_id:
                by_id[asset_id] = asset

        # Step one, as in BuildAssetLookups: id -> name, id -> parent.
        parent_of: dict[str, str | None] = {}
        name_of: dict[str, str | None] = {}
        for asset_id, asset in by_id.items():
            parent_id = _parent_asset_id(asset)
            parent_of[asset_id] = parent_id if parent_id in by_id else None
            name_of[asset_id] = asset.get("name")

        # Step two: keep the assets with PMs and everything above them.
        keep: set[str] = set()
        for asset_id in with_pms:
            current: str | None = _normalize_id(asset_id)
            hops = 0
            while current and current not in keep and hops < _MAX_HIERARCHY_HOPS:
                keep.add(current)
                current = parent_of.get(current)
                hops += 1

        # A parent that didn't make it into the set -- the hop cap cut the
        # walk short on absurd data -- would leave its child pointing at an
        # asset no read can see, so that child stands as a top-level asset.
        parent_of = {
            asset_id: (parent if parent in keep else None)
            for asset_id, parent in parent_of.items()
            if asset_id in keep
        }

        # Step three: root and level per asset -- GetRootAssetID and
        # GetAssetLevel, but each answer worked out once and reused.
        resolved: dict[str, tuple[str, int]] = {}

        def root_and_level(asset_id: str) -> tuple[str, int]:
            path: list[str] = []
            current = asset_id
            while current not in resolved:
                parent_id = parent_of.get(current)
                if parent_id is None or parent_id in path or len(path) >= _MAX_HIERARCHY_HOPS:
                    # Top of the branch, or a loop. A loop is cut here rather
                    # than merely stopped at: left in place it would leave
                    # both assets in the ring with a parent and no root, and
                    # the picker lists a branch from its root down.
                    if parent_id is not None:
                        parent_of[current] = None
                    resolved[current] = (current, 0)
                    break
                path.append(current)
                current = parent_id
            root, level = resolved[current]
            for step, walked in enumerate(reversed(path), start=1):
                resolved[walked] = (root, level + step)
            return resolved[asset_id]

        has_children = {parent_of[asset_id] for asset_id in keep if parent_of.get(asset_id)}

        def building_of(asset_id: str) -> str | None:
            """The asset one step below the root -- the macro's GetBuildingAssetID.

            Dept 914 Machinery Maint   <- root, level 0
              Building 706             <- building, level 1  *** this
                3101-3107 Salvagnini   <- level 2
                  3103 Fiber Laser     <- level 3

            Any of those last three answer "Building 706"; the root itself
            has no building above it and answers None.
            """

            current: str | None = asset_id
            hops = 0
            while current is not None and hops < _MAX_HIERARCHY_HOPS:
                if resolved[current][1] <= 1:
                    return current if resolved[current][1] == 1 else None
                current = parent_of.get(current)
                hops += 1
            return None

        rows = []
        for asset_id in sorted(keep):
            root, level = root_and_level(asset_id)
            rows.append(
                {
                    "asset_id": asset_id,
                    "asset_name": name_of.get(asset_id),
                    "parent_asset_id": parent_of.get(asset_id),
                    "root_asset_id": root,
                    "building_asset_id": building_of(asset_id),
                    "level": level,
                    "has_children": 1 if asset_id in has_children else 0,
                }
            )
        return rows

    # ------------------------------------------------------------------
    # Projecting PMs beyond whatever Limble has already generated
    # ------------------------------------------------------------------
    def _project_future_events(
        self,
        history: list[dict[str, Any]],
        start_date: str | None,
        end_date: str | None,
    ) -> list[dict[str, Any]]:
        """Estimate PM occurrences beyond whatever Limble has already generated.

        Limble only keeps one real work order alive per recurring PM at a
        time -- completing it is what makes the next one appear -- so a
        calendar that only ever shows real rows goes blank a cycle or two out
        for every asset. This fills that gap with estimates, built entirely
        from what's already synced locally: no extra Limble call, and no
        dependency on Limble's own recurrence data, which (see
        _CADENCE_DAYS above) this account's /tasks endpoint doesn't expose.

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

        A line with nothing open in Limble whose newest occurrence is more
        than _STALE_AFTER_INTERVALS of its own cycles old isn't projected at
        all -- it has most likely stopped running.

        Nothing is ever projected before today, whatever window is being
        looked at -- an "estimated" pill on a month that's already happened
        would just be an overdue real row wearing the wrong badge.

        And while a line has an open work order, projection is capped at the
        next _OPEN_LINE_PROJECTION_LIMIT cycles past the anchor and no
        further, so an unresolved occurrence doesn't grow an indefinite tail
        of guesses behind it. See _OPEN_LINE_PROJECTION_LIMIT for why.
        """

        today = _today()
        window_start = date.fromisoformat(start_date[:10]) if start_date else None
        window_end = date.fromisoformat(end_date[:10]) if end_date else None

        # One series per PM line: (asset, line name). See _series_key for why
        # the code alone isn't enough.
        by_series: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for row in history:
            task_name = row.get("task_name")
            code = _cadence_code(task_name)
            asset_id = row.get("asset_id")
            if not code or not asset_id:
                continue
            by_series.setdefault((asset_id, _series_key(task_name)), []).append(row)

        projected: list[dict[str, Any]] = []
        for (asset_id, line), rows in by_series.items():
            # Every row in a series has the same name up to case and spacing,
            # so they all parse to the same code.
            code = _cadence_code(rows[0]["task_name"])
            interval_days = _CADENCE_DAYS[code]
            # Short and stable, so two lines projected onto the same day never
            # share a task_id.
            line_tag = hashlib.sha1(line.encode("utf-8")).hexdigest()[:8]

            due_dates = sorted(date.fromisoformat(r["due_date"][:10]) for r in rows if r.get("due_date"))
            if not due_dates:
                continue
            completed_dates = [
                date.fromisoformat(r["completed_date"][:10]) for r in rows if r.get("completed_date")
            ]

            # A line that has gone quiet is not projected at all. See
            # _STALE_AFTER_INTERVALS for what "quiet" means and why an open
            # work order always counts as alive.
            has_open = any(not r.get("completed_date") for r in rows)
            last_seen = max(due_dates[-1], max(completed_dates, default=due_dates[-1]))
            if not has_open and (today - last_seen).days > _STALE_AFTER_INTERVALS * interval_days:
                continue

            # The anchor: last completed, or -- only for a line that has
            # never once been completed -- its own latest known due date.
            # That fallback is the one gap in the "immune to due-date
            # manipulation" promise below: with zero completion history
            # there is nothing sturdier to build the very first estimate on.
            anchor = max(completed_dates) if completed_dates else due_dates[-1]

            # Names and asset details come from the line's newest row, so an
            # estimate reads the way the PM currently reads in Limble.
            sample = max(rows, key=lambda r: r.get("due_date") or "")

            # When the line has been through more than one name -- someone
            # revised the duration on the end of it -- the estimate says so,
            # and the day details can list them. Newest first, each with the
            # last day it was seen on a real occurrence.
            last_seen: dict[str, str] = {}
            for row in rows:
                name = row.get("task_name")
                seen = max(
                    (value[:10] for value in (row.get("due_date"), row.get("completed_date")) if value),
                    default="",
                )
                if name and seen > last_seen.get(name, ""):
                    last_seen[name] = seen
            also_known_as = [
                {"task_name": name, "last_seen": seen}
                for name, seen in sorted(last_seen.items(), key=lambda item: item[1], reverse=True)
            ]

            if has_open:
                # A hard ceiling, not a window-jump target: always the first
                # _OPEN_LINE_PROJECTION_LIMIT cycles after the anchor, full
                # stop, regardless of which month is being viewed. See
                # _OPEN_LINE_PROJECTION_LIMIT.
                ks: range | itertools.count = range(1, _OPEN_LINE_PROJECTION_LIMIT + 1)
            else:
                # Start at the first cycle inside the window (or today, if
                # today is later than the window) rather than walking every
                # cycle from the anchor to get there. Only which estimates
                # are *looked at* changes: each one is still
                # anchor + k * interval.
                effective_start = window_start
                if effective_start is None or today > effective_start:
                    effective_start = today
                k = 1
                if effective_start > anchor:
                    k = max(1, -(-(effective_start - anchor).days // interval_days))
                ks = itertools.count(k)

            for k in ks:
                candidate = anchor + timedelta(days=interval_days * k)
                if window_end and candidate > window_end:
                    break
                if not has_open and window_end is None and k > 500:
                    # No end date means "project forever" -- refuse to loop
                    # unbounded. The real caller (the calendar page) always
                    # sends a month's start/end, so this only guards
                    # something calling events() open-ended.
                    break
                if window_start and candidate < window_start:
                    continue
                if candidate < today:
                    # Never an estimate for a month that's already happened.
                    continue

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
                if _near_any(candidate, due_dates, interval_days / 2):
                    continue

                due = candidate.isoformat()
                projected.append(
                    {
                        "task_id": f"projected-{asset_id}-{code}-{line_tag}-{due}",
                        "asset_id": asset_id,
                        "asset_number": sample.get("asset_number"),
                        "asset_name": sample.get("asset_name"),
                        "task_name": sample.get("task_name"),
                        "status_raw": None,
                        "due_date": due,
                        "completed_date": None,
                        "is_completed": 0,
                        "is_projected": True,
                        # Only when there is something to expand.
                        "also_known_as": also_known_as if len(also_known_as) > 1 else [],
                    }
                )

        return projected

    # ------------------------------------------------------------------
    # Reads for the page
    # ------------------------------------------------------------------
    def _parent_links(self) -> tuple[dict[str, dict[str, Any]], dict[str, list[str]]]:
        """The stored hierarchy as (asset by id, child ids by parent id).

        Empty on a database that hasn't been synced since this shipped, and
        everything built on it falls back to a flat list -- the calendar
        just works the way it did before until the next sync fills it in.
        """

        by_id = {str(row["asset_id"]): row for row in self.repo.fetch_assets()}
        children: dict[str, list[str]] = {}
        for asset_id, row in by_id.items():
            parent_id = row.get("parent_asset_id")
            if parent_id and parent_id in by_id:
                children.setdefault(str(parent_id), []).append(asset_id)
        return by_id, children

    def asset_options(self) -> list[dict[str, Any]]:
        """Every pickable asset, in tree order: each parent, then its children.

        Each option carries `parent_asset_id`, `depth` (0 for top level) and
        `descendant_count` (how many assets picking it brings along), which
        is what the picker indents by and what the chip's "+N" shows, plus
        `group_label` -- the building and department this asset sits in.

        The top two levels of Limble's hierarchy are the department and the
        building, which nobody picks a PM schedule by: picking "Dept 914
        Machinery Maint" would mean every PM in the department. They are
        left out of the list and named in each asset's `group_label`
        instead, so the first row of a branch is the machine line itself. A
        department or building that carries PMs of its own, or has nothing
        under it, stays pickable -- otherwise its own PMs could never be
        shown.

        An asset with PMs that the hierarchy doesn't know about -- nothing
        synced since this shipped, or an asset missing from /assets -- is
        listed at the top level on its own, exactly as before.
        """

        self._ensure_schema()
        with_pms = self.repo.asset_options()
        by_id, children = self._parent_links()

        own_pms = set()
        for option in with_pms:
            asset_id = str(option["asset_id"]) if option.get("asset_id") else None
            if not asset_id:
                continue
            own_pms.add(asset_id)
            if asset_id not in by_id:
                by_id[asset_id] = {
                    "asset_id": asset_id,
                    "asset_name": option.get("asset_name"),
                    "parent_asset_id": None,
                }

        depth_below: dict[str, int] = {}

        def deepest_below(asset_id: str, seen: frozenset[str] = frozenset()) -> int:
            """How many levels of assets hang under this one (0 for a leaf)."""

            if asset_id in depth_below:
                return depth_below[asset_id]
            if asset_id in seen:
                return 0
            below = seen | {asset_id}
            deepest = max(
                (1 + deepest_below(child_id, below) for child_id in children.get(asset_id, ())),
                default=0,
            )
            depth_below[asset_id] = deepest
            return deepest

        def is_grouping(asset_id: str) -> bool:
            """A department or building rather than a rung of the tree.

            Two things have to be true. It sits in the top two levels,
            where this account keeps departments and buildings. And its
            branch is deep enough for those two levels to be grouping at
            all: department, building, machine line, and something under the
            line. A shallower branch is a machine with sub-assets and
            nothing above it, which keeps its place -- absolute depth is the
            only thing separating the two, since Limble marks neither.

            Nothing nests under a grouping asset: its branch is listed as if
            it were the top. A grouping asset with PMs of its own is still
            listed -- a label can't be ticked, and those PMs have to be
            reachable -- but as one row beside that branch rather than above
            it, because "4002 Panel Finishing System" indented under
            "Building 706" reads as though the building owns it.
            """

            row = by_id[asset_id]
            level = row.get("level") or 0
            if level > 1:
                return False
            root = row.get("root_asset_id") or asset_id
            return deepest_below(root if root in by_id else asset_id) >= 3

        def label_for(asset_id: str) -> str:
            """"Building 706 (Dept 914 Machinery Maint)", the macro's label.

            Either half alone when that is all there is, and empty for an
            asset with neither above it.
            """

            row = by_id.get(asset_id, {})
            names = []
            for key in ("building_asset_id", "root_asset_id"):
                other = row.get(key)
                if other and other != asset_id and other in by_id:
                    name = by_id[other].get("asset_name")
                    if name and name not in names:
                        names.append(name)
            if len(names) == 2:
                return f"{names[0]} ({names[1]})"
            return names[0] if names else ""

        def name_key(asset_id: str) -> tuple[str, str]:
            name = by_id[asset_id].get("asset_name") or ""
            return (name.casefold(), asset_id)

        def count_below(asset_id: str, seen: set[str]) -> int:
            total = 0
            for child_id in children.get(asset_id, ()):
                if child_id not in seen:
                    seen.add(child_id)
                    total += 1 + count_below(child_id, seen)
            return total

        options: list[dict[str, Any]] = []
        placed: set[str] = set()

        def place(asset_id: str, depth: int) -> None:
            if asset_id in placed:
                return
            placed.add(asset_id)

            # A department or building doesn't own a rung of the list: its
            # children take its place at the depth it would have had, and
            # carry its name as a label instead. It appears as a row of its
            # own only when it has PMs filed directly against it, beside its
            # branch rather than above it.
            if is_grouping(asset_id):
                if asset_id in own_pms:
                    append_row(asset_id, depth)
                for child_id in sorted(children.get(asset_id, ()), key=name_key):
                    place(child_id, depth)
                return

            append_row(asset_id, depth)
            for child_id in sorted(children.get(asset_id, ()), key=name_key):
                place(child_id, depth + 1)

        def append_row(asset_id: str, depth: int) -> None:
            row = by_id[asset_id]
            options.append(
                {
                    "asset_id": asset_id,
                    "asset_number": asset_id,
                    "asset_name": row.get("asset_name"),
                    "parent_asset_id": row.get("parent_asset_id") if depth else None,
                    # Counted while placing, not read from the stored level:
                    # this is the depth *in the list being drawn*, so it can
                    # never disagree with the indentation the page renders --
                    # including for an asset only pm_task knows about, which
                    # has no hierarchy row at all. The stored level is the
                    # same number for everything the sync has seen.
                    "depth": depth,
                    "level": row.get("level", depth),
                    "root_asset_id": row.get("root_asset_id"),
                    "building_asset_id": row.get("building_asset_id"),
                    "group_label": label_for(asset_id),
                    "descendant_count": count_below(asset_id, {asset_id}),
                    "has_children": row.get("has_children", 0),
                }
            )

        roots = [
            asset_id
            for asset_id, row in by_id.items()
            if not row.get("parent_asset_id") or row["parent_asset_id"] not in by_id
        ]
        for asset_id in sorted(roots, key=name_key):
            place(asset_id, 0)
        return options

    def _with_descendants(self, asset_ids: list[str]) -> list[str]:
        """Expand a selection so picking a parent picks everything under it.

        The parent itself stays in: 4002 can have PMs of its own as well as
        its sub-assets', and picking it should show both. Expansion only ever
        goes down, so picking a sub-asset on its own shows just that one.
        Breadth-first with a seen set, so a parent and one of its children
        both picked -- or a loop in the data -- doesn't count anything twice.
        """

        _, children = self._parent_links()
        expanded: list[str] = []
        seen: set[str] = set()
        queue = deque(str(asset_id) for asset_id in asset_ids)
        while queue:
            asset_id = queue.popleft()
            if asset_id in seen:
                continue
            seen.add(asset_id)
            expanded.append(asset_id)
            queue.extend(children.get(asset_id, ()))
        return expanded

    def _selection(self, asset_ids: list[str], exclude: list[str] | None) -> list[str]:
        """The picked assets with their sub-assets, minus any the page hid.

        `exclude` is the exact set of asset ids unticked in a chip's expanded
        view -- exact, not expanded: the page already lists every asset under
        a hidden group, so hiding "Laser Side" arrives as Laser Side plus each
        of its machines. That keeps one sub-asset re-ticked under a hidden
        group visible, rather than hidden again by its parent.
        """

        hidden = set(exclude or ())
        return [asset_id for asset_id in self._with_descendants(asset_ids) if asset_id not in hidden]

    def events(
        self,
        asset_ids: list[str] | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        exclude: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        if asset_ids is not None and len(asset_ids) == 0:
            return []
        self._ensure_schema()
        if asset_ids is not None:
            asset_ids = self._selection(asset_ids, exclude)
            # Everything picked has been hidden. An empty list reaching
            # fetch_tasks would mean "every asset", so stop here.
            if not asset_ids:
                return []
        real = self.repo.fetch_tasks(asset_ids=asset_ids, due_since=start_date, due_until=end_date)

        # Projecting reads each series' full history, with no date bound, to
        # find its last completed occurrence -- that anchor is often months
        # before the window being looked at. That read is only bounded by the
        # asset filter, so without one (no ?assets= at all, meaning "every
        # asset") it would be the whole table on every request. The page
        # always sends its selection; an unfiltered request gets real rows
        # only.
        if asset_ids is None:
            return real

        history = self.repo.fetch_tasks(asset_ids=asset_ids)
        projected = self._project_future_events(history, start_date, end_date)

        combined = real + projected
        combined.sort(key=lambda row: row["due_date"] or "")
        return combined

    def last_completed(
        self, asset_ids: list[str], exclude: list[str] | None = None
    ) -> dict[str, Any] | None:
        """The most recently completed PM for a chip, or None if there isn't one.

        A parent's chip stands for its whole branch everywhere else on the
        page, so it does here too: the answer can be one of its sub-assets'
        PMs. Only real rows are ever considered -- an estimate is never
        completed. An empty selection answers None without opening the
        database, the same way events() and summary() treat one.
        """

        if not asset_ids:
            return None
        self._ensure_schema()
        # Only what the chip is currently showing: a sub-asset unticked in
        # its expanded view isn't a candidate.
        return self.repo.fetch_last_completed(self._selection(asset_ids, exclude))

    def summary(
        self, asset_ids: list[str] | None = None, exclude: list[str] | None = None
    ) -> dict[str, Any]:
        empty = {"scheduled": 0, "completed": 0, "overdue": 0, "compliance": 0.0}
        if asset_ids is not None and len(asset_ids) == 0:
            return empty

        self._ensure_schema()
        if asset_ids is not None:
            asset_ids = self._selection(asset_ids, exclude)
            if not asset_ids:
                return empty
        today = _today().isoformat()
        year_start = _today().replace(month=1, day=1).isoformat()
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