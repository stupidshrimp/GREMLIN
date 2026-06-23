"""Data transfer objects for GREMLIN reliability views."""

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class MetricCard:
    label: str
    value: str
    detail: str


@dataclass(frozen=True)
class FailureClassification:
    name: str
    count: int
    severity: str
    description: str


@dataclass(frozen=True)
class AnalysisResult:
    title: str
    status: str
    summary: str
    values: dict[str, Any]
