"""DTO placeholders for service <-> route contracts."""

from dataclasses import dataclass


@dataclass
class MetricsSnapshot:
    mtbf_hours: float | None
    mttr_hours: float | None
    first_pass_yield: float | None


@dataclass
class SyncSummary:
    assets: int
    work_orders: int
    failures: int
