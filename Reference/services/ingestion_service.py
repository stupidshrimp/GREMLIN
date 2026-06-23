"""Data ingestion service placeholders."""

from repositories.raw_repo import RawRepository


class IngestionService:
    """Normalizes and stores incoming maintenance records."""

    def __init__(self, raw_repo: RawRepository) -> None:
        self.raw_repo = raw_repo

    def ingest(self, record: dict[str, object]) -> dict[str, object]:
        normalized = {str(key).strip().lower(): value for key, value in record.items()}
        self.raw_repo.add(normalized)
        return normalized
