"""Analysis repository for life-data examples."""

from models.dto import AnalysisResult


class AnalysisRepository:
    """Provides sample life-data analysis results."""

    def latest_results(self) -> list[AnalysisResult]:
        return [
            AnalysisResult(
                "Pump Fleet Weibull Fit",
                "Ready",
                "Beta greater than one indicates wear-out behavior in the sample fleet.",
                {"beta": 1.82, "eta_hours": 7420},
            ),
            AnalysisResult(
                "Conveyor Bearing Survival Curve",
                "Draft",
                "Survival estimate generated from failure and suspension records.",
                {"survival_at_1y": "94%", "records": 128},
            ),
        ]
