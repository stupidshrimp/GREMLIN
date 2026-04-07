"""Failure event repository placeholders."""


class FailureRepository:
    """Provides failure-event reads and classification updates."""

    def fetch_unclassified_failures(self) -> list[dict]:
        """Return unclassified failure rows (placeholder)."""
        # TODO: query fact_failure_event where classification_status='unreviewed'.
        return []

    def fetch_mode_summary(self) -> list[dict]:
        """Return grouped counts by failure mode (placeholder)."""
        # TODO: query grouped mode counts for Pareto chart.
        return []

    def update_classification(
        self,
        failure_event_id: int,
        failure_mode_id: int,
        failure_mechanism_id: int | None,
        user_id: str,
        notes: str | None,
    ) -> None:
        """Update classification fields for an event (placeholder)."""
        # TODO: issue SQL UPDATE + INSERT into audit trail table.
        _ = (failure_event_id, failure_mode_id, failure_mechanism_id, user_id, notes)
