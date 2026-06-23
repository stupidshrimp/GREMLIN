"""Pure monthly availability calculations for the GREMLIN dashboard."""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date, datetime
from statistics import median
from typing import Protocol

MONTH_LABELS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


@dataclass(frozen=True)
class RawWorkOrder:
    task_id: str
    asset_number: str
    asset_name: str | None
    created_date: datetime
    completed_date: datetime | None
    downtime_minutes: float
    type: str | int | None
    status: str | None = None
    name: str | None = None
    description: str | None = None
    completion_notes: str | None = None
    is_pm_candidate: bool = False


@dataclass(frozen=True)
class AssetGroup:
    id: int | None
    asset_group: str
    asset_numbers: list[str]
    schedule_hours_per_day: float
    break_hours_per_day: float
    lunch_hours_per_day: float
    setup_hours_per_day: float
    net_scheduled_hours_per_day: float
    include_flag: bool
    notes: str = ""
    sort_order: int = 0


@dataclass(frozen=True)
class LinkedDowntimeRule:
    rule_group: str
    parent_asset_number: str
    linked_asset_number: str
    impact_factor: float


@dataclass(frozen=True)
class AvailabilityResult:
    selected_year: int
    asset_group: str
    asset_number: str
    asset_display_name: str
    month_date: str
    month_label: str
    scheduled_hours: float
    downtime_minutes: float
    downtime_hours: float
    availability_percent: float
    flagged: int
    overlap_count: int
    manual_ot_hours: float
    adjusted_scheduled_hours: float
    linked_downtime_hours: float
    adjusted_downtime_hours: float
    adjusted_availability_percent: float
    downtime_logic: str
    total_wo_count: int
    zero_downtime_wo_count: int
    no_wo_entries_flag: int
    zero_downtime_wo_percent: float
    zero_no_entry_note: str


class AvailabilityDataSource(Protocol):
    def load_asset_groups(self, include_only: bool = False) -> list[AssetGroup]: ...
    def load_display_names(self) -> dict[str, str]: ...
    def load_linked_rules(self) -> list[LinkedDowntimeRule]: ...
    def load_manual_ot(self, selected_year: int) -> dict[tuple[str, str, str], float]: ...
    def load_raw_work_orders(self, selected_year: int, utc_offset_hours: float) -> list[RawWorkOrder]: ...


def month_range_for_year(selected_year: int, today: date | None = None) -> list[date]:
    today = today or date.today()
    if selected_year > today.year:
        return []
    if selected_year == today.year:
        end_month = max(0, today.month - 1)
    else:
        end_month = 12
    return [date(selected_year, month, 1) for month in range(1, end_month + 1)]


def weekday_count(year: int, month: int) -> int:
    return sum(1 for day in range(1, calendar.monthrange(year, month)[1] + 1) if date(year, month, day).weekday() < 5)


def safe_availability(downtime_hours: float, scheduled_hours: float) -> float:
    if scheduled_hours <= 0:
        return 0.0
    return max(0.0, 1.0 - (downtime_hours / scheduled_hours))


class AvailabilityCalculator:
    """Calculates one result row for every included asset/month."""

    def __init__(self, data_source: AvailabilityDataSource, *, today: date | None = None) -> None:
        self.data_source = data_source
        self.today = today

    def calculate_availability(self, selected_year: int, utc_offset_hours: float = 5.0) -> list[AvailabilityResult]:
        months = month_range_for_year(selected_year, self.today)
        groups = self.data_source.load_asset_groups(include_only=True)
        display_names = self.data_source.load_display_names()
        linked_rules = self.data_source.load_linked_rules()
        manual_ot = self.data_source.load_manual_ot(selected_year)
        raw_rows = self.data_source.load_raw_work_orders(selected_year, utc_offset_hours)

        rows_by_asset_month: dict[tuple[str, str], list[RawWorkOrder]] = {}
        for row in raw_rows:
            if self._is_pm_work_order(row) or row.created_date.year != selected_year:
                continue
            month_date = date(row.created_date.year, row.created_date.month, 1).isoformat()
            rows_by_asset_month.setdefault((str(row.asset_number).strip(), month_date), []).append(row)

        direct_lookup: dict[tuple[str, str], float] = {}

        def direct_downtime_hours(asset_number: str, month_date: str) -> float:
            key = (str(asset_number).strip(), month_date)
            if key not in direct_lookup:
                direct_lookup[key] = sum(max(0.0, wo.downtime_minutes or 0.0) for wo in rows_by_asset_month.get(key, [])) / 60.0
            return direct_lookup[key]

        for group in groups:
            for asset_number in group.asset_numbers:
                asset = str(asset_number).strip()
                for month in months:
                    direct_downtime_hours(asset, month.isoformat())

        rules_by_parent: dict[str, list[LinkedDowntimeRule]] = {}
        for rule in linked_rules:
            if rule.impact_factor >= 0:
                rules_by_parent.setdefault(str(rule.parent_asset_number).strip(), []).append(rule)

        results: list[AvailabilityResult] = []
        for group in groups:
            net_hours = max(0.0, float(group.net_scheduled_hours_per_day))
            for asset_number in group.asset_numbers:
                asset = str(asset_number).strip()
                if not asset:
                    continue
                for month in months:
                    month_key = month.isoformat()
                    rows = rows_by_asset_month.get((asset, month_key), [])
                    scheduled_hours = weekday_count(month.year, month.month) * net_hours
                    downtime_minutes = sum(max(0.0, wo.downtime_minutes or 0.0) for wo in rows)
                    downtime_hours = downtime_minutes / 60.0
                    availability_percent = safe_availability(downtime_hours, scheduled_hours)
                    overlap_count = sum(1 for wo in rows if self._is_month_overlap(wo) and (wo.downtime_minutes or 0) > 0)
                    total_wo_count = len(rows)
                    zero_downtime_count = sum(1 for wo in rows if (wo.downtime_minutes or 0) == 0)
                    zero_pct = zero_downtime_count / total_wo_count if total_wo_count else 0.0
                    if total_wo_count == 0:
                        note = "No WO entries this month"
                    elif zero_downtime_count > 0:
                        note = f"{zero_downtime_count} WO(s) with 0 downtime"
                    else:
                        note = ""
                    ot_hours = max(0.0, manual_ot.get((group.asset_group, asset, month_key), 0.0))
                    linked_hours = sum(
                        direct_downtime_hours(str(rule.linked_asset_number).strip(), month_key) * max(0.0, rule.impact_factor)
                        for rule in rules_by_parent.get(asset, [])
                    )
                    adjusted_scheduled = scheduled_hours + ot_hours
                    adjusted_downtime = downtime_hours + linked_hours
                    results.append(AvailabilityResult(
                        selected_year=selected_year,
                        asset_group=group.asset_group,
                        asset_number=asset,
                        asset_display_name=display_names.get(asset, asset),
                        month_date=month_key,
                        month_label=MONTH_LABELS[month.month - 1],
                        scheduled_hours=scheduled_hours,
                        downtime_minutes=downtime_minutes,
                        downtime_hours=downtime_hours,
                        availability_percent=availability_percent,
                        flagged=1 if downtime_hours > scheduled_hours else 0,
                        overlap_count=overlap_count,
                        manual_ot_hours=ot_hours,
                        adjusted_scheduled_hours=adjusted_scheduled,
                        linked_downtime_hours=linked_hours,
                        adjusted_downtime_hours=adjusted_downtime,
                        adjusted_availability_percent=safe_availability(adjusted_downtime, adjusted_scheduled),
                        downtime_logic="Direct + linked downtime" if rules_by_parent.get(asset) else "Direct downtime only",
                        total_wo_count=total_wo_count,
                        zero_downtime_wo_count=zero_downtime_count,
                        no_wo_entries_flag=1 if total_wo_count == 0 else 0,
                        zero_downtime_wo_percent=zero_pct,
                        zero_no_entry_note=note,
                    ))
        return results

    def summarize_overall(self, results: list[AvailabilityResult]) -> dict[str, float | int]:
        total_wo = sum(row.total_wo_count for row in results)
        zero_wo = sum(row.zero_downtime_wo_count for row in results)
        availabilities = [row.adjusted_availability_percent for row in results]
        return {
            "total_work_orders": total_wo,
            "zero_downtime_work_orders": zero_wo,
            "zero_downtime_work_order_percent": zero_wo / total_wo if total_wo else 0.0,
            "asset_months_no_wo_entries": sum(row.no_wo_entries_flag for row in results),
            "total_adjusted_downtime_hours": sum(row.adjusted_downtime_hours for row in results),
            "overall_median_availability": median(availabilities) if availabilities else 0.0,
            "lowest_availability_shown": min(availabilities) if availabilities else 0.0,
        }

    @staticmethod
    def _is_pm_work_order(row: RawWorkOrder) -> bool:
        return bool(row.is_pm_candidate) or str(row.type or "").strip() in {"1", "4"}

    @staticmethod
    def _is_month_overlap(row: RawWorkOrder) -> bool:
        if row.completed_date is None:
            return False
        return row.created_date.year != row.completed_date.year or row.created_date.month != row.completed_date.month
