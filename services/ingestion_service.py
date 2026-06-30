"""Ingestion orchestration: Limble API -> GREMLIN.db.

This layer:

1. pulls tasks (and, for enrichment, assets) from :mod:`integrations.limble`
2. transforms each task into the ``raw_json`` shape the GREMLIN mapping layer
   already understands (see ``LifeDataService._map_raw_record`` and the
   Availability dashboard's ``_load_from_raw_json``)
3. persists them via :class:`repositories.raw_repo.RawRepository`
4. refreshes ``mapped_cmms_record`` so the app sees the new rows immediately

Transform decisions (confirmed for this Limble account):

* **Asset Number = the Limble ``assetID``.** The app keys every screen off
  ``asset_number``; the ``/tasks`` endpoint only carries a numeric ``assetID``,
  so we copy it into the ``"Asset Number"`` field the mapper reads.
* **``downtime`` is stored in minutes.** The downstream availability calculator
  divides ``downtime`` by 60 to get hours, so the value is normalised to minutes
  here (the input unit is configurable to guard against a future change).
* **Dates** are emitted both as the original Unix values *and* as ISO-8601 UTC
  ``*_Final`` strings, because the Weibull/event-processing path parses the
  ``*_Final`` columns and cannot read raw Unix integers.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable

from integrations.limble import LimbleClient
from repositories.raw_repo import RawRepository

_DOWNTIME_UNIT_FACTORS = {"minutes": 1.0, "seconds": 1.0 / 60.0, "hours": 60.0}


class IngestionService:
    """Coordinates the Limble -> GREMLIN.db synchronization workflow."""

    def __init__(
        self,
        limble_client: LimbleClient,
        raw_repo: RawRepository,
        *,
        downtime_unit: str = "minutes",
        fetch_assets: bool = True,
        refresh_mapping: bool = True,
        exclude_templates: bool = True,
        log: Callable[[str], None] = print,
    ) -> None:
        self.limble_client = limble_client
        self.raw_repo = raw_repo
        unit = (downtime_unit or "minutes").strip().lower()
        if unit not in _DOWNTIME_UNIT_FACTORS:
            raise ValueError(f"downtime_unit must be one of {sorted(_DOWNTIME_UNIT_FACTORS)}; got {downtime_unit!r}")
        self.downtime_unit = unit
        self.fetch_assets = fetch_assets
        self.refresh_mapping = refresh_mapping
        self.exclude_templates = exclude_templates
        self._log = log

    # ------------------------------------------------------------------
    # Orchestration
    # ------------------------------------------------------------------
    def sync_all(self, updated_since: int | None = None, *, dry_run: bool = False) -> dict[str, Any]:
        """Run one sync cycle and return a summary dict."""

        self._log("Fetching tasks from Limble ...")
        tasks = self.limble_client.get_tasks(updated_since=updated_since)
        fetched_tasks = len(tasks)
        self._log(f"Fetched {fetched_tasks} task(s).")

        # Limble /tasks includes template rows (PM/work-order definitions, not
        # real events). The legacy export filtered these out before building the
        # asset task sheet, and downstream disposition/analysis does not exclude
        # them, so drop truthy-template tasks here to avoid contaminating data.
        excluded_templates = 0
        if self.exclude_templates:
            kept = [task for task in tasks if not self._is_template(task)]
            excluded_templates = len(tasks) - len(kept)
            if excluded_templates:
                self._log(f"Excluded {excluded_templates} template task(s).")
            tasks = kept

        asset_index = AssetIndex.empty()
        if self.fetch_assets:
            self._log("Fetching assets for enrichment ...")
            assets = self.limble_client.get_assets()
            asset_index = AssetIndex.from_assets(assets)
            self._log(f"Fetched {len(assets)} asset(s).")

        records = [self.transform(task, asset_index) for task in tasks]

        if dry_run:
            self._log("Dry run: no database changes were made.")
            return {
                "fetched_tasks": fetched_tasks,
                "excluded_templates": excluded_templates,
                "fetched_assets": asset_index.count,
                "records": len(records),
                "inserted": 0,
                "updated": 0,
                "skipped": 0,
                "mapped": 0,
                "dry_run": True,
            }

        self.raw_repo.ensure_schema()
        batch_id = self.raw_repo.start_batch(notes=f"Limble sync of {len(records)} task(s)")
        try:
            counts = self.raw_repo.upsert_records(batch_id, records)
        except Exception:
            self.raw_repo.complete_batch(batch_id, status="FAILED", raw_row_count=0)
            raise
        self.raw_repo.complete_batch(batch_id, status="COMPLETED", raw_row_count=len(records))
        self._log(
            f"Raw import complete: {counts['inserted']} inserted, "
            f"{counts['updated']} updated, {counts['skipped']} unchanged."
        )

        mapping = {"mapped": 0, "mapping_ok": True, "mapping_note": "skipped (--no-map)"}
        if self.refresh_mapping:
            mapping = self._refresh_mapped_records()

        return {
            "fetched_tasks": fetched_tasks,
            "excluded_templates": excluded_templates,
            "fetched_assets": asset_index.count,
            "records": len(records),
            "import_batch_id": batch_id,
            **counts,
            **mapping,
            "dry_run": False,
        }

    def _refresh_mapped_records(self) -> dict[str, Any]:
        """Map new/changed raw rows into ``mapped_cmms_record`` via LifeDataService.

        Returns ``{"mapped": int, "mapping_ok": bool, "mapping_note": str | None}``
        so the caller can report an honest outcome instead of a bland success.
        """

        # Precondition: mapping requires raw_cmms_record to carry both
        # raw_record_id (FK target + LifeDataService's downtime backfill JOIN) and
        # import_batch_id (mapped_cmms_record has a NOT NULL FK to import_batch;
        # LifeDataService otherwise inserts "0 AS import_batch_id", which violates
        # that FK). A legacy table missing either cannot be mapped without
        # migrating the database. We do not restructure the user's raw tables here
        # (an earlier in-place rebuild of these tables was intentionally reverted),
        # so report the situation clearly rather than masking a failed/no-op
        # mapping as success — the raw data is still imported and will map once the
        # database includes those columns.
        missing = self._missing_mapping_columns()
        if missing:
            note = (
                f"raw_cmms_record is missing column(s) {', '.join(missing)} required for mapping, so the "
                "imported rows could not be mapped and will not appear on the dashboards yet. The raw data "
                "was imported and will map once the database includes those columns."
            )
            self._log("Warning: " + note)
            return {"mapped": 0, "mapping_ok": False, "mapping_note": note}

        try:
            from services.life_data_service import LifeDataService
        except Exception as exc:  # noqa: BLE001 - mapping is best-effort
            note = f"LifeDataService unavailable: {exc}"
            self._log("Skipping mapping refresh (" + note + ").")
            return {"mapped": 0, "mapping_ok": False, "mapping_note": note}
        try:
            service = LifeDataService(db_path=self.raw_repo.db_path, refresh_on_startup=False)
            mapped = int(service.refresh_mapped_cmms_records() or 0)
            self._log(f"Mapped {mapped} raw record(s) into mapped_cmms_record.")
            return {"mapped": mapped, "mapping_ok": True, "mapping_note": None}
        except Exception as exc:  # noqa: BLE001 - never lose a good raw import over mapping
            note = f"mapping refresh failed: {exc}"
            self._log("Warning: " + note + ". Raw data was imported successfully.")
            return {"mapped": 0, "mapping_ok": False, "mapping_note": note}

    def _missing_mapping_columns(self) -> list[str]:
        """Return the columns the mapping layer needs that raw_cmms_record lacks."""

        conn = self.raw_repo.connect()
        try:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(raw_cmms_record)")}
        finally:
            conn.close()
        return [column for column in ("raw_record_id", "import_batch_id") if column not in columns]

    @staticmethod
    def _is_template(task: dict[str, Any]) -> bool:
        """True when a Limble task is a template (PM/WO definition, not a real event)."""

        value = task.get("template")
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "y", "t")
        return bool(value)

    # ------------------------------------------------------------------
    # Transform
    # ------------------------------------------------------------------
    def transform(self, task: dict[str, Any], asset_index: "AssetIndex") -> dict[str, Any]:
        """Turn one Limble task into a raw_json dict for raw_cmms_record."""

        record: dict[str, Any] = dict(task)  # keep full fidelity of the API payload
        asset_id = task.get("assetID")

        # Asset Number == assetID (confirmed for this account).
        if asset_id not in (None, ""):
            record["Asset Number"] = str(asset_id)

        # Asset hierarchy enrichment from /assets.
        asset_index.enrich(record, asset_id)

        # Normalise downtime to minutes (what the consumers expect).
        minutes = self._downtime_to_minutes(task.get("downtime"))
        if minutes is not None:
            if self.downtime_unit != "minutes":
                record["downtime_source_value"] = task.get("downtime")
                record["downtime_source_unit"] = self.downtime_unit
            record["downtime"] = minutes

        # Emit ISO-8601 UTC datetimes alongside the original Unix timestamps so
        # the *_Final-preferring downstream parsers have a value they can read.
        self._add_date_fields(record, task.get("createdDate"), "createdDate_Final", "createdDateTime")
        self._add_date_fields(record, task.get("startDate"), "startDate_Final", "startDateTime")
        self._add_date_fields(record, task.get("dateCompleted"), "completedDate_Final", "completedDateTime")
        due_value = task.get("dueDate") if task.get("dueDate") not in (None, "", 0) else task.get("due")
        self._add_date_fields(record, due_value, "dueDate_Final", None)

        return record

    @staticmethod
    def _add_date_fields(record: dict[str, Any], unix_value: Any, final_key: str, datetime_key: str | None) -> None:
        iso = _unix_to_iso_utc(unix_value)
        if iso is None:
            return
        record[final_key] = iso
        if datetime_key:
            record[datetime_key] = iso

    def _downtime_to_minutes(self, value: Any) -> float | None:
        number = _coerce_number(value)
        if number is None:
            return None
        return number * _DOWNTIME_UNIT_FACTORS[self.downtime_unit]


class AssetIndex:
    """Lookup of Limble assets by id, with parent/root resolution."""

    def __init__(self, by_id: dict[str, dict[str, Any]], has_children: set[str]) -> None:
        self._by_id = by_id
        self._has_children = has_children

    @property
    def count(self) -> int:
        return len(self._by_id)

    @classmethod
    def empty(cls) -> "AssetIndex":
        return cls({}, set())

    @classmethod
    def from_assets(cls, assets: list[dict[str, Any]]) -> "AssetIndex":
        by_id: dict[str, dict[str, Any]] = {}
        has_children: set[str] = set()
        for asset in assets:
            asset_id = asset.get("assetID")
            if asset_id in (None, ""):
                continue
            by_id[str(asset_id)] = asset
            parent = _asset_parent_id(asset)
            if parent:
                has_children.add(parent)
        return cls(by_id, has_children)

    def enrich(self, record: dict[str, Any], asset_id: Any) -> None:
        if asset_id in (None, "") or not self._by_id:
            return
        asset = self._by_id.get(str(asset_id))
        if asset is None:
            return
        name = asset.get("name")
        if name not in (None, ""):
            record.setdefault("Asset Name", name)

        parent_id = _asset_parent_id(asset)
        if parent_id:
            record["Immediate Parent Asset ID"] = parent_id
            parent = self._by_id.get(parent_id)
            if parent and parent.get("name") not in (None, ""):
                record["Immediate Parent Asset Name"] = parent.get("name")

        root, depth = self._resolve_root(str(asset_id))
        if root is not None:
            record["Root Asset ID"] = root.get("assetID")
            if root.get("name") not in (None, ""):
                record["Root Asset Name"] = root.get("name")
        record["WO Asset Level"] = depth
        record["Asset Has Children"] = 1 if str(asset_id) in self._has_children else 0

    def _resolve_root(self, asset_id: str) -> tuple[dict[str, Any] | None, int]:
        """Walk parents to the root; return (root_asset, depth_from_root)."""

        current = self._by_id.get(asset_id)
        if current is None:
            return None, 0
        seen = {asset_id}
        depth = 0
        while True:
            parent_id = _asset_parent_id(current)
            if not parent_id or parent_id in seen or parent_id not in self._by_id:
                return current, depth
            seen.add(parent_id)
            current = self._by_id[parent_id]
            depth += 1


def _asset_parent_id(asset: dict[str, Any]) -> str | None:
    for key in ("parentAssetID", "parentID", "parent_asset_id"):
        value = asset.get(key)
        if value not in (None, "", 0, "0"):
            return str(value)
    return None


def _coerce_number(value: Any) -> float | None:
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def _unix_to_iso_utc(value: Any) -> str | None:
    """Convert a Unix timestamp (seconds, or milliseconds) to an ISO-8601 UTC string."""

    number = _coerce_number(value)
    if number is None or number <= 0:
        return None
    # Heuristic: values above ~ year 2286 in seconds are almost certainly ms.
    seconds = number / 1000.0 if number > 10_000_000_000 else number
    try:
        return datetime.fromtimestamp(seconds, tz=timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return None
