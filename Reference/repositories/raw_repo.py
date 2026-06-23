"""Raw data repository placeholders for future persistence integration."""


class RawRepository:
    """Stores incoming records in memory until a database is configured."""

    def __init__(self) -> None:
        self._records: list[dict[str, object]] = []

    def add(self, record: dict[str, object]) -> None:
        self._records.append(record)

    def all(self) -> list[dict[str, object]]:
        return list(self._records)
