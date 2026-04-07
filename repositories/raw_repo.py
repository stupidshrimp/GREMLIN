"""Raw-data repository placeholders."""

from typing import Any


class RawRepository:
    """Stores immutable raw Limble payloads."""

    def store_batch(self, source_endpoint: str, payloads: list[dict[str, Any]]) -> None:
        """Persist raw payload batch (placeholder)."""
        # TODO: insert rows into raw_limble_* tables with payload JSON + metadata.
        _ = (source_endpoint, payloads)
