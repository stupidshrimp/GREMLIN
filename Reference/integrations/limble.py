"""Limble CMMS integration facade."""


class LimbleClient:
    """Placeholder client for Limble API synchronization."""

    def __init__(self, api_key: str | None = None) -> None:
        self.api_key = api_key

    def list_assets(self) -> list[dict[str, object]]:
        return []

    def list_tasks(self) -> list[dict[str, object]]:
        return []
