"""Failure classification repository."""

from models.dto import FailureClassification


class FailureRepository:
    """Provides failure classification records."""

    def list_failure_classifications(self) -> list[FailureClassification]:
        return [
            FailureClassification(
                "Infant mortality",
                12,
                "Medium",
                "Early-life failures that should be screened through commissioning checks.",
            ),
            FailureClassification(
                "Random failure",
                31,
                "High",
                "Unpredictable events that benefit from condition monitoring.",
            ),
            FailureClassification(
                "Wear-out",
                18,
                "High",
                "Age-related degradation requiring overhaul or replacement planning.",
            ),
        ]
