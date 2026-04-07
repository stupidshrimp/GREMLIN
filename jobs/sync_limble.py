"""Manual/scheduled job entrypoint for Limble sync (placeholder).

Usage:
- Preferred module mode: `python -m jobs.sync_limble`
- Supported script mode: `python jobs/sync_limble.py`
"""

from pathlib import Path
import sys

if __package__ in (None, ""):
    # Allow script-mode execution from repo root by adding project root to sys.path.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from integrations.limble import LimbleClient, LimbleConfig
from repositories.raw_repo import RawRepository
from services.ingestion_service import IngestionService


def run_sync(updated_since: str | None = None) -> dict[str, int]:
    """Run one sync cycle using placeholder wiring."""
    # TODO: load real config from environment or secrets manager.
    config = LimbleConfig(base_url="https://api.limblecmms.com", api_key="REPLACE_ME")
    client = LimbleClient(config=config)
    raw_repo = RawRepository()
    service = IngestionService(limble_client=client, raw_repo=raw_repo)
    return service.sync_all(updated_since=updated_since)


if __name__ == "__main__":
    summary = run_sync()
    print(f"Sync complete: {summary}")
