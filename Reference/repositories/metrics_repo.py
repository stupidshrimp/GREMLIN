"""Metrics repository for reliability dashboard sample data."""

from models.dto import MetricCard


class MetricsRepository:
    """Provides metric summaries for the dashboard."""

    def list_metric_cards(self) -> list[MetricCard]:
        return [
            MetricCard("MTBF", "1,284 h", "Mean time between failures"),
            MetricCard("Availability", "98.7%", "Operational readiness estimate"),
            MetricCard("Open PMs", "42", "Preventive maintenance work orders"),
            MetricCard("Critical Assets", "8", "Assets requiring reliability review"),
        ]
