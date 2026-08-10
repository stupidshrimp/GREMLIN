"""Assembles the Availability card's payload from storage and the calculator.

Thin orchestration only: load configuration, resolve the month window, load
work orders, compute, and shape the result for JSON. The arithmetic lives in
``availability_service`` and the storage in
``repositories.availability_repo``; keeping this layer free of both makes each
testable on its own.

Every call recomputes. There is no cache and no recalculate button -- see
``docs/availability-dashboard-design.md`` §2.5.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

# The storage layer's error type, imported for its HTTP meaning rather than for
# any storage: app.py already maps it to a 400, which is what a request naming a
# group, asset or month that does not exist deserves.
from repositories.availability_repo import AvailabilityConfigError
from services.availability_service import (
    DEFAULT_GOAL_PERCENT,
    DEFAULT_WINDOW_MONTHS,
    MONTH_LABELS,
    build_series,
    compute_rows,
    resolve_window,
    work_order_contributions,
)

# Guard rails for the window control. One month is the smallest useful chart;
# thirty-six keeps a stray query string from asking for centuries of columns.
MIN_WINDOW_MONTHS = 1
MAX_WINDOW_MONTHS = 36


def clamp_window_months(value: Any, default: int = DEFAULT_WINDOW_MONTHS) -> int:
    """Coerce a requested window length into the supported range."""

    try:
        months = int(value)
    except (TypeError, ValueError):
        return default
    return max(MIN_WINDOW_MONTHS, min(MAX_WINDOW_MONTHS, months))


def _iso(value: date | None) -> str | None:
    return value.isoformat() if value else None


def _empty_reason(repository, charted: list[str], earliest: date | None) -> str:
    """Explain an empty card in terms of the thing the user would need to fix.

    Three different situations produce no charts, and they need three different
    answers: nothing imported, nothing configured, or nothing complete yet.
    Reporting the first for all of them sends someone to re-run a Limble sync
    when the real problem is that no asset group has been set up.
    """

    if not charted:
        return "No asset groups are configured with assets yet."
    if earliest is None:
        if repository.has_any_work_orders():
            return (
                "No work orders were found for the configured asset groups. "
                "Check that the asset numbers in Settings match the ones in Limble."
            )
        return "No work orders have been imported yet."
    return "No complete month of work-order data is available yet."


def build_dashboard(repository, *, months: int = DEFAULT_WINDOW_MONTHS, today: date | None = None) -> dict:
    """Compute every group's chart series for the requested month window."""

    # The plant's clock, not the server's: whether a month has finished must be
    # judged on the same timezone the work orders are bucketed by, or a UTC
    # server charts July as complete while the plant is still working July 31.
    today = today or repository.plant_today()
    months = clamp_window_months(months)

    groups = repository.load_groups()
    included = [group for group in groups if group.include]
    charted = sorted({asset for group in included for asset in group.asset_numbers})
    linked_rules = repository.load_linked_rules()

    # Work orders must be loaded for a wider set than the charts draw. A linked
    # rule can point at an asset that is not a member of any included group --
    # after it is removed from a group's membership, or after a group is
    # excluded -- and if its downtime is never loaded, the still-active rule
    # silently contributes zero instead of the parent's real linked hours.
    scope = sorted(set(charted) | {
        rule.linked_asset_number for rule in linked_rules if rule.impact_factor > 0
    })
    work_orders = repository.load_work_orders(scope)

    # The window follows the assets that are actually charted, so a stale rule
    # pointing at a decommissioned asset cannot stretch the axis into months
    # nothing on screen has data for.
    charted_months = sorted({
        date(order.created_local.year, order.created_local.month, 1)
        for order in work_orders
        if order.asset_number in set(charted)
    })
    earliest = charted_months[0] if charted_months else None
    latest = charted_months[-1] if charted_months else None
    window = resolve_window(today, months=months, data_earliest=earliest, data_latest=latest)

    # With nothing to compute from, every asset would come out at 100% and the
    # page would show nine charts of flat perfection. "No downtime recorded" and
    # "no data" are the same arithmetic and very different facts, so show an
    # empty state instead -- but name the right fact. A database holding work
    # orders for assets nobody has added to a group yet is a configuration gap,
    # not a missing import, and telling the user nothing has been imported would
    # send them to fix the wrong thing.
    if earliest is None or not window:
        return {
            "months": [],
            "month_labels": [],
            "window_months": months,
            "groups": [],
            "all_asset_numbers": charted,
            "timezone": repository.load_timezone(),
            "timezone_warning": repository.timezone_warning(),
            "generated_at": datetime.now().replace(microsecond=0).isoformat(),
            "data_earliest": _iso(earliest),
            "data_latest": _iso(latest),
            "empty_reason": _empty_reason(repository, charted, earliest),
        }

    rows = compute_rows(
        included,
        window,
        work_orders,
        linked_rules=linked_rules,
        display_names=repository.load_display_names(),
        manual_ot=repository.load_manual_ot(),
    )
    series = build_series(rows, included, window, goals=repository.load_goals())

    detail: dict[str, list[dict]] = {}
    for row in rows:
        detail.setdefault(row.asset_group, []).append(
            {
                "asset_number": row.asset_number,
                "display_name": row.display_name,
                "month": row.month.isoformat(),
                "month_label": row.month_label,
                "scheduled_hours": round(row.scheduled_hours, 2),
                "manual_ot_hours": round(row.manual_ot_hours, 2),
                "adjusted_scheduled_hours": round(row.adjusted_scheduled_hours, 2),
                "direct_downtime_hours": round(row.direct_downtime_hours, 2),
                "linked_downtime_hours": round(row.linked_downtime_hours, 2),
                "adjusted_downtime_hours": round(row.adjusted_downtime_hours, 2),
                "availability": row.availability,
                "flagged": row.flagged,
                "overlap_count": row.overlap_count,
                "total_wo_count": row.total_wo_count,
                "zero_downtime_wo_count": row.zero_downtime_wo_count,
                "no_wo_entries": row.no_wo_entries,
                "note": row.note,
                "downtime_logic": row.downtime_logic,
            }
        )

    return {
        "months": [m.isoformat() for m in window],
        "month_labels": [s.month_labels for s in series][0] if series else [],
        "window_months": months,
        "timezone": repository.load_timezone(),
        "timezone_warning": repository.timezone_warning(),
        "generated_at": datetime.now().replace(microsecond=0).isoformat(),
        "data_earliest": _iso(earliest),
        "data_latest": _iso(latest),
        "all_asset_numbers": charted,
        "groups": [
            {
                "asset_group": chart.asset_group,
                "net_scheduled_hours_per_day": chart.net_scheduled_hours_per_day,
                "month_labels": chart.month_labels,
                "assets": chart.assets,
                "average": chart.average,
                "goal": chart.goal,
                "rows": detail.get(chart.asset_group, []),
            }
            for chart in series
        ],
    }


def parse_month(value: Any) -> date:
    """Coerce a request's month parameter to the first of that month.

    Accepts ``YYYY-MM`` and any full ISO date, so a caller can pass back either
    the month key the dashboard emitted or the bare month a human would type.
    """

    text = str(value or "").strip()
    if not text:
        raise AvailabilityConfigError("A 'month' is required.")
    try:
        parsed = date.fromisoformat(text if len(text) > 7 else f"{text}-01")
    except ValueError as exc:
        raise AvailabilityConfigError(
            f"'{text}' is not a month. Expected YYYY-MM or YYYY-MM-DD."
        ) from exc
    return date(parsed.year, parsed.month, 1)


def _wo_json(contribution) -> dict:
    """One work order row, as the drill-down table reads it."""

    order = contribution.order
    detail = contribution.detail
    return {
        "task_id": order.task_id,
        "asset_number": order.asset_number,
        "asset_name": detail.asset_name,
        "task_name": detail.task_name,
        "status": detail.status,
        "type_raw": detail.type_raw,
        "record_class": detail.record_class,
        "description": detail.description,
        "completion_notes": detail.completion_notes,
        "created": order.created_local.isoformat(),
        "completed": order.completed_local.isoformat() if order.completed_local else None,
        "downtime_hours": round(order.downtime_hours, 4),
        "counted_hours": round(contribution.counted_hours, 4),
        "source": contribution.source,
        "impact_factor": contribution.impact_factor,
        "crosses_month": contribution.crosses_month,
    }


def build_work_order_detail(
    repository, *, asset_group: str, asset_number: str, month: date | str
) -> dict:
    """The work orders behind a single bar, with the totals they add up to.

    Loaded on demand rather than shipped with the chart payload: this is one
    asset-month out of the several hundred the card draws, and carrying every
    work order's name, description and completion notes for all of them would
    cost far more than the numbers themselves.

    The asset-month totals are recomputed here through :func:`compute_rows`
    rather than read back from the caller, so the header of this view and the
    bar it explains are produced by the same code and cannot drift apart.
    """

    month = month if isinstance(month, date) else parse_month(month)
    asset = str(asset_number or "").strip()
    if not asset:
        raise AvailabilityConfigError("An 'asset_number' is required.")

    group = next(
        (g for g in repository.load_groups() if g.asset_group == asset_group and g.include), None
    )
    if group is None:
        raise AvailabilityConfigError(f"'{asset_group}' is not an included asset group.")
    if asset not in {str(a).strip() for a in group.asset_numbers}:
        raise AvailabilityConfigError(f"Asset {asset} is not in {group.asset_group}.")

    # Only this asset's own rules matter, but its linked assets' work orders do:
    # a rule can point outside the group, or at an asset no group holds any more
    # (design doc §2.4), and leaving those unloaded would show a bar explained by
    # rows that sum to less than it.
    rules = [
        rule
        for rule in repository.load_linked_rules()
        if str(rule.parent_asset_number).strip() == asset and rule.impact_factor > 0
    ]
    scope = sorted({asset} | {str(rule.linked_asset_number).strip() for rule in rules})
    details = repository.load_work_order_details(scope, month)
    display_names = repository.load_display_names()

    rows = compute_rows(
        [group],
        [month],
        [detail.order for detail in details],
        linked_rules=rules,
        display_names=display_names,
        manual_ot=repository.load_manual_ot(),
    )
    # Every asset in the group gets a row, but only this one's inputs were
    # loaded; the rest are computed from nothing and are not ours to report.
    row = next(candidate for candidate in rows if candidate.asset_number == asset)

    contributions = work_order_contributions(asset, month, details, linked_rules=rules)
    linked_assets = sorted({str(rule.linked_asset_number).strip() for rule in rules})

    return {
        "asset_group": group.asset_group,
        "asset_number": asset,
        "display_name": row.display_name,
        "month": month.isoformat(),
        # Always carries the year. A single-month view has no neighbouring
        # columns to date it by, so the window's bare "Jun" would be ambiguous
        # in a way it is not on the axis it came from.
        "month_label": f"{MONTH_LABELS[month.month - 1]} {month.year}",
        "scheduled_hours": round(row.scheduled_hours, 2),
        "manual_ot_hours": round(row.manual_ot_hours, 2),
        "adjusted_scheduled_hours": round(row.adjusted_scheduled_hours, 2),
        "direct_downtime_hours": round(row.direct_downtime_hours, 2),
        "linked_downtime_hours": round(row.linked_downtime_hours, 2),
        "adjusted_downtime_hours": round(row.adjusted_downtime_hours, 2),
        "availability": row.availability,
        "flagged": row.flagged,
        "goal": repository.load_goals().get((group.asset_group, month), DEFAULT_GOAL_PERCENT),
        "net_scheduled_hours_per_day": group.net_scheduled_hours_per_day,
        "total_wo_count": row.total_wo_count,
        "zero_downtime_wo_count": row.zero_downtime_wo_count,
        "overlap_count": row.overlap_count,
        "note": row.note,
        # Named so the view can say *why* a machine the reader did not click on
        # is in the list, without the client having to fetch the rule set.
        "linked_assets": [
            {
                "asset_number": linked,
                "display_name": display_names.get(linked, linked),
                "impact_factor": next(
                    rule.impact_factor
                    for rule in rules
                    if str(rule.linked_asset_number).strip() == linked
                ),
            }
            for linked in linked_assets
        ],
        "work_orders": [_wo_json(contribution) for contribution in contributions],
    }


def build_config(repository) -> dict:
    """The editable configuration behind the card, for the Settings page."""

    groups = repository.load_groups()
    display_names = repository.load_display_names()
    return {
        "timezone": repository.load_timezone(),
        "groups": [
            {
                "asset_group": group.asset_group,
                "asset_numbers": list(group.asset_numbers),
                "schedule_hours_per_day": group.schedule_hours_per_day,
                "break_hours_per_day": group.break_hours_per_day,
                "lunch_hours_per_day": group.lunch_hours_per_day,
                "setup_hours_per_day": group.setup_hours_per_day,
                "net_scheduled_hours_per_day": group.net_scheduled_hours_per_day,
                "include": group.include,
                "notes": group.notes,
                "sort_order": group.sort_order,
            }
            for group in groups
        ],
        "display_names": display_names,
        "linked_rules": [
            {
                "rule_group": rule.rule_group,
                "parent_asset_number": rule.parent_asset_number,
                "linked_asset_number": rule.linked_asset_number,
                "impact_factor": rule.impact_factor,
            }
            for rule in repository.load_linked_rules()
        ],
        "goals": [
            {"asset_group": group, "month": month.isoformat(), "goal_percent": value}
            for (group, month), value in sorted(repository.load_goals().items())
        ],
        "manual_ot": [
            {"asset_number": asset, "month": month.isoformat(), "hours": value}
            for (asset, month), value in sorted(repository.load_manual_ot().items())
        ],
    }
