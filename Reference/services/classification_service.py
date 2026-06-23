"""Helpers for failure classification workflows."""


class ClassificationService:
    """Classifies failures from simple severity keywords."""

    def classify(self, description: str) -> str:
        lowered = description.lower()
        if any(word in lowered for word in ("critical", "shutdown", "safety")):
            return "High"
        if any(word in lowered for word in ("repeat", "degraded", "warning")):
            return "Medium"
        return "Low"
