"""Monthly asset availability, replicating the Availability Dashboard workbook.

Availability answers "what share of its scheduled hours was this asset able to
run?", per asset, per calendar month, grouped into the nine asset groups the
plant reports on.

    availability = (adjusted scheduled hours - adjusted downtime hours)
                   / adjusted scheduled hours

Everything in this module is pure: it takes already-loaded configuration and
already-localized work orders and returns result rows. Nothing is cached and
nothing is written, so every request recomputes against the current schedule
(see ``docs/availability-dashboard-design.md`` §2.5). Loading lives in
``repositories.availability_repo``.

Two behaviours are deliberate and easy to mistake for bugs:

* **Every work order with downtime counts**, whatever its Limble ``type`` or
  its classification. Both earlier implementations filtered and both filtered
  wrongly -- see the design doc §2.1 for the measurements. Callers must not
  pre-filter by PM/corrective either.
* **Linked downtime does not cascade.** A parent's share is computed from each
  linked asset's *direct* downtime, never from an already-adjusted figure.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

MONTH_LABELS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")

# How many months the card shows before the user changes the window.
DEFAULT_WINDOW_MONTHS = 5

# Used for any group/month with no explicit goal row.
DEFAULT_GOAL_PERCENT = 0.95


@dataclass(frozen=True)
class AssetGroup:
    """One reportable group of assets and the shift schedule they share."""

    asset_group: str
    asset_numbers: tuple[str, ...]
    schedule_hours_per_day: float
    break_hours_per_day: float = 0.0
    lunch_hours_per_day: float = 0.0
    setup_hours_per_day: float = 0.0
    include: bool = True
    notes: str = ""
    sort_order: int = 0

    @property
    def net_scheduled_hours_per_day(self) -> float:
        """Hours per day the group is actually expected to produce.

        Mirrors the workbook's ``Configurator!G`` formula
        ``=MAX(0, C - SUM(D:F))``. Net is always derived, never stored or typed,
        so it cannot disagree with the parts it is made of.
        """

        gross = self.schedule_hours_per_day - (
            self.break_hours_per_day + self.lunch_hours_per_day + self.setup_hours_per_day
        )
        return max(0.0, gross)


@dataclass(frozen=True)
class LinkedRule:
    """One asset's downtime charging a share onto a mechanically coupled parent."""

    parent_asset_number: str
    linked_asset_number: str
    impact_factor: float = 0.5
    rule_group: str = ""


@dataclass(frozen=True)
class WorkOrder:
    """A work order carrying downtime, with its created date already localized.

    ``created_local`` and ``completed_local`` are naive local datetimes; the
    repository resolves the plant timezone before constructing these so this
    module never deals with offsets.
    """

    asset_number: str
    created_local: datetime
    downtime_hours: float
    completed_local: datetime | None = None
    task_id: str = ""


@dataclass(frozen=True)
class WorkOrderDetail:
    """One work order plus the descriptive text the drill-down displays.

    Composition rather than extension, and deliberately so. ``WorkOrder``
    carries no type or classification field precisely so that no future change
    can reintroduce the PM filter §2.1 exists to prevent; hanging that text off
    a separate record keeps the guarantee structural. A ``WorkOrderDetail``
    cannot be handed to :func:`compute_rows` even by accident, because it has no
    ``asset_number`` of its own.

    ``description`` is whichever of the description / requestor description /
    request title fields the source system actually filled in -- Limble
    populates a different one depending on whether a work order began as a
    request.
    """

    order: WorkOrder
    task_name: str = ""
    status: str = ""
    type_raw: str = ""
    record_class: str = ""
    asset_name: str = ""
    description: str = ""
    completion_notes: str = ""


@dataclass(frozen=True)
class WorkOrderContribution:
    """One work order as it lands in one asset-month bar.

    ``counted_hours`` is what this order actually added to *that* bar, which is
    not always its own downtime: a linked order contributes only its share, and
    a negative one contributes nothing.
    """

    detail: WorkOrderDetail
    source: str  # "direct" or "linked"
    impact_factor: float
    counted_hours: float
    crosses_month: bool

    @property
    def order(self) -> WorkOrder:
        return self.detail.order


@dataclass(frozen=True)
class AvailabilityRow:
    """One asset in one month -- the equivalent of an Availability Data row."""

    asset_group: str
    asset_number: str
    display_name: str
    month: date
    month_label: str
    scheduled_hours: float
    manual_ot_hours: float
    adjusted_scheduled_hours: float
    direct_downtime_hours: float
    linked_downtime_hours: float
    adjusted_downtime_hours: float
    availability: float | None
    flagged: bool
    overlap_count: int
    total_wo_count: int
    zero_downtime_wo_count: int
    no_wo_entries: bool
    note: str

    @property
    def downtime_logic(self) -> str:
        return "Direct + linked downtime" if self.linked_downtime_hours > 0 else "Direct downtime only"


@dataclass
class GroupSeries:
    """Everything one chart needs: a bar series per asset plus two lines."""

    asset_group: str
    months: list[date]
    month_labels: list[str]
    assets: list[dict] = field(default_factory=list)
    average: list[float | None] = field(default_factory=list)
    goal: list[float | None] = field(default_factory=list)
    net_scheduled_hours_per_day: float = 0.0


# ----------------------------------------------------------------------
# Month arithmetic
# ----------------------------------------------------------------------
def add_months(month: date, delta: int) -> date:
    """Shift a first-of-month date by ``delta`` months."""

    index = (month.year * 12 + month.month - 1) + delta
    return date(index // 12, index % 12 + 1, 1)


def month_span(first: date, last: date) -> list[date]:
    """Every first-of-month from ``first`` through ``last`` inclusive."""

    if last < first:
        return []
    months = []
    cursor = date(first.year, first.month, 1)
    end = date(last.year, last.month, 1)
    while cursor <= end:
        months.append(cursor)
        cursor = add_months(cursor, 1)
    return months


def last_complete_month(today: date) -> date:
    """The most recent month that has finished.

    The current month is always excluded: a partial month has fewer elapsed
    hours than scheduled ones, so it reads artificially low until it closes.
    """

    return add_months(date(today.year, today.month, 1), -1)


def resolve_window(
    today: date,
    *,
    months: int = DEFAULT_WINDOW_MONTHS,
    data_earliest: date | None = None,
    data_latest: date | None = None,
) -> list[date]:
    """Choose which months to chart.

    Defaults to the ``months`` most recent complete months, then clamps to the
    range that actually holds work-order data so a database with three months
    of history shows three columns rather than two empty ones.
    """

    if months < 1:
        return []
    end = last_complete_month(today)
    if data_latest is not None:
        end = min(end, date(data_latest.year, data_latest.month, 1))
    start = add_months(end, -(months - 1))
    if data_earliest is not None:
        start = max(start, date(data_earliest.year, data_earliest.month, 1))
    return month_span(start, end)


def format_month_labels(months: list[date]) -> list[str]:
    """Axis labels for a window, disambiguated when it spans more than a year.

    A window inside one calendar year uses the bare month name, matching the
    workbook. Once it crosses a year boundary the same name repeats -- a
    24-month window shows two columns labelled ``Jun`` -- and those labels head
    the editable Goal and OT columns, so a reader has no way to tell which year
    a cell belongs to.

    The year is then appended to *every* label rather than only the repeated
    ones: a mix of ``Jun`` and ``Jun 25`` reads as two different kinds of thing
    rather than as the same thing disambiguated.
    """

    if not months:
        return []
    if len({month.year for month in months}) == 1:
        return [MONTH_LABELS[month.month - 1] for month in months]
    return [f"{MONTH_LABELS[month.month - 1]} {month.year % 100:02d}" for month in months]


def weekday_count(year: int, month: int) -> int:
    """Mon-Fri days in the month.

    Scheduled hours count weekdays only, regardless of the group's hours per
    day -- which is why Salvagnini's 24 h/day operation is credited 22 x 18 =
    396 h in January 2026. Groups that genuinely run seven days are overstated;
    see the design doc §6.
    """

    days = calendar.monthrange(year, month)[1]
    return sum(1 for day in range(1, days + 1) if date(year, month, day).weekday() < 5)


# ----------------------------------------------------------------------
# Calculation
# ----------------------------------------------------------------------
def _month_key(when: datetime | date) -> date:
    return date(when.year, when.month, 1)


def _crosses_month(order: WorkOrder) -> bool:
    """True when the work order's downtime spans a month boundary.

    Downtime is attributed to the month the work order was *created*, so a
    repair that begins on the 31st and finishes in the next month lands
    entirely in the earlier month. The workbook flags these in its Overlap
    column rather than splitting them, and so do we.
    """

    start = _month_key(order.created_local)
    if order.completed_local is not None and _month_key(order.completed_local) != start:
        return True
    try:
        end = order.created_local + timedelta(hours=order.downtime_hours)
    except OverflowError:
        # A downtime value large enough to overflow a date is certainly not
        # confined to its own month.
        return True
    return _month_key(end) != start


def availability_percent(downtime_hours: float, scheduled_hours: float) -> float | None:
    """Availability for one asset-month, or ``None`` when it is undefined.

    Returns ``None`` rather than 0% or 100% when there are no scheduled hours
    (a group configured down to zero net hours). An asset that was never
    scheduled has no availability to report, and inventing one would either
    look like a total outage or like a perfect month.

    Negative results clamp to 0. Linked downtime can push a Salvagnini asset
    past 100% downtime; the row is flagged rather than drawn below the axis.
    """

    if scheduled_hours <= 0:
        return None
    return max(0.0, (scheduled_hours - downtime_hours) / scheduled_hours)


def compute_rows(
    groups: list[AssetGroup],
    months: list[date],
    work_orders: list[WorkOrder],
    *,
    linked_rules: list[LinkedRule] | None = None,
    display_names: dict[str, str] | None = None,
    manual_ot: dict[tuple[str, date], float] | None = None,
) -> list[AvailabilityRow]:
    """Compute one row per included asset per month.

    ``work_orders`` must be every work order for the assets and months in
    scope, unfiltered by type or classification -- filtering happens nowhere in
    this pipeline (design doc §2.1). Rows with zero or negative downtime still
    matter: they feed the data-quality counts that distinguish "ran perfectly"
    from "nobody logged anything".
    """

    display_names = display_names or {}
    manual_ot = manual_ot or {}
    month_set = set(months)

    # Bucket work orders once by (asset, month); every later lookup is a hit on
    # this index rather than another pass over the full list.
    by_asset_month: dict[tuple[str, date], list[WorkOrder]] = {}
    for order in work_orders:
        asset = str(order.asset_number).strip()
        if not asset:
            continue
        month = _month_key(order.created_local)
        if month not in month_set:
            continue
        by_asset_month.setdefault((asset, month), []).append(order)

    def direct_downtime(asset: str, month: date) -> float:
        # Negative downtime is discarded per order, matching the workbook's
        # `If downtimeSeconds > 0` guard.
        return sum(max(0.0, o.downtime_hours) for o in by_asset_month.get((asset, month), []))

    rules_by_parent: dict[str, list[LinkedRule]] = {}
    for rule in linked_rules or []:
        if rule.impact_factor > 0:
            rules_by_parent.setdefault(str(rule.parent_asset_number).strip(), []).append(rule)

    rows: list[AvailabilityRow] = []
    for group in sorted(groups, key=lambda g: (g.sort_order, g.asset_group)):
        if not group.include:
            continue
        net_hours = group.net_scheduled_hours_per_day
        for asset in group.asset_numbers:
            asset = str(asset).strip()
            if not asset:
                continue
            for month in months:
                orders = by_asset_month.get((asset, month), [])
                scheduled = weekday_count(month.year, month.month) * net_hours
                ot_hours = max(0.0, manual_ot.get((asset, month), 0.0))
                adjusted_scheduled = scheduled + ot_hours

                direct = direct_downtime(asset, month)
                linked = sum(
                    direct_downtime(str(rule.linked_asset_number).strip(), month) * rule.impact_factor
                    for rule in rules_by_parent.get(asset, [])
                )
                adjusted_downtime = direct + linked

                total_wo = len(orders)
                zero_downtime_wo = sum(1 for o in orders if o.downtime_hours == 0)
                if total_wo == 0:
                    note = "No WO entries this month"
                elif zero_downtime_wo:
                    note = f"{zero_downtime_wo} WO(s) with 0 downtime"
                else:
                    note = ""

                rows.append(
                    AvailabilityRow(
                        asset_group=group.asset_group,
                        asset_number=asset,
                        display_name=display_names.get(asset, asset),
                        month=month,
                        month_label=MONTH_LABELS[month.month - 1],
                        scheduled_hours=scheduled,
                        manual_ot_hours=ot_hours,
                        adjusted_scheduled_hours=adjusted_scheduled,
                        direct_downtime_hours=direct,
                        linked_downtime_hours=linked,
                        adjusted_downtime_hours=adjusted_downtime,
                        availability=availability_percent(adjusted_downtime, adjusted_scheduled),
                        flagged=adjusted_downtime > adjusted_scheduled,
                        overlap_count=sum(1 for o in orders if o.downtime_hours > 0 and _crosses_month(o)),
                        total_wo_count=total_wo,
                        zero_downtime_wo_count=zero_downtime_wo,
                        no_wo_entries=total_wo == 0,
                        note=note,
                    )
                )
    return rows


def work_order_contributions(
    asset_number: str,
    month: date,
    details: list[WorkOrderDetail],
    *,
    linked_rules: list[LinkedRule] | None = None,
) -> list[WorkOrderContribution]:
    """Every work order behind one asset-month, and what each one contributed.

    This is the evidence under a single bar. The arithmetic repeats
    :func:`compute_rows` rather than approximating it, because the two must
    agree exactly: summing ``counted_hours`` over the direct rows reproduces
    that month's ``direct_downtime_hours`` and over the linked rows its
    ``linked_downtime_hours``. A drill-down whose rows do not add up to the
    number they explain is worse than no drill-down at all.

    Three behaviours carry over from the calculator and are the reason a reader
    opens this list in the first place:

    * **Negative downtime counts as zero**, so an order can appear with hours
      recorded and nothing contributed.
    * **A linked order contributes its share**, not its downtime -- which is why
      ``impact_factor`` travels with every row rather than only in a footnote.
    * **Zero-downtime orders are listed.** They contribute nothing and are the
      entire evidence that an asset sitting at 100% was being worked on rather
      than merely unlogged.

    ``details`` may hold work orders for any month; only the ones bucketed into
    ``month`` are returned, using the same created-date rule as the chart.
    """

    asset = str(asset_number).strip()

    # A list of claims rather than a lookup keyed by asset: a rule may name the
    # parent as its own linked asset, and the calculator would then charge that
    # asset's downtime twice. Collapsing the two into one entry here would show
    # a bar of 1.5x downtime explained by rows summing to 1x.
    claims: list[tuple[str, str, float]] = [(asset, "direct", 1.0)]
    for rule in linked_rules or []:
        if str(rule.parent_asset_number).strip() != asset or rule.impact_factor <= 0:
            continue
        linked = str(rule.linked_asset_number).strip()
        if linked:
            claims.append((linked, "linked", float(rule.impact_factor)))

    by_asset: dict[str, list[WorkOrderDetail]] = {}
    for detail in details:
        order = detail.order
        if _month_key(order.created_local) != month:
            continue
        by_asset.setdefault(str(order.asset_number).strip(), []).append(detail)

    contributions: list[WorkOrderContribution] = []
    for source_asset, source, factor in claims:
        for detail in by_asset.get(source_asset, []):
            order = detail.order
            contributions.append(
                WorkOrderContribution(
                    detail=detail,
                    source=source,
                    impact_factor=factor,
                    counted_hours=max(0.0, order.downtime_hours) * factor,
                    # Gated on downtime the same way ``overlap_count`` is, so the
                    # rows marked here are countable against the figure the
                    # summary table shows.
                    crosses_month=order.downtime_hours > 0 and _crosses_month(order),
                )
            )

    # Biggest contributor first: the question that opens this list is almost
    # always "what took the machine down", and the answer is the top row.
    contributions.sort(key=lambda c: (-c.counted_hours, c.order.created_local, c.order.task_id))
    return contributions


def build_series(
    rows: list[AvailabilityRow],
    groups: list[AssetGroup],
    months: list[date],
    *,
    goals: dict[tuple[str, date], float] | None = None,
) -> list[GroupSeries]:
    """Reshape result rows into one chart payload per group.

    ``average`` is the unweighted mean of the group's assets for that month,
    matching the workbook's ``=AVERAGE(...)`` -- an asset with ten times the
    downtime of another still counts once. Months where no asset has a defined
    availability produce ``None`` so the line breaks rather than dropping to
    zero.
    """

    goals = goals or {}
    by_group: dict[str, list[AvailabilityRow]] = {}
    for row in rows:
        by_group.setdefault(row.asset_group, []).append(row)

    series: list[GroupSeries] = []
    for group in sorted(groups, key=lambda g: (g.sort_order, g.asset_group)):
        if not group.include:
            continue
        group_rows = by_group.get(group.asset_group, [])
        if not group_rows:
            continue

        indexed = {(row.asset_number, row.month): row for row in group_rows}
        chart = GroupSeries(
            asset_group=group.asset_group,
            months=list(months),
            month_labels=format_month_labels(months),
            net_scheduled_hours_per_day=group.net_scheduled_hours_per_day,
        )

        for asset in group.asset_numbers:
            asset = str(asset).strip()
            cells = [indexed.get((asset, month)) for month in months]
            if not any(cells):
                continue
            first = next(cell for cell in cells if cell)
            chart.assets.append(
                {
                    "asset_number": asset,
                    "display_name": first.display_name,
                    "values": [cell.availability if cell else None for cell in cells],
                    "flagged": [bool(cell.flagged) if cell else False for cell in cells],
                    "notes": [cell.note if cell else "" for cell in cells],
                }
            )

        for month in months:
            defined = [
                indexed[(a, month)].availability
                for a in (str(x).strip() for x in group.asset_numbers)
                if (a, month) in indexed and indexed[(a, month)].availability is not None
            ]
            chart.average.append(sum(defined) / len(defined) if defined else None)
            chart.goal.append(goals.get((group.asset_group, month), DEFAULT_GOAL_PERCENT))

        series.append(chart)
    return series
