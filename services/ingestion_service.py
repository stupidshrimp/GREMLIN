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
* **``downtime`` is reported by Limble in seconds** (e.g. ``12600`` == 3 h 30 m).
  It is stored in ``raw_json`` exactly as Limble provides it;
  ``LifeDataService._parse_downtime_minutes`` is the single place that normalises
  it (seconds -> minutes -> hours). Ingestion deliberately does not rescale the
  value, so there is no second conversion to keep in sync.
* **Dates** are emitted both as the original Unix values *and* as ISO-8601 UTC
  ``*_Final`` strings, because the Weibull/event-processing path parses the
  ``*_Final`` columns and cannot read raw Unix integers.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any, Callable

from integrations.limble import LimbleClient, task_touched_at
from repositories.raw_repo import INSTRUCTIONS_READ_ERROR, RawRepository
from services.wo_narrative import NARRATIVE_KEYS, extract_narrative


# The phases a sync moves through, in order. The names are part of the contract
# with anything reporting progress (see services.sync_service): a caller weights
# them to turn "phase + count" into an overall fraction, so renaming one here
# means renaming it there.
PHASE_TASKS = "tasks"
PHASE_ASSETS = "assets"
PHASE_INSTRUCTIONS = "instructions"
PHASE_TRANSFORM = "transform"
PHASE_WRITE = "write"
PHASE_MAP = "map"

# Stamped onto a task's stored payload when its instructions were last read, so
# a run can tell "asked and there was nothing" from "never asked". Kept in
# raw_json rather than a table of its own because it describes that payload and
# travels with it through the same merge.
INSTRUCTIONS_READ_AT = "instructions_read_at"

# How many work orders one run will read instructions for unless told otherwise.
# There is deliberately no unlimited default: the first run against a long
# history would otherwise take days at the client's rate limit, and a bounded run
# that resumes is the same work done in the same order without a surprise.
DEFAULT_INSTRUCTIONS_LIMIT = 500

# How often the transform loop reports progress, in records.
_TRANSFORM_PROGRESS_EVERY = 250


class IngestionService:
    """Coordinates the Limble -> GREMLIN.db synchronization workflow."""

    def __init__(
        self,
        limble_client: LimbleClient,
        raw_repo: RawRepository,
        *,
        fetch_assets: bool = True,
        refresh_mapping: bool = True,
        exclude_templates: bool = True,
        fetch_instructions: bool = True,
        instructions_limit: int | None = DEFAULT_INSTRUCTIONS_LIMIT,
        log: Callable[[str], None] = print,
        progress: Callable[[str, int | None, int | None], None] | None = None,
        on_batch_started: Callable[[int], None] | None = None,
    ) -> None:
        self.limble_client = limble_client
        self.raw_repo = raw_repo
        self.fetch_assets = fetch_assets
        self.refresh_mapping = refresh_mapping
        self.exclude_templates = exclude_templates
        # The Area Affected / Condition / Cause / Action boxes live in a task's
        # *instructions*, which /tasks does not carry -- each one is its own
        # request. On by default: the cost is the first walk through the history,
        # not the nightly run, because a task records that it was read and is not
        # read again. A sync left to itself therefore keeps the narrative current
        # for a minute a night, where an off-by-default phase would have meant a
        # flag nobody remembers and tables that quietly stop being true.
        self.fetch_instructions = fetch_instructions
        self.instructions_limit = instructions_limit
        self._log = log
        # ``progress(phase, current, total)`` is optional structured reporting
        # for a UI; ``log`` stays the human-readable narration the CLI prints.
        # ``total`` is None where the size is genuinely unknown until the phase
        # ends (the Limble list endpoints do not report a total), so callers
        # must treat it as "unknown" rather than "zero".
        self._progress = progress
        # Called with the import_batch id as soon as the row exists. A caller
        # watching the database for other people's imports needs to know which
        # open batch is this one's, and cannot infer it: the row does not appear
        # until the fetching is over, so for most of a run there isn't one.
        self._on_batch_started = on_batch_started

    # ------------------------------------------------------------------
    # Orchestration
    # ------------------------------------------------------------------
    def _emit(self, phase: str, current: int | None = None, total: int | None = None) -> None:
        """Report progress, if anyone is listening.

        Progress reporting is decoration on a data import: a listener that
        raises must not lose an otherwise good sync, so failures are swallowed.
        """

        if self._progress is None:
            return
        try:
            self._progress(phase, current, total)
        except Exception:  # noqa: BLE001 - never fail a sync over its progress bar
            pass

    def sync_all(self, updated_since: int | None = None, *, dry_run: bool = False) -> dict[str, Any]:
        """Run one sync cycle and return a summary dict."""

        self._log("Fetching tasks from Limble ...")
        self._emit(PHASE_TASKS, 0, None)
        tasks = self.limble_client.get_tasks(
            updated_since=updated_since,
            on_page=lambda fetched, _page: self._emit(PHASE_TASKS, fetched, None),
        )
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
            self._emit(PHASE_ASSETS, 0, None)
            assets = self.limble_client.get_assets(
                on_page=lambda fetched, _page: self._emit(PHASE_ASSETS, fetched, None),
            )
            asset_index = AssetIndex.from_assets(assets)
            self._log(f"Fetched {len(assets)} asset(s).")

        instruction_counts = self._attach_instructions(tasks)

        self._emit(PHASE_TRANSFORM, 0, len(tasks))
        records = []
        for position, task in enumerate(tasks, start=1):
            records.append(self.transform(task, asset_index))
            if position % _TRANSFORM_PROGRESS_EVERY == 0 or position == len(tasks):
                self._emit(PHASE_TRANSFORM, position, len(tasks))

        if dry_run:
            self._log("Dry run: no database changes were made.")
            return {
                "fetched_tasks": fetched_tasks,
                "excluded_templates": excluded_templates,
                "fetched_assets": asset_index.count,
                **instruction_counts,
                "records": len(records),
                "inserted": 0,
                "updated": 0,
                "skipped": 0,
                "mapped": 0,
                "dry_run": True,
            }

        self._log(f"Writing {len(records)} record(s) to the database ...")
        self._emit(PHASE_WRITE, 0, len(records))
        self.raw_repo.ensure_schema()
        batch_id = self.raw_repo.start_batch(notes=f"Limble sync of {len(records)} task(s)")
        if self._on_batch_started is not None:
            try:
                self._on_batch_started(batch_id)
            except Exception:  # noqa: BLE001 - bookkeeping must not fail an import
                pass
        try:
            counts = self.raw_repo.upsert_records(
                batch_id,
                records,
                progress=lambda done, total: self._emit(PHASE_WRITE, done, total),
            )
        except Exception:
            self.raw_repo.complete_batch(batch_id, status="FAILED", raw_row_count=0)
            raise
        self._log(
            f"Raw import complete: {counts['inserted']} inserted, "
            f"{counts['updated']} updated, {counts['skipped']} unchanged."
        )

        try:
            mapping = {"mapped": 0, "mapping_ok": True, "mapping_note": "skipped (--no-map)"}
            if self.refresh_mapping:
                # The mapping refresh is one SQL statement inside LifeDataService, so
                # there is no count to report while it runs -- only that it started.
                self._emit(PHASE_MAP, None, None)
                self._log("Refreshing the mapped layer ...")
                mapping = self._refresh_mapped_records()
        except Exception:
            # _refresh_mapped_records handles failures from the mapper itself as
            # best-effort outcomes, but setup work such as schema inspection can
            # still raise. Do not leave a committed raw batch looking in-flight.
            self.raw_repo.complete_batch(batch_id, status="FAILED", raw_row_count=len(records))
            raise

        # The batch completion timestamp is also the database-backed signal
        # used by other processes to decide that every derived view is current.
        # Keep the batch open until mapping has returned; publishing COMPLETED
        # immediately after the raw upsert lets a reader reload and cache the
        # old mapped layer while this final phase is still in progress.
        self.raw_repo.complete_batch(batch_id, status="COMPLETED", raw_row_count=len(records))

        return {
            "fetched_tasks": fetched_tasks,
            "excluded_templates": excluded_templates,
            "fetched_assets": asset_index.count,
            **instruction_counts,
            "records": len(records),
            "import_batch_id": batch_id,
            **counts,
            **mapping,
            "dry_run": False,
        }

    def _attach_instructions(self, tasks: list[dict[str, Any]]) -> dict[str, Any]:
        """Fill in each task's four narrative boxes from its instruction items.

        The boxes the maintenance teams fill out -- Area Affected, Condition,
        Cause, Action -- are task *instructions*, and Limble serves those only as
        a per-task sub-resource. One request per task at the client's rate limit
        makes a full history impossible to walk in one run, so this is selective
        on three counts:

        * **Off unless asked for.** A sync that does not need the narrative
          should not pay for it.
        * **Completed tasks only.** The boxes are the closing write-up; a task
          still open has nothing in them.
        * **Only what has not been read.** Selection turns on whether the task
          was asked about, not on whether the asking found anything -- plenty of
          completed work orders legitimately have their boxes blank, and
          selecting on the answer would leave those candidates forever at the
          head of a newest-first queue, so every capped run would re-read the
          same few and never reach the rest. A task edited since it was read is
          asked about again, because boxes left blank at closing can be filled in
          later.

        Only the four answers are kept, written onto the task as ordinary
        top-level fields. The instruction list itself runs to kilobytes of
        checklist boilerplate per task and would bloat raw_json for nothing --
        the mapper reads the distilled fields through the same path it reads any
        other task field.
        """

        if not self.fetch_instructions:
            return {}
        if not callable(getattr(self.limble_client, "get_task_instructions", None)):
            # An older client, or a stand-in that only knows the list endpoints.
            # The narrative is an addition to a sync, never the point of one, so
            # losing the whole import over it would be the wrong trade.
            self._log("This Limble client cannot read task instructions; skipping the failure narrative.")
            return {}

        read_at, errored = self._instructions_read_state()
        candidates = [
            task for task in tasks
            if self._task_key(task) is not None
            and task.get("dateCompleted") not in (None, "", 0, "0")
            and self._needs_reading(task, read_at)
        ]
        # Most recently completed first, because a capped run has to choose and
        # this is the choice worth making twice over: the boxes only exist on
        # work orders closed since the template started asking for them, so
        # recent tasks are the ones that have anything to read, and they are also
        # the ones an analysis is looking at. Taking them in arrival order would
        # spend a whole run on history that predates the template and return
        # nothing.
        # Never tried first, then the ones a previous run could not read, each
        # newest-completed first. A task that always fails -- deleted, or invisible
        # to this key -- then costs the tail of a run rather than its head, so it
        # can never be what stops the backfill reaching older history.
        candidates.sort(key=lambda task: (self._task_key(task) in errored, -self._completed_at(task)))
        limit = self.instructions_limit
        capped = len(candidates)
        if limit is not None:
            # Clamped rather than ignored. Reading nothing is a wasted run;
            # treating a negative cap as "no cap" would instead start an
            # unbounded rate-limited walk of the whole history.
            candidates = candidates[:max(0, limit)]
        remaining = capped - len(candidates)

        if not candidates:
            self._log("No tasks need their instructions fetched.")
            return {
                "instructions_fetched": 0,
                "instructions_found": 0,
                "instructions_failed": 0,
                "instructions_remaining": remaining,
                "instructions_seconds": 0.0,
            }

        self._log(f"Fetching instructions for {len(candidates)} task(s) ...")
        started = time.monotonic()
        self._emit(PHASE_INSTRUCTIONS, 0, len(candidates))
        by_id = {id(task): task for task in candidates}
        found = 0
        errors = 0
        for position, task in enumerate(candidates, start=1):
            task_id = self._task_key(task)
            try:
                instructions = self.limble_client.get_task_instructions(task_id)
            except Exception as exc:  # noqa: BLE001 - one bad task must not lose the sync
                self._log(f"Warning: could not read instructions for task {task_id}: {exc}")
                # Stamped like a success, and for the same reason: a task that
                # always fails -- deleted, or one this key cannot see -- would
                # otherwise stay unread forever at the head of a newest-first
                # queue and block the backfill exactly as a blank one used to.
                # The error is recorded alongside so the next run can put it
                # behind everything never tried, where a permanent failure costs
                # the tail of a run rather than all of it.
                by_id[id(task)][INSTRUCTIONS_READ_AT] = _utc_now()
                by_id[id(task)][INSTRUCTIONS_READ_ERROR] = str(exc)[:500]
                errors += 1
                self._emit(PHASE_INSTRUCTIONS, position, len(candidates))
                continue
            narrative = extract_narrative({"instructions": instructions})
            # Stamped whether or not anything was found. Plenty of completed work
            # orders legitimately have no boxes filled in, and without a record of
            # having asked they would be selected again on every run -- ahead of
            # everything older, since the newest are read first -- so a capped
            # backfill would re-read the same head of the queue forever and never
            # reach the rest of the history.
            by_id[id(task)][INSTRUCTIONS_READ_AT] = _utc_now()
            by_id[id(task)][INSTRUCTIONS_READ_ERROR] = ""
            # Every box is written, not just the answered ones. This read saw the
            # whole task, so a box it did not return is one that was cleared in
            # Limble, and the merge preserves what a payload omits -- leaving the
            # old text in place would keep a work order showing failure evidence
            # its own record no longer makes.
            by_id[id(task)].update({key: narrative.get(key, "") for key in NARRATIVE_KEYS})
            if narrative:
                found += 1
            self._emit(PHASE_INSTRUCTIONS, position, len(candidates))

        self._log(
            f"Read instructions for {len(candidates)} task(s); {found} carried a failure narrative."
            + (f" {errors} could not be read." if errors else "")
            + (f" {remaining} more still to do." if remaining else "")
        )
        return {
            "instructions_fetched": len(candidates),
            "instructions_found": found,
            "instructions_failed": errors,
            "instructions_remaining": remaining,
            # Reported so a caller timing the sync can take this phase back out.
            # It is paced by the API's rate limit rather than by how much work
            # the sync did, so counting it would put a backfill's hours into the
            # estimate shown before an ordinary nightly run.
            "instructions_seconds": round(time.monotonic() - started, 3),
        }

    def _instructions_read_state(self) -> tuple[dict[str, float], set[str]]:
        """When each stored task was last read, and which reads failed.

        Rows imported before these fields existed are absent and so are read once.
        """

        read_at: dict[str, float] = {}
        errored: set[str] = set()
        try:
            for task_key, payload in self.raw_repo.iter_task_payloads():
                stamp = _coerce_number(payload.get(INSTRUCTIONS_READ_AT))
                if stamp is None and len(extract_narrative(payload)) == len(NARRATIVE_KEYS):
                    # Stored by a build that kept answers without stamping the
                    # attempt. A full narrative is proof enough that it was read,
                    # so treat it as read when the task was last touched -- not as
                    # read forever, which no later edit could ever exceed and
                    # which would freeze those answers even after Limble's change.
                    stamp = float(task_touched_at(payload))
                if stamp is None:
                    continue
                read_at[task_key] = stamp
                if str(payload.get(INSTRUCTIONS_READ_ERROR) or "").strip():
                    errored.add(task_key)
        except Exception as exc:  # noqa: BLE001 - a database without the table yet
            self._log(f"Could not check which tasks have been read ({exc}); considering all candidates.")
            return {}, set()
        return read_at, errored

    @staticmethod
    def _needs_reading(task: dict[str, Any], read_at: dict[str, float]) -> bool:
        """True when this task has not been read, or has changed since it was.

        A work order can be completed with its boxes blank and filled in later,
        so a task edited since the last read is worth asking about again. One
        that has not changed is not.
        """

        key = IngestionService._task_key(task)
        previous = read_at.get(key) if key is not None else None
        if previous is None:
            return True
        return task_touched_at(task) > previous

    @staticmethod
    def _completed_at(task: dict[str, Any]) -> float:
        """When this task was completed, as a sortable number (0 when unknown)."""

        number = _coerce_number(task.get("dateCompleted"))
        if number is None or number <= 0:
            return 0.0
        # Millisecond timestamps sort above second ones otherwise, putting a
        # handful of oddly-stored rows in front of everything real.
        return number / 1000.0 if number > 10_000_000_000 else number

    @staticmethod
    def _task_key(task: dict[str, Any]) -> str | None:
        value = task.get("taskID")
        if value in (None, ""):
            return None
        return str(value).strip() or None

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

        # Store downtime exactly as Limble reports it (seconds). It is the raw
        # source value; LifeDataService._parse_downtime_minutes is the single
        # place that normalises it (seconds -> minutes -> hours). Pre-scaling
        # here would double-convert against that assumption.
        downtime = task.get("downtime")
        if downtime not in (None, ""):
            record["downtime"] = downtime

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


def _utc_now() -> float:
    return datetime.now(tz=timezone.utc).timestamp()


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
