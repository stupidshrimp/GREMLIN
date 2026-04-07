"""Ingestion orchestration placeholders.

This layer coordinates:
1) pulling data from integrations/limble.py
2) persisting raw payloads
3) transforming/upserting core tables
"""

from integrations.limble import LimbleClient
from repositories.raw_repo import RawRepository


class IngestionService:
    """Coordinates Limble data synchronization workflow."""

    def __init__(self, limble_client: LimbleClient, raw_repo: RawRepository) -> None:
        self.limble_client = limble_client
        self.raw_repo = raw_repo

    def sync_all(self, updated_since: str | None = None) -> dict[str, int]:
        """Run all ingestion steps (placeholder)."""
        assets = self.limble_client.get_assets(updated_since=updated_since)
        work_orders = self.limble_client.get_work_orders(updated_since=updated_since)
        failures = self.limble_client.get_failure_events(updated_since=updated_since)

        # TODO: write payloads to raw tables and call transform/upsert steps.
        self.raw_repo.store_batch("assets", assets)
        self.raw_repo.store_batch("work_orders", work_orders)
        self.raw_repo.store_batch("failures", failures)

        return {
            "assets": len(assets),
            "work_orders": len(work_orders),
            "failures": len(failures),
        }
