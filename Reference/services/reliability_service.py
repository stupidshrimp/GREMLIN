"""Reliability service orchestration for GREMLIN pages."""

from repositories.analysis_repo import AnalysisRepository
from repositories.failure_repo import FailureRepository
from repositories.metrics_repo import MetricsRepository


class ReliabilityService:
    """Coordinates repository data for reliability dashboard views."""

    def __init__(
        self,
        metrics_repo: MetricsRepository,
        failure_repo: FailureRepository,
        analysis_repo: AnalysisRepository,
    ) -> None:
        self.metrics_repo = metrics_repo
        self.failure_repo = failure_repo
        self.analysis_repo = analysis_repo

    def get_metrics_dashboard_data(self) -> dict[str, object]:
        return {
            "cards": self.metrics_repo.list_metric_cards(),
            "analyses": self.analysis_repo.latest_results(),
        }

    def get_failure_classification_data(self) -> dict[str, object]:
        classifications = self.failure_repo.list_failure_classifications()
        return {
            "classifications": classifications,
            "total": sum(item.count for item in classifications),
        }
