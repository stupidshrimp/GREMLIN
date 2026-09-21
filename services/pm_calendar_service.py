"""Service layer for the PM Calendar page.

Bridges Limble and the local PM calendar database: pulls PM tasks from
Limble on a background thread and stores them via PmCalendarRepository, and
serves the reads the calendar page needs (asset list, events for a date
range, YTD summary counts).
"""

from __future__ import annotations

import hashlib
import itertools
import threading
from bisect import bisect_left
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
# These are whole weeks, not calendar months or years, and that is also on
# purpose: it's how this account's templates are set up in Limble
# ("Repeats Every 4 Weeks on Fri", "Every 52 Weeks", "Every 156 Weeks"), so
# it's what Limble will actually generate. It does mean M comes round 13
# times a year rather than 12, and A lands about a day and a quarter
# earlier each year (52 weeks is 364 days); 3Y is 1,092 days against a
# calendar ~1,096. Calendar-month arithmetic would look tidier and disagree
# with Limble. The staleness bound below keeps any projection within a few
# cycles of real activity, so the drift never has years to build up.
_CADENCE_WEEKS: dict[str, int] = {
    "2W": 2,  # Every two weeks
    "M": 4,  # Monthly
    "Q": 12,  # Quarterly
    "SA": 26,  # Semi-annual
    "A": 52,  # Annual
    "3Y": 156,  # Every three years
}


def _cadence_code(task_name: str | None) -> str | None:
    """Pull the recurrence code out of a PM name, e.g. "M" from "3103 - M - ...".

    The second " - "-delimited segment, upper-cased so a stray "m" or "sa"
    still matches. Anything that doesn't split that way, or whose middle
    segment isn't one of _CADENCE_WEEKS, returns None -- silently, on
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
    return code if code in _CADENCE_WEEKS else None


def _series_key(task_name: str) -> str:
    """One PM line's identity: its name, ignoring case and runs of spaces.

    The line, not the cadence code, is what repeats. One asset can carry two
    PMs with the same code -- a monthly on the machine and a monthly on its
    chiller -- and keyed on the code alone their histories were pooled into
    one stream: one anchor, one name, and the other line gone from every
    future month. The name is what tells them apart; case and spacing are
    folded so a stray capital or a double space in Limble doesn't split one
    line in two.
    """

    return " ".join(task_name.split()).casefold()


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

            with self._lock:
                self._job = {
                    "state": STATE_SUCCEEDED,
                    "fetched": len(tasks),
                    "matched_pm_tasks": len(rows),
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
        _CADENCE_WEEKS above) this account's /tasks endpoint doesn't expose.

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
            interval_days = _CADENCE_WEEKS[code] * 7
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
                    }
                )

        return projected

    # ------------------------------------------------------------------
    # Reads for the page
    # ------------------------------------------------------------------
    def asset_options(self) -> list[dict[str, Any]]:
        self._ensure_schema()
        return self.repo.asset_options()

    def events(
        self,
        asset_ids: list[str] | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> list[dict[str, Any]]:
        if asset_ids is not None and len(asset_ids) == 0:
            return []
        self._ensure_schema()
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

    def summary(self, asset_ids: list[str] | None = None) -> dict[str, Any]:
        empty = {"scheduled": 0, "completed": 0, "overdue": 0, "compliance": 0.0}
        if asset_ids is not None and len(asset_ids) == 0:
            return empty

        self._ensure_schema()
        today = date.today().isoformat()
        year_start = date.today().replace(month=1, day=1).isoformat()
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