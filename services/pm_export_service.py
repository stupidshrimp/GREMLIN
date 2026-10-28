"""The manager export: one report model, rendered as HTML and as PDF.

The PM calendar page answers "what is happening on these machines" for
someone who already knows how it works. This answers a narrower pair of
questions for someone who does not: **when was PM due on these parts of the
equipment, and when was it last completed?** It is meant to be attached to an
email and read once, so it carries no filters, no sync button and no jargon
that only means something next to the live page.

Everything is computed once, here, into a plain dictionary -- the "report
model". The HTML template renders that model, and the PDF is that same HTML
printed by a browser. There is deliberately no second layout: the moment the
PDF is laid out separately from the page, the two start to disagree about
what a figure means and nobody notices for months. See
`services/html_to_pdf.py`.

Two row sets feed the report, and keeping them straight is the whole trick:

* **The record** -- every PM whose *due date* falls inside the window. This
  is the spine of the report, because "was this done when it should have
  been" is a question about the schedule, and crediting a window with a PM
  that was merely finished inside it would count last year's backlog as this
  year's compliance. It drives the figures, the per-asset summary, the
  monthly chart and the detail table.

* **The calendar** -- whatever the live page would draw for those months,
  which means by *effective date*: a completed PM on the day it was done, an
  outstanding one on the day it is due. Read straight from
  `PmCalendarService.events`, so the grids in the report match the page a
  reader might go and look at afterwards.

The two sets differ, on purpose, and a PM done seven months late appears in
May's table row and on October's calendar. The report says so in a line of
its own rather than leaving a manager to discover it.
"""

from __future__ import annotations

import calendar as calendar_module
from datetime import date, timedelta
from typing import Any, Iterable

from services import pm_calendar_service as calendar
from services.pm_calendar_service import PmCalendarService, _NO_CADENCE

# Reached through the module rather than imported by name, and deliberately.
# `from ... import _today` binds the function object at import time, so a test
# that pins the clock on the calendar service would not be pinning it here --
# the export would report against the real date while everything it reads
# reported against the fixed one, and the two would disagree in ways that look
# like arithmetic bugs. The same goes for the cadence helpers: one module owns
# what a frequency code means, and this reads it from there every time.


def _today() -> date:
    return calendar._today()


def _only_codes(rows: list[dict[str, Any]], codes: list[str] | None) -> list[dict[str, Any]]:
    return calendar._only_codes(rows, codes)


def _code_of(task_name: Any) -> str | None:
    return calendar._cadence_code(task_name if isinstance(task_name, str) else None)

# What each cadence code is called in a report nobody should have to decode.
CADENCE_WORDS: dict[str, str] = {
    "D": "Daily",
    "W": "Weekly",
    "2W": "Every two weeks",
    "M": "Monthly",
    "2M": "Every two months",
    "3M": "Every three months",
    "Q": "Quarterly",
    "SA": "Semi-annual",
    "A": "Annual",
    "2Y": "Every two years",
    "3Y": "Every three years",
    "4Y": "Every four years",
    "5Y": "Every five years",
    _NO_CADENCE: "No stated frequency",
}

# How a PM's outcome is described, in the order a reader cares about.
ON_TIME = "on_time"
LATE = "late"
MISSED = "missed"
OPEN = "open"
ESTIMATED = "estimated"

STATUS_WORDS: dict[str, str] = {
    ON_TIME: "Done on time",
    LATE: "Done late",
    MISSED: "Not done",
    OPEN: "Not yet due",
    ESTIMATED: "Expected",
}

# How far past the window the "what is coming next" section looks. A year
# covers every cadence the calendar projects except the multi-year ones,
# and a manager asking "what is next" is not asking about 2029.
_LOOKAHEAD_DAYS = 365

# The windows the dialog offers as buttons. Anything else arrives as an
# explicit start and end.
PRESETS = ("last_6_months", "last_12_months", "year_to_date", "this_year")


def _iso(value: Any) -> str:
    """The date part of a stored timestamp, or "" when there isn't one."""

    return (value or "")[:10] if isinstance(value, str) else ""


def _as_date(value: Any) -> date | None:
    text = _iso(value)
    if not text:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _month_key(iso: str) -> str:
    return iso[:7]


def _month_label(key: str) -> str:
    year, month = int(key[:4]), int(key[5:7])
    return f"{calendar_module.month_name[month]} {year}"


def _end_of_month(day: date) -> date:
    return day.replace(day=calendar_module.monthrange(day.year, day.month)[1])


def window_for(
    preset: str | None = None,
    start: str | None = None,
    end: str | None = None,
    today: date | None = None,
) -> tuple[str, str, str]:
    """Resolve the report window to (start, end, label), whole months.

    A preset wins over explicit dates, and an unknown preset falls back to
    the last six months rather than failing -- the export is a read, and a
    report of the wrong six months is a better answer to a typo in a query
    string than a 400.

    Both ends are snapped to month boundaries. The calendar grids are drawn
    a month at a time, and a window ending on the 12th draws a half month
    that reads as though nothing was scheduled after it.
    """

    today = today or _today()
    if preset == "year_to_date":
        first, last = today.replace(month=1, day=1), today
    elif preset == "this_year":
        first, last = today.replace(month=1, day=1), today.replace(month=12, day=31)
    elif preset == "last_12_months":
        first, last = _shift_months(today.replace(day=1), -11), today
    elif preset == "last_6_months" or (preset and preset not in PRESETS):
        first, last = _shift_months(today.replace(day=1), -5), today
    else:
        first = _as_date(start) or _shift_months(today.replace(day=1), -5)
        last = _as_date(end) or today
    if last < first:
        first, last = last, first
    first = first.replace(day=1)
    last = _end_of_month(last)
    if first == last.replace(day=1):
        label = _month_label(first.isoformat()[:7])
    else:
        label = f"{_month_label(first.isoformat()[:7])} to {_month_label(last.isoformat()[:7])}"
    return first.isoformat(), last.isoformat(), label


def _shift_months(day: date, months: int) -> date:
    """`day` moved by whole months, clamped to the target month's length."""

    index = day.year * 12 + (day.month - 1) + months
    year, month = divmod(index, 12)
    month += 1
    return date(year, month, min(day.day, calendar_module.monthrange(year, month)[1]))


def _months_between(start: str, end: str) -> list[str]:
    first = date.fromisoformat(start).replace(day=1)
    last = date.fromisoformat(end).replace(day=1)
    keys = []
    cursor = first
    while cursor <= last:
        keys.append(cursor.isoformat()[:7])
        cursor = _shift_months(cursor, 1)
    return keys


def _outcome(row: dict[str, Any], today: date) -> tuple[str, int]:
    """How a PM due in the window turned out, and by how many days.

    The number is days late for one that was completed after its date, days
    outstanding for one that is late and still not done, and 0 otherwise --
    so a reader never has to work out which sign means what.
    """

    due = _as_date(row.get("due_date"))
    done = _as_date(row.get("completed_date"))
    if done and due:
        late_by = (done - due).days
        return (LATE, late_by) if late_by > 0 else (ON_TIME, 0)
    if done:
        # No due date on file. It was done, and there is nothing to have
        # been late against, so it counts as met rather than as a problem.
        return ON_TIME, 0
    if due and due < today:
        return MISSED, (today - due).days
    return OPEN, 0


class PmExportService:
    """Builds the manager report from the calendar's own reads.

    Holds no database of its own: every figure comes through
    `PmCalendarService`, so the export cannot quietly diverge from the page.
    """

    def __init__(self, calendar: PmCalendarService) -> None:
        self.calendar = calendar

    # ------------------------------------------------------------------

    def report(
        self,
        asset_ids: list[str],
        *,
        exclude: list[str] | None = None,
        codes: list[str] | None = None,
        preset: str | None = None,
        start: str | None = None,
        end: str | None = None,
    ) -> dict[str, Any]:
        """The whole report, as plain data both renderers read."""

        today = _today()
        window_start, window_end, window_label = window_for(preset, start, end, today)
        selection = self.calendar.resolve_assets(asset_ids, exclude)

        assets = self._assets(selection)
        if not selection:
            return self._empty(window_start, window_end, window_label, codes, today, assets)

        # The record: due inside the window, whatever happened to it after.
        due_rows = _only_codes(
            self.calendar.repo.fetch_tasks(
                asset_ids=selection,
                due_since=window_start,
                due_until=window_end,
                date_field="due_date",
            ),
            codes,
        )
        for row in due_rows:
            row["outcome"], row["days"] = _outcome(row, today)

        # The calendar: what the live page would draw for these months.
        drawn = self.calendar.events(
            asset_ids=asset_ids,
            start_date=window_start,
            end_date=window_end,
            exclude=exclude,
            codes=codes,
        )

        return {
            "generated_on": today.isoformat(),
            "window": {
                "start": window_start,
                "end": window_end,
                "label": window_label,
                "ends_in_future": date.fromisoformat(window_end) > today,
            },
            "cadences": self._cadences(codes),
            "assets": assets,
            "totals": self._totals(due_rows),
            "per_asset": self._per_asset(due_rows, assets, selection, codes),
            "by_month": self._by_month(due_rows, window_start, window_end),
            "calendar": self._calendar(drawn, window_start, window_end, today),
            "rows": self._detail_rows(due_rows),
            "overdue": self._overdue(asset_ids, exclude, codes, today),
            "upcoming": self._upcoming(asset_ids, exclude, codes, today),
        }

    # ------------------------------------------------------------------

    def _empty(
        self,
        window_start: str,
        window_end: str,
        window_label: str,
        codes: list[str] | None,
        today: date,
        assets: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """A report with nothing picked, which still has to render.

        Every asset hidden, or none chosen, is a thing a person can do in the
        dialog. It produces a report that says so, rather than an exception.
        """

        return {
            "generated_on": today.isoformat(),
            "window": {
                "start": window_start,
                "end": window_end,
                "label": window_label,
                "ends_in_future": date.fromisoformat(window_end) > today,
            },
            "cadences": self._cadences(codes),
            "assets": assets,
            "totals": self._totals([]),
            "per_asset": [],
            "by_month": self._by_month([], window_start, window_end),
            "calendar": [],
            "rows": [],
            "overdue": {"total": 0, "pms": []},
            "upcoming": [],
        }

    def _assets(self, selection: Iterable[str]) -> list[dict[str, Any]]:
        """Name and number for each selected asset, in name order."""

        picked = set(selection)
        if not picked:
            return []
        rows = [
            {
                "asset_id": str(row["asset_id"]),
                "asset_name": row.get("asset_name") or f"Asset {row['asset_id']}",
            }
            for row in self.calendar.repo.fetch_assets()
            if str(row["asset_id"]) in picked
        ]
        found = {row["asset_id"] for row in rows}
        # An asset with PM rows but no row in pm_asset -- possible on a
        # database synced before the asset tree was stored. Better a line
        # with an id on it than a silently shorter report.
        rows.extend(
            {"asset_id": asset_id, "asset_name": f"Asset {asset_id}"}
            for asset_id in sorted(picked - found)
        )
        return sorted(rows, key=lambda row: row["asset_name"].lower())

    def _cadences(self, codes: list[str] | None) -> dict[str, Any]:
        if not codes:
            return {"codes": [], "label": "All frequencies", "filtered": False}
        words = [CADENCE_WORDS.get(code.upper(), code.upper()) for code in codes]
        return {
            "codes": [code.upper() for code in codes],
            "label": ", ".join(words),
            "filtered": True,
        }

    def _totals(self, rows: list[dict[str, Any]]) -> dict[str, Any]:
        counts = {ON_TIME: 0, LATE: 0, MISSED: 0, OPEN: 0}
        late_days: list[int] = []
        for row in rows:
            counts[row["outcome"]] += 1
            if row["outcome"] == LATE:
                late_days.append(row["days"])
        due = len(rows)
        completed = counts[ON_TIME] + counts[LATE]
        # Measured against what has actually come due. Counting PMs that are
        # not due yet as failures would make any window containing future
        # months look like a collapse.
        answerable = due - counts[OPEN]
        return {
            "due": due,
            "completed": completed,
            "on_time": counts[ON_TIME],
            "late": counts[LATE],
            "missed": counts[MISSED],
            "open": counts[OPEN],
            "answerable": answerable,
            "completion_rate": round(completed / answerable * 100, 1) if answerable else None,
            "on_time_rate": round(counts[ON_TIME] / answerable * 100, 1) if answerable else None,
            "average_days_late": round(sum(late_days) / len(late_days), 1) if late_days else None,
            "worst_days_late": max(late_days) if late_days else None,
        }

    def _per_asset(
        self,
        rows: list[dict[str, Any]],
        assets: list[dict[str, Any]],
        selection: list[str],
        codes: list[str] | None,
    ) -> list[dict[str, Any]]:
        """One line per asset: what was due, what happened, when it last ran.

        "Last completed" deliberately ignores the window. The question it
        answers -- "when was this last done?" -- is not about the reporting
        period, and an asset with nothing in the window is exactly the one a
        reader most wants that date for.
        """

        grouped: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            grouped.setdefault(str(row.get("asset_id")), []).append(row)

        last_done = self._last_completed_by_asset(selection, codes)

        out = []
        for asset in assets:
            asset_id = asset["asset_id"]
            mine = grouped.get(asset_id, [])
            totals = self._totals(mine)
            last = last_done.get(asset_id)
            out.append(
                {
                    **asset,
                    "due": totals["due"],
                    "completed": totals["completed"],
                    "on_time": totals["on_time"],
                    "late": totals["late"],
                    "missed": totals["missed"],
                    "open": totals["open"],
                    "completion_rate": totals["completion_rate"],
                    "average_days_late": totals["average_days_late"],
                    "last_completed_date": _iso(last.get("completed_date")) if last else None,
                    "last_completed_name": last.get("task_name") if last else None,
                }
            )
        return out

    def _last_completed_by_asset(
        self, selection: list[str], codes: list[str] | None
    ) -> dict[str, dict[str, Any]]:
        """The newest completion on each selected asset, over all of history.

        Read through `fetch_tasks` with no date bound, which is the whole
        history of the selection rather than of the account: the export runs
        against assets somebody picked, and that is what keeps this bounded.
        A cadence filter has to be applied here in Python, since no SQL can
        parse a frequency out of a task name.
        """

        if not selection:
            return {}
        newest: dict[str, dict[str, Any]] = {}
        for row in _only_codes(self.calendar.repo.fetch_tasks(asset_ids=selection), codes):
            completed = _iso(row.get("completed_date"))
            if not completed:
                continue
            asset_id = str(row.get("asset_id"))
            held = newest.get(asset_id)
            if held is None or completed > _iso(held.get("completed_date")):
                newest[asset_id] = row
        return newest

    def _by_month(
        self, rows: list[dict[str, Any]], window_start: str, window_end: str
    ) -> list[dict[str, Any]]:
        """The monthly chart series, by due month, every month present."""

        buckets = {
            key: {ON_TIME: 0, LATE: 0, MISSED: 0, OPEN: 0}
            for key in _months_between(window_start, window_end)
        }
        for row in rows:
            key = _month_key(_iso(row.get("due_date")))
            if key in buckets:
                buckets[key][row["outcome"]] += 1
        series = []
        for key, counts in buckets.items():
            due = sum(counts.values())
            answerable = due - counts[OPEN]
            series.append(
                {
                    "key": key,
                    "label": _month_label(key),
                    "short": f"{calendar_module.month_abbr[int(key[5:7])]} {key[2:4]}",
                    "due": due,
                    "on_time": counts[ON_TIME],
                    "late": counts[LATE],
                    "missed": counts[MISSED],
                    "open": counts[OPEN],
                    "on_time_rate": (
                        round(counts[ON_TIME] / answerable * 100) if answerable else None
                    ),
                }
            )
        return series

    def _calendar(
        self,
        drawn: list[dict[str, Any]],
        window_start: str,
        window_end: str,
        today: date,
    ) -> list[dict[str, Any]]:
        """The month grids, carrying only days that have something on them.

        A printed month grid is mostly empty cells -- this account averages
        PMs on a handful of days a month -- and six blank weeks per month
        turns a readable report into a stack of mostly-white pages. So each
        month lists its active days in order, and says how many days it
        skipped, which is information rather than the absence of it.
        """

        by_month: dict[str, dict[str, dict[str, Any]]] = {}
        for event in drawn:
            iso = _iso(event.get("effective_date")) or _iso(event.get("due_date"))
            if not iso or not (window_start <= iso <= window_end):
                continue
            day = by_month.setdefault(_month_key(iso), {}).setdefault(
                iso, {"date": iso, "items": []}
            )
            day["items"].append(self._calendar_item(event, today))

        # Months with nothing in them are collapsed into one line between the
        # months that do. A twelve-month report on a semi-annual asset is
        # otherwise ten identical "no PM activity this month" blocks, which
        # pushes the months that matter onto later pages and reads as though
        # the report is padding itself.
        blocks: list[dict[str, Any]] = []
        quiet: list[str] = []

        def flush_quiet() -> None:
            if not quiet:
                return
            first, last = _month_label(quiet[0]), _month_label(quiet[-1])
            blocks.append(
                {
                    "kind": "quiet",
                    "count": len(quiet),
                    "label": first if len(quiet) == 1 else f"{first} to {last}",
                }
            )
            quiet.clear()

        for key in _months_between(window_start, window_end):
            days = by_month.get(key, {})
            if not days:
                quiet.append(key)
                continue
            flush_quiet()
            in_month = calendar_module.monthrange(int(key[:4]), int(key[5:7]))[1]
            ordered = []
            for iso in sorted(days):
                day = days[iso]
                moment = date.fromisoformat(iso)
                day["day"] = moment.day
                day["weekday"] = calendar_module.day_abbr[moment.weekday()]
                day["items"].sort(key=lambda item: (item["asset_name"], item["name"]))
                ordered.append(day)
            counts = {ON_TIME: 0, LATE: 0, MISSED: 0, OPEN: 0, ESTIMATED: 0}
            for day in ordered:
                for item in day["items"]:
                    counts[item["status"]] += 1
            blocks.append(
                {
                    "kind": "month",
                    "key": key,
                    "label": _month_label(key),
                    "days": ordered,
                    "active_days": len(ordered),
                    "quiet_days": in_month - len(ordered),
                    "total": sum(counts.values()),
                    "counts": counts,
                }
            )
        flush_quiet()
        return blocks

    def _calendar_item(self, event: dict[str, Any], today: date) -> dict[str, Any]:
        if event.get("is_projected"):
            status = ESTIMATED
            days = 0
        else:
            status, days = _outcome(event, today)
        due = _iso(event.get("due_date"))
        done = _iso(event.get("completed_date"))
        return {
            "name": event.get("task_name") or "PM",
            "asset_name": event.get("asset_name") or "",
            "asset_id": str(event.get("asset_id") or ""),
            "cadence": self._cadence_word(event.get("task_name")),
            "status": status,
            "status_word": STATUS_WORDS[status],
            "due_date": due,
            "completed_date": done,
            "days": days,
            "estimated": bool(event.get("is_projected")),
        }

    def _cadence_word(self, task_name: Any) -> str:
        code = _code_of(task_name)
        return CADENCE_WORDS.get(code or _NO_CADENCE, CADENCE_WORDS[_NO_CADENCE])

    def _detail_rows(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """The table: every PM due in the window, worst outcome first.

        Ordered so the rows a manager is looking for are at the top -- not
        done, then late by the most days, then on time -- rather than by a
        date they would have to scan for.
        """

        rank = {MISSED: 0, LATE: 1, OPEN: 2, ON_TIME: 3}
        detail = [
            {
                "asset_name": row.get("asset_name") or "",
                "asset_id": str(row.get("asset_id") or ""),
                "name": row.get("task_name") or "PM",
                "cadence": self._cadence_word(row.get("task_name")),
                "due_date": _iso(row.get("due_date")),
                "completed_date": _iso(row.get("completed_date")),
                "status": row["outcome"],
                "status_word": STATUS_WORDS[row["outcome"]],
                "days": row["days"],
                "task_id": str(row.get("task_id") or ""),
            }
            for row in rows
        ]
        detail.sort(
            key=lambda row: (
                rank[row["status"]],
                -row["days"],
                row["asset_name"].lower(),
                row["due_date"],
            )
        )
        return detail

    def _overdue(
        self,
        asset_ids: list[str],
        exclude: list[str] | None,
        codes: list[str] | None,
        today: date,
    ) -> dict[str, Any]:
        """The backlog as it stands now, oldest first.

        Taken from the page's own `overdue`, so the report and the figure on
        the page cannot disagree. It is "right now", not "at the end of the
        window" -- a manager reading a report wants to know what is
        outstanding today.
        """

        found = self.calendar.overdue(asset_ids, exclude=exclude, codes=codes)
        pms = []
        for row in found.get("pms", []):
            due = _iso(row.get("due_date"))
            pms.append(
                {
                    "asset_name": row.get("asset_name") or "",
                    "asset_id": str(row.get("asset_id") or ""),
                    "name": row.get("task_name") or "PM",
                    "cadence": self._cadence_word(row.get("task_name")),
                    "due_date": due,
                    "days": (today - date.fromisoformat(due)).days if due else 0,
                }
            )
        pms.sort(key=lambda row: -row["days"])
        return {"total": found.get("total", len(pms)), "pms": pms}

    def _upcoming(
        self,
        asset_ids: list[str],
        exclude: list[str] | None,
        codes: list[str] | None,
        today: date,
    ) -> list[dict[str, Any]]:
        """The next PM due on each asset, looking a year past today.

        Includes the calendar's estimates, marked as estimates, because an
        asset whose next work order has not been raised yet is precisely the
        one a reader would otherwise read as "nothing due".
        """

        horizon = (today + timedelta(days=_LOOKAHEAD_DAYS)).isoformat()
        ahead = self.calendar.events(
            asset_ids=asset_ids,
            start_date=today.isoformat(),
            end_date=horizon,
            exclude=exclude,
            codes=codes,
        )
        soonest: dict[str, dict[str, Any]] = {}
        for event in ahead:
            if event.get("completed_date"):
                continue
            due = _iso(event.get("due_date"))
            if not due or due < today.isoformat():
                continue
            asset_id = str(event.get("asset_id") or "")
            held = soonest.get(asset_id)
            if held is None or due < held["due_date"]:
                soonest[asset_id] = {
                    "asset_name": event.get("asset_name") or "",
                    "asset_id": asset_id,
                    "name": event.get("task_name") or "PM",
                    "cadence": self._cadence_word(event.get("task_name")),
                    "due_date": due,
                    "days": (date.fromisoformat(due) - today).days,
                    "estimated": bool(event.get("is_projected")),
                }
        return sorted(soonest.values(), key=lambda row: (row["due_date"], row["asset_name"].lower()))
