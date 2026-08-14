"""Run the Limble sync on demand, with progress a page can render.

The scheduled job (``python -m jobs.sync_limble``, run nightly by Task
Scheduler) is still the normal way GREMLIN.db is refreshed. This module lets a
developer start *the same* :class:`~services.ingestion_service.IngestionService`
run from the developer dashboard, for the days when waiting until tonight is not
an option: a Limble correction that has to reach the dashboards now, or checking
that credentials still work after they were rotated.

Two things make an on-demand sync different from the CLI one, and both are the
reason this module exists rather than the route simply calling ``sync_all``:

**It cannot block the request.** A full pull is minutes of paginated, rate
limited HTTP; an HTTP request that waited for it would time out in every proxy
between the browser and Flask. So the run happens on a background thread and the
page polls a status snapshot. Flask serves requests threaded, so the polls are
answered while the sync is still going.

**Someone is watching it.** The CLI prints to a console nobody reads at 2 a.m.;
this one has a person waiting, who deserves to know both what it is doing and
how long it has left. ``IngestionService`` therefore reports structured phase
progress, which this module weights into a single fraction, and the estimate of
how long the whole thing takes comes from how long previous syncs actually took.

Those timings are kept in a small JSON file beside the database
(``GREMLIN.sync-history.json``) rather than derived from ``import_batch``: a
batch row only opens once the fetching is done, so its start and end timestamps
measure the database write and nothing else -- a few seconds standing in for a
run whose minutes are almost entirely HTTP. The file is written by both this
module and the scheduled CLI job, so the nightly runs inform the estimate a
person sees at their desk, and it survives restarts. It is pure convenience:
every read and write is best effort, and losing it costs an estimate, not data.

Concurrency
-----------
One sync at a time per process, enforced by :class:`LimbleSyncRunner`'s lock.
That is not a distributed lock: the nightly Task Scheduler job is a different
process entirely, and could overlap with a run started here.

The database survives that -- SQLite serialises the writers through
``BEGIN IMMEDIATE``, and the import is idempotent -- but the *content* has one
sharp edge worth naming. Each run writes the snapshot it fetched, and
``RawRepository`` takes the last write for a task without comparing how old the
payload is. Two overlapping runs whose writes land in the opposite order to
their fetches therefore leave the earlier snapshot in place for any task edited
between them, until the next sync corrects it. It takes an overlap, an edit
inside the window, and reversed ordering (a rate-limit backoff is the plausible
cause), so it is a narrow case rather than an impossible one.

What this module does about it is warn: :meth:`LimbleSyncRunner.describe`
reports an import batch that is open and is not this run's own. It does not
prevent the overlap, because preventing it means deciding which run loses --
and a scheduled nightly import that silently does not happen is its own kind of
bad day. That decision belongs to whoever runs GREMLIN.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from collections import deque
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from integrations.limble import LimbleClient, LimbleConfig
from repositories.raw_repo import RawRepository
from services.ingestion_service import (
    PHASE_ASSETS,
    PHASE_MAP,
    PHASE_TASKS,
    PHASE_TRANSFORM,
    PHASE_WRITE,
    IngestionService,
)

# Job states reported to the client.
STATE_IDLE = "idle"
STATE_RUNNING = "running"
STATE_SUCCEEDED = "succeeded"
STATE_FAILED = "failed"

# import_batch.status values that mean "this run is over", one way or another.
_FINISHED_BATCH_STATUSES = frozenset({"COMPLETED", "FAILED", "CANCELLED"})

# Longest a sync is believed to take. Beyond it, an open import batch is treated
# as abandoned (a crashed run leaves its row STARTED forever) rather than
# reported as in flight, and a recorded timing is treated as a clock artefact
# rather than allowed to skew the estimate.
_MAX_RUN_SECONDS = 6 * 60 * 60

# Where past run timings live, and how many are kept. Five is enough for a
# median to ignore the one run that hit a rate-limit backoff, and few enough
# that the estimate follows the database as it grows.
_HISTORY_SUFFIX = ".sync-history.json"
_HISTORY_RUNS = 5


@dataclass(frozen=True)
class Phase:
    """One step of a sync, and its share of the total time.

    The weights are rough measurements, not guarantees: the two fetch phases
    dominate because Limble is paginated and rate limited at roughly one request
    per second, while the transform is pure CPU over already-fetched data. They
    exist to turn "phase 3 of 5, 1,200 of ~5,000 records" into a single fraction
    that moves at a believable pace, and to scale the time estimate when a run
    skips phases (a dry run does no writing, so it is expected to take only the
    fetch phases' share of a normal sync).
    """

    key: str
    label: str
    weight: float


PHASES: tuple[Phase, ...] = (
    Phase(PHASE_TASKS, "Fetching tasks from Limble", 0.40),
    Phase(PHASE_ASSETS, "Fetching assets from Limble", 0.20),
    Phase(PHASE_TRANSFORM, "Transforming records", 0.03),
    Phase(PHASE_WRITE, "Writing to GREMLIN.db", 0.27),
    Phase(PHASE_MAP, "Refreshing the mapped layer", 0.10),
)
_TOTAL_WEIGHT = sum(phase.weight for phase in PHASES)


class SyncAlreadyRunningError(RuntimeError):
    """Raised when a sync is asked for while one is already running here."""


class SyncOptionError(ValueError):
    """Raised when the requested options cannot be used."""


# ---------------------------------------------------------------------------
# Configuration helpers (shared with the CLI in jobs/sync_limble.py)
# ---------------------------------------------------------------------------


# Scopes already loaded, so the cache is per-scope: a restricted load must not
# convince a later unrestricted one that there is nothing left to read.
_dotenv_loaded_scopes: set[tuple[str | None, tuple[str, ...] | None]] = set()
# Values this loader put into the environment, so a forced reload can tell its
# own earlier work apart from a variable someone set outside the process.
_dotenv_applied: dict[str, str] = {}

# What the web process is allowed to take from a .env file.
#
# A .env is whole-application configuration, and the app reads some of it -- most
# importantly GREMLIN_DB_PATH -- on every request, while LifeDataService is built
# once and cached. So a process that had already answered a life-data request
# against one database could, the moment a status poll loaded a .env naming
# another, start pointing SchemaService and this sync at the second one while the
# analysis pages kept reading the first. Reading credentials must not be able to
# re-point the app, so the dashboard applies only the variables it came for. The
# CLI stays unrestricted: it is a fresh process that has built nothing yet, and
# GREMLIN_DB_PATH from .env is exactly how the scheduled job is configured.
LIMBLE_ENV_PREFIX = "LIMBLE_"

# Application settings the web process may take from a .env file. Unlike the
# credentials above these are read once, at import, before anything is built --
# which is the only safe moment for them. Loading GREMLIN_DB_PATH mid-process
# would be the hazard described above; loading it before the first service
# exists is simply configuration. GREMLIN_DB_PATH is deliberately not in this
# list all the same: a deployment with one in .env for the scheduled job would
# otherwise find the web app moving to that database on its next restart, which
# is a decision for whoever runs it rather than a side effect of this change.
APP_ENV_KEYS = frozenset({
    "GREMLIN_SECRET_KEY",
    "GREMLIN_ACCESS_DB_PATH",
    "GREMLIN_ADMIN_USERNAME",
    "GREMLIN_ADMIN_PIN",
    "GREMLIN_TRUSTED_PROXY_HOPS",
})


def _dotenv_candidates() -> list[Path]:
    """The ``.env`` files consulted, in precedence order.

    The working directory comes first because it is the deployment's own choice
    of configuration; the repo copy is the fallback for running out of a checkout.
    """

    return [Path.cwd() / ".env", Path(__file__).resolve().parents[1] / ".env"]


def _read_dotenv_values() -> dict[str, str]:
    """Merge the candidate files into one mapping, first occurrence winning.

    Precedence is resolved here rather than while assigning to ``os.environ``,
    because the two are not the same once a forced reload is allowed to replace
    its own earlier values: assigning file by file would let the repo fallback
    overwrite what the working directory just supplied, and a deployment whose
    two files disagree would sync the wrong database or account.
    """

    values: dict[str, str] = {}
    seen: set[Path] = set()
    for path in _dotenv_candidates():
        if path in seen or not path.is_file():
            continue
        seen.add(path)
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            if not key or key in values:
                continue
            values[key] = value.strip().strip('"').strip("'")
    return values


def load_dotenv_files(
    *,
    force: bool = False,
    only_prefix: str | None = None,
    only_keys: frozenset[str] | None = None,
) -> None:
    """Minimal ``.env`` loader (no third-party dependency).

    Reads ``.env`` from the current directory and the repo root, setting any
    variable that is not already present in the environment. The web app does
    not load ``.env`` at startup, so an on-demand sync has to do it for itself
    to find the same credentials the scheduled job uses.

    ``only_prefix`` limits which variables are applied, and the caching is per
    scope so a restricted load does not satisfy a later unrestricted one. The
    dashboard passes :data:`LIMBLE_ENV_PREFIX` for the reason given there; the
    CLI passes nothing and applies the file whole, as it always has.

    The result is cached because the status endpoint polls once a second while a
    sync runs and reports whether credentials are configured; ``force=True``
    re-reads the files, which is what starting a sync does so that credentials
    edited since the app started are picked up without a restart.

    A forced reload also *replaces* values it set on an earlier pass, which a
    plain "never overwrite the environment" loader would not: in a long-lived web
    process the first status poll already copied the old credentials into
    ``os.environ``, so without this, rotating ``.env`` would change nothing until
    someone restarted the app. A real environment variable still wins -- it is
    only overwritten when the current value is the one this loader wrote, which
    means nobody has set it since.
    """

    scope = (only_prefix, tuple(sorted(only_keys)) if only_keys is not None else None)
    if scope in _dotenv_loaded_scopes and not force:
        return
    _dotenv_loaded_scopes.add(scope)
    values = _read_dotenv_values()

    def _in_scope(name: str) -> bool:
        if only_keys is not None and name not in only_keys:
            return False
        return only_prefix is None or name.startswith(only_prefix)

    if force:
        # Deleting a credential from .env is how an operator withdraws it, so a
        # reload that only ever adds and replaces would keep a revoked secret in
        # use until the process restarted -- while reporting it as configured.
        # Owning a value means giving it up too.
        for key in [name for name in _dotenv_applied if _in_scope(name) and name not in values]:
            if os.environ.get(key) == _dotenv_applied[key]:
                os.environ.pop(key, None)
            # Either way this loader no longer owns the name: if the value has
            # been changed since, it belongs to whoever changed it.
            _dotenv_applied.pop(key, None)

    for key, value in values.items():
        if not _in_scope(key):
            continue
        current = os.environ.get(key)
        ours = _dotenv_applied.get(key)
        if current is None or (force and ours is not None and current == ours):
            os.environ[key] = value
            _dotenv_applied[key] = value


def parse_since(value: str | None) -> int | None:
    """Parse an ISO date/datetime or Unix timestamp into Unix seconds."""

    if not value:
        return None
    value = value.strip()
    if not value:
        return None
    if value.isdigit():
        return int(value)
    text = value.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        try:
            dt = datetime.strptime(value, "%Y-%m-%d")
        except ValueError as exc:
            raise SyncOptionError(
                f"'Only tasks touched since' must be a date (YYYY-MM-DD), an ISO datetime, "
                f"or a Unix timestamp; got {value!r}"
            ) from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


# Every option this endpoint understands. Anything else in a request body is a
# mistake worth reporting rather than dropping.
KNOWN_OPTION_KEYS = frozenset({"dry_run", "no_assets", "no_map", "include_templates", "since"})


@dataclass(frozen=True)
class SyncOptions:
    """The knobs the dashboard exposes, mirroring the CLI's flags."""

    dry_run: bool = False
    fetch_assets: bool = True
    refresh_mapping: bool = True
    include_templates: bool = False
    since: str | None = None

    @classmethod
    def from_payload(cls, payload: dict[str, Any] | None) -> "SyncOptions":
        """Build options from a JSON request body, validating as we go."""

        data = payload or {}
        if not isinstance(data, dict):
            raise SyncOptionError("Sync options must be a JSON object.")
        # An option this endpoint does not know is a misspelling of one it does,
        # and ignoring it decides the request the dangerous way: {"dryrun": true}
        # would run the full writing sync its author was trying to avoid. Say
        # which name was not understood rather than acting on a guess.
        unknown = sorted(set(data) - KNOWN_OPTION_KEYS)
        if unknown:
            raise SyncOptionError(
                f"Unknown sync option(s): {', '.join(unknown)}. "
                f"Valid options are: {', '.join(sorted(KNOWN_OPTION_KEYS))}."
            )
        since = data.get("since")
        if since is not None and not isinstance(since, str):
            raise SyncOptionError("'Only tasks touched since' must be text.")
        options = cls(
            dry_run=_as_bool("dry_run", data.get("dry_run")),
            fetch_assets=not _as_bool("no_assets", data.get("no_assets")),
            refresh_mapping=not _as_bool("no_map", data.get("no_map")),
            include_templates=_as_bool("include_templates", data.get("include_templates")),
            since=(since or "").strip() or None,
        )
        # Validate here, where the caller is still holding the request, so a bad
        # date is a 400 rather than a background job that fails a second later.
        parse_since(options.since)
        return options

    def to_dict(self) -> dict[str, Any]:
        return {
            "dry_run": self.dry_run,
            "fetch_assets": self.fetch_assets,
            "refresh_mapping": self.refresh_mapping,
            "include_templates": self.include_templates,
            "since": self.since,
        }

    def active_phases(self) -> tuple[Phase, ...]:
        """The phases this run will actually go through, in order."""

        skipped: set[str] = set()
        if not self.fetch_assets:
            skipped.add(PHASE_ASSETS)
        if self.dry_run:
            # A dry run stops after transforming: nothing is written, so nothing
            # can be mapped either.
            skipped.update({PHASE_WRITE, PHASE_MAP})
        elif not self.refresh_mapping:
            skipped.add(PHASE_MAP)
        return tuple(phase for phase in PHASES if phase.key not in skipped)


def phase_share(options: SyncOptions) -> float:
    """The fraction of a full sync's time this run's phases represent.

    Both entry points weigh a run through here, so a timing recorded by the
    nightly job is comparable with one recorded from the dashboard.
    """

    return sum(phase.weight for phase in options.active_phases()) / _TOTAL_WEIGHT


# Spellings accepted from a hand-written request. The page sends real JSON
# booleans; these exist so `curl -d '{"dry_run":"true"}'` behaves as its author
# expects rather than as Python truthiness would have it.
_TRUE_WORDS = frozenset({"1", "true", "yes", "on"})
_FALSE_WORDS = frozenset({"0", "false", "no", "off"})


def _as_bool(field: str, value: Any) -> bool:
    """Read one flag strictly, because guessing at it decides whether we write.

    Truthiness is the wrong rule here. ``{"dry_run": "treu"}`` is not a request
    for a real sync -- it is a typo by somebody who wanted a preview, and the
    quiet reading of it writes to the database they were being careful about.
    Anything that is not recognisably a boolean is a bad request, the same way a
    body that does not parse is.
    """

    # Absent, or an explicit JSON null: the option was not given.
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        text = value.strip().lower()
        if text in _TRUE_WORDS:
            return True
        if text in _FALSE_WORDS:
            return False
    raise SyncOptionError(f"'{field}' must be true or false; got {value!r}.")


# ---------------------------------------------------------------------------
# Import history (the last completed batch, and any import still in flight)
# ---------------------------------------------------------------------------


def _parse_db_timestamp(value: Any) -> datetime | None:
    """Parse an import_batch timestamp into an aware UTC datetime.

    Both writers store UTC: ``RawRepository`` formats ``%Y-%m-%d %H:%M:%S`` and
    SQLite's ``datetime('now')`` default produces the same shape. Older rows may
    hold an ISO string instead, so try that too, and treat anything else as
    unusable rather than guessing.
    """

    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    for parse in (datetime.fromisoformat, lambda raw: datetime.strptime(raw, "%Y-%m-%d %H:%M:%S")):
        try:
            parsed = parse(text)
        except (ValueError, TypeError):
            continue
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return None


def _readonly_connection(db_path: Path) -> sqlite3.Connection:
    """Open a ``mode=ro`` connection to the database.

    Read-only matters for the same reason it does on the rest of the developer
    dashboard: GREMLIN.db serialises writers through ``BEGIN IMMEDIATE`` on a
    shared drive, and a status poll must never queue behind (or ahead of) a real
    writer. Unlike the SQL console this runs one fixed, parameterless SELECT, so
    ``mode=ro`` alone is the whole guard -- there is no user SQL here for an
    authorizer to constrain.
    """

    uri = f"{db_path.resolve().as_uri()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=5.0)
    conn.row_factory = sqlite3.Row
    return conn


def read_import_history(
    db_path: str | Path,
    *,
    scan_limit: int = 25,
    exclude_batch_id: int | None = None,
) -> dict[str, Any]:
    """Report on recent import batches: the last completed one, and any still open.

    ``exclude_batch_id`` leaves one batch out of the "still open" answer. A run
    that is watching its own progress passes the batch it opened, so what comes
    back is an import belonging to somebody *else* -- which is the only kind
    worth warning about.

    Deliberately *not* a source of durations. A batch row is opened after the
    fetching finishes (see ``IngestionService.sync_all``), so the span between
    its start and end timestamps covers the database write alone -- seconds,
    for a run that spent minutes on HTTP. Timings come from
    :func:`read_run_timings` instead.

    Best effort by design. Every failure mode here (no database yet, a legacy
    ``import_batch`` without timestamps, an unreadable file) costs a line of
    context and nothing else, so it degrades to "no history" instead of failing
    the request that asked for status.
    """

    empty: dict[str, Any] = {
        "last_completed_at": None,
        "last_row_count": None,
        "in_flight": None,
        "available": False,
    }
    path = Path(db_path)
    if not path.is_file():
        return empty

    try:
        # closing(), not the connection's own context manager: that one only
        # commits or rolls back, so a status poll every second would leak a file
        # descriptor per poll.
        with closing(_readonly_connection(path)) as conn:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(import_batch)")}
            if not columns:
                return empty
            wanted = [
                column
                for column in ("import_batch_id", "status", "import_started_at", "import_completed_at", "raw_row_count")
                if column in columns
            ]
            if "import_started_at" not in wanted:
                return empty
            if "import_batch_id" not in columns:
                # A legacy table without the column still has the rowid the
                # writer's INSERT returned, so a batch can always be named --
                # which is what lets a run recognise its own.
                wanted = ["rowid AS import_batch_id", *wanted]
            order = "import_batch_id" if "import_batch_id" in columns else "rowid"
            rows = conn.execute(
                f"SELECT {', '.join(wanted)} FROM import_batch ORDER BY {order} DESC LIMIT ?",
                (max(1, scan_limit),),
            ).fetchall()
    except sqlite3.Error:
        return empty

    last_completed_at: str | None = None
    last_row_count: int | None = None
    in_flight: dict[str, Any] | None = None
    now = datetime.now(timezone.utc)

    for row in rows:
        keys = row.keys()
        status = str(row["status"]).strip().upper() if "status" in keys and row["status"] is not None else ""
        started = _parse_db_timestamp(row["import_started_at"])
        completed = _parse_db_timestamp(row["import_completed_at"]) if "import_completed_at" in keys else None

        # A batch that recorded a completion time is over, whatever its status
        # says -- and on a legacy import_batch with no status column at all (a
        # shape RawRepository.complete_batch deliberately still writes to), that
        # timestamp is the only signal there is. Reading a missing status as
        # "not finished" would make every completed import on such a database
        # look like a sync in progress for the next six hours.
        finished = status in _FINISHED_BATCH_STATUSES or completed is not None

        batch_id = row["import_batch_id"] if "import_batch_id" in keys else None
        ours = exclude_batch_id is not None and batch_id == exclude_batch_id
        if not finished and not ours and started is not None and in_flight is None:
            age = (now - started).total_seconds()
            # Ignore rows a crashed run left open: reporting a months-old
            # "still running" batch would just train people to ignore the
            # warning that matters.
            if 0 <= age <= _MAX_RUN_SECONDS:
                in_flight = {
                    "import_batch_id": batch_id,
                    "started_at": row["import_started_at"],
                    "age_seconds": round(age, 1),
                    "status": status or None,
                }

        if started is None or completed is None or last_completed_at is not None:
            continue
        # Prefer what the row says; fall back to its timestamps where the schema
        # cannot say. On a table without a status column a failed import is
        # indistinguishable from a successful one, so this can name a failure as
        # the last completed import -- its raw_row_count of 0 tells that story,
        # and the alternative is reporting nothing at all.
        if status and status != "COMPLETED":
            continue
        last_completed_at = row["import_completed_at"]
        raw_count = row["raw_row_count"] if "raw_row_count" in keys else None
        last_row_count = int(raw_count) if isinstance(raw_count, (int, float)) else None

    return {
        "last_completed_at": last_completed_at,
        "last_row_count": last_row_count,
        "in_flight": in_flight,
        "available": True,
    }


def _median(values: list[float]) -> float:
    """Median, which shrugs off the one sync that hit a rate-limit backoff."""

    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2


# ---------------------------------------------------------------------------
# Run timings (how long a sync of this database actually takes)
# ---------------------------------------------------------------------------


def run_timings_path(db_path: str | Path) -> Path:
    """Where this database's run timings are kept: beside the database itself.

    Beside it, rather than beside the code, because the timings describe *this*
    database -- how many records it holds and how long its syncs take -- and
    would be wrong for a different one opened by the same install.
    """

    path = Path(db_path)
    return path.with_name(path.stem + _HISTORY_SUFFIX)


def read_run_timings(db_path: str | Path) -> list[dict[str, Any]]:
    """Previous run timings, newest first. Any problem reads as "none recorded"."""

    try:
        raw = json.loads(run_timings_path(db_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    runs = raw.get("runs") if isinstance(raw, dict) else None
    if not isinstance(runs, list):
        return []
    return [run for run in runs if isinstance(run, dict) and isinstance(run.get("full_seconds"), (int, float))]


def record_run_timing(
    db_path: str | Path,
    *,
    seconds: float,
    share: float = 1.0,
    counts: dict[str, Any] | None = None,
) -> None:
    """Remember how long a completed sync took, for the next run's estimate.

    ``share`` is the fraction of a full sync's phases this run performed, so a
    run that skipped the write is stored as what a full one would have cost and
    the two remain comparable. Best effort: a database directory that cannot be
    written to costs the estimate, never the sync that just succeeded.
    """

    # A run measured in milliseconds is not a sample of anything (a real sync
    # spends seconds on its first HTTP call alone), and would only take a slot
    # from a timing worth keeping.
    if seconds < 0.05 or share <= 0:
        return
    # No database, nothing to describe: a dry run against a path that does not
    # exist should not leave a timings file behind in that directory.
    if not Path(db_path).is_file():
        return
    entry = {
        "at": _utc_now_iso(),
        "seconds": round(seconds, 1),
        "share": round(share, 4),
        "full_seconds": round(seconds / share, 1),
    }
    for key in (PHASE_TASKS, PHASE_ASSETS):
        value = (counts or {}).get(key)
        if isinstance(value, int) and value > 0:
            entry[key] = value

    path = run_timings_path(db_path)
    runs = [entry] + read_run_timings(db_path)
    try:
        # Write-then-replace so a reader (or a crash) never sees half a file.
        # The scratch name carries the process id because the web app and the
        # scheduled job can be writing beside the same database at once, and one
        # shared scratch file would let them interleave into each other's bytes.
        # Whoever replaces last still wins the file, which costs at most one
        # timing sample out of five and needs no lock to be safe.
        temporary = path.with_suffix(f"{path.suffix}.{os.getpid()}.tmp")
        temporary.write_text(
            json.dumps({"runs": runs[:_HISTORY_RUNS]}, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        os.replace(temporary, path)
    except OSError:
        return


def summarise_run_timings(db_path: str | Path) -> dict[str, Any]:
    """Typical full-sync duration and record counts from previous runs."""

    runs = read_run_timings(db_path)
    durations = [float(run["full_seconds"]) for run in runs if 0 < float(run["full_seconds"]) <= _MAX_RUN_SECONDS]
    counts: dict[str, int] = {}
    for phase in (PHASE_TASKS, PHASE_ASSETS):
        for run in runs:
            value = run.get(phase)
            if isinstance(value, int) and value > 0:
                counts[phase] = value
                break
    return {
        "runs": len(durations),
        "median_full_seconds": _median(durations) if durations else None,
        "last_run_at": runs[0].get("at") if runs else None,
        "counts": counts,
    }


def _raw_record_count(db_path: Path) -> int | None:
    """Rows in raw_cmms_record, as a fallback estimate of the task count."""

    if not db_path.is_file():
        return None
    try:
        with closing(_readonly_connection(db_path)) as conn:
            if not {row[1] for row in conn.execute("PRAGMA table_info(raw_cmms_record)")}:
                return None
            return int(conn.execute("SELECT COUNT(*) FROM raw_cmms_record").fetchone()[0] or 0)
    except sqlite3.Error:
        return None


def _limble_config_or_error() -> tuple[LimbleConfig | None, str | None]:
    try:
        return LimbleConfig.from_env(), None
    except ValueError as exc:
        return None, str(exc)


def describe_credentials(*, reload_env: bool = False) -> dict[str, Any]:
    """Report whether Limble credentials are configured, without revealing them."""

    load_dotenv_files(force=reload_env, only_prefix=LIMBLE_ENV_PREFIX)
    config, detail = _limble_config_or_error()
    if config is None and not reload_env:
        # "Missing" may only mean the cached load ran before anyone wrote the
        # file. That would otherwise be a trap with no way out: the page disables
        # the Run button when credentials are missing, and starting a sync is the
        # only thing that forces a re-read -- so an operator who adds .env to a
        # running app would be told to restart it for no reason. Re-reading costs
        # two stat calls, and only in the state that is already broken.
        load_dotenv_files(force=True, only_prefix=LIMBLE_ENV_PREFIX)
        config, detail = _limble_config_or_error()
    if config is None:
        return {"configured": False, "detail": detail, "base_url": os.getenv("LIMBLE_BASE_URL") or None}
    return {"configured": True, "detail": None, "base_url": config.base_url}


# ---------------------------------------------------------------------------
# The runner
# ---------------------------------------------------------------------------


class LimbleSyncRunner:
    """Runs one Limble sync at a time on a background thread, and reports on it.

    A single instance is shared by every request in the process (see ``app.py``),
    so all of its mutable state is guarded by one lock: the worker thread writes
    progress into it while request threads read snapshots out of it.
    """

    def __init__(self, *, max_messages: int = 200, on_success: Callable[[], None] | None = None) -> None:
        self._lock = threading.Lock()
        # Called after a sync that wrote, before the status turns terminal, so
        # the host process can drop caches this run just invalidated. The runner
        # cannot know what those are -- it is the app that holds them.
        self._on_success = on_success
        self._messages: deque[dict[str, str]] = deque(maxlen=max_messages)
        self._thread: threading.Thread | None = None
        self._job: dict[str, Any] = {"state": STATE_IDLE}
        # Counts seen on the last run in this process, used to interpolate the
        # fetch phases (the API never says how many records are coming).
        self._observed: dict[str, int] = {}

    # -- public API --------------------------------------------------------
    def start(self, db_path: str | Path, options: SyncOptions, *, log: Callable[[str], None] = print) -> dict[str, Any]:
        """Start a sync in the background and return the initial status."""

        path = Path(db_path)
        # Fail early on the thing that is certain to fail late: the CLI requires
        # an existing database (only ``--create`` makes one), and finding out
        # after a five minute fetch that there was nowhere to put the data is a
        # waste of everybody's time.
        if not options.dry_run and not path.is_file():
            raise SyncOptionError(
                f"GREMLIN.db was not found at {path}. Set GREMLIN_DB_PATH to a reachable "
                "GREMLIN.db file, or run the sync from the command line with --create."
            )

        history = read_import_history(path)
        timings = summarise_run_timings(path)
        expected = self._expected_counts(path, history, timings)
        active = options.active_phases()
        share = phase_share(options)
        estimate, basis = self._estimate(timings, share)

        with self._lock:
            if self._job.get("state") == STATE_RUNNING:
                worker = self._thread
                if worker is not None and worker.is_alive():
                    raise SyncAlreadyRunningError(
                        "A Limble sync is already running. Wait for it to finish before starting another."
                    )
                # The worker is gone without having reported an outcome (only an
                # interpreter-level failure gets past its own except clause).
                # Close the job out rather than let a dead thread block every
                # future sync until someone restarts the app. This is sound only
                # because a recorded worker has always been started before the
                # lock was released -- see thread.start() below.
                self._job["state"] = STATE_FAILED
                self._job["error"] = "The sync stopped without reporting a result."
            self._messages.clear()
            # Appended directly: _append_message takes the same (non-reentrant)
            # lock this block holds, and the line has to land before the worker's
            # own first message.
            self._messages.append({"at": _utc_now_iso(), "text": "Sync requested from the developer dashboard."})
            self._job = {
                "state": STATE_RUNNING,
                "options": options.to_dict(),
                "phase": None,
                "phase_label": "Starting",
                "phase_index": 0,
                "phase_count": len(active),
                "progress": 0.0,
                "counts": {},
                # What each phase is counting towards: `targets` is what the
                # phase itself reported (exact), `expected` is this module's
                # guess for the phases that cannot know (the API never says how
                # many records are coming). The page labels the two differently.
                "targets": {},
                "expected": dict(expected),
                "started_at": _utc_now_iso(),
                "finished_at": None,
                "elapsed_seconds": 0.0,
                "estimated_total_seconds": estimate,
                "estimate_basis": basis,
                "summary": None,
                "error": None,
                "db_path": str(path),
                "_monotonic_start": time.monotonic(),
                "_active": active,
                "_share": share,
                # Set once this run opens its import_batch, which does not
                # happen until the fetching is done.
                "_batch_id": None,
            }
            thread = threading.Thread(
                target=self._run,
                args=(path, options, log),
                name="limble-sync",
                daemon=True,
            )
            self._thread = thread
            # Started while still holding the lock. A thread that is recorded but
            # not yet running is not alive, so a second request arriving in that
            # window would read the job as abandoned, take it over, and leave two
            # syncs fetching and writing at once. Holding the lock across the
            # start makes "recorded" and "running" the same state. The worker
            # blocks on its first status write until this block ends, which is
            # microseconds; it cannot deadlock, because nothing here waits on it.
            thread.start()

        return self.status()

    def status(self) -> dict[str, Any]:
        """A JSON-safe snapshot of the current (or most recent) run."""

        with self._lock:
            job = dict(self._job)
            messages = list(self._messages)

        state = job.get("state", STATE_IDLE)
        if state == STATE_RUNNING and job.get("_monotonic_start") is not None:
            job["elapsed_seconds"] = round(time.monotonic() - job["_monotonic_start"], 1)
        estimate = job.get("estimated_total_seconds")
        elapsed = job.get("elapsed_seconds")
        remaining = None
        if state == STATE_RUNNING and estimate is not None and elapsed is not None:
            remaining = round(max(estimate - elapsed, 0.0), 1)
        job["estimated_remaining_seconds"] = remaining
        job["messages"] = messages
        # Internal bookkeeping never crosses the wire.
        for key in [key for key in job if key.startswith("_")]:
            job.pop(key)
        return job

    def describe(self, db_path: str | Path) -> dict[str, Any]:
        """Everything the sync panel needs: job status, credentials, history."""

        path = Path(db_path)
        job = self.status()
        # Leave this run's own batch out rather than every open batch: the row
        # only appears once fetching is done, so for most of a run any open
        # batch belongs to somebody else -- the nightly job, most likely, which
        # is exactly the overlap the warning is for. Suppressing all of them
        # while running hid the warning precisely when it was true.
        with self._lock:
            own_batch_id = self._job.get("_batch_id") if self._job.get("state") == STATE_RUNNING else None
        history = read_import_history(path, exclude_batch_id=own_batch_id)
        timings = summarise_run_timings(path)
        in_flight = history.get("in_flight")
        return {
            "job": job,
            "credentials": describe_credentials(),
            "db_path": str(path),
            "db_exists": path.is_file(),
            "history": {
                "last_completed_at": history.get("last_completed_at"),
                "last_row_count": history.get("last_row_count"),
                "timed_runs": timings.get("runs"),
                "median_seconds": timings.get("median_full_seconds"),
            },
            "in_flight_batch": in_flight,
        }

    # -- the run itself ----------------------------------------------------
    def _run(self, db_path: Path, options: SyncOptions, log: Callable[[str], None]) -> None:
        try:
            # Re-read .env on every run so rotated credentials take effect
            # without restarting the app -- credentials only: a sync must not
            # re-point the process at a different database on its way past.
            load_dotenv_files(force=True, only_prefix=LIMBLE_ENV_PREFIX)
            updated_since = parse_since(options.since)
            config = LimbleConfig.from_env()
            client = LimbleClient(config, log=self._make_logger(log))
            service = IngestionService(
                limble_client=client,
                raw_repo=RawRepository(db_path),
                fetch_assets=options.fetch_assets,
                refresh_mapping=options.refresh_mapping,
                exclude_templates=not options.include_templates,
                log=self._make_logger(log),
                progress=self._on_progress,
                on_batch_started=self._remember_batch,
            )
            self._append_message(f"Database: {db_path}")
            self._append_message(f"Limble base URL: {config.base_url}")
            if updated_since is not None:
                cutoff = datetime.fromtimestamp(updated_since, tz=timezone.utc).isoformat()
                self._append_message(f"Filtering to tasks touched since {cutoff}.")
            summary = service.sync_all(updated_since=updated_since, dry_run=options.dry_run)
        except Exception as exc:  # noqa: BLE001 - the worker thread must never die silently
            self._append_message(f"Sync failed: {exc}")
            self._finish(STATE_FAILED, summary=None, error=str(exc))
            return

        self._append_message("Sync complete.")
        self._finish(STATE_SUCCEEDED, summary=summary, error=None)

    def _finish(self, state: str, *, summary: dict[str, Any] | None, error: str | None) -> None:
        with self._lock:
            share = self._job.get("_share") or 1.0
            db_path = self._job.get("db_path")
            counts = dict(self._job.get("counts") or {})
            dry_run = bool((self._job.get("options") or {}).get("dry_run"))
            # Measured from the same clock the page has been ticking against, so
            # the final "took 2m 14s" agrees with the last elapsed it displayed,
            # and excludes the bookkeeping below.
            start = self._job.get("_monotonic_start")
            duration = time.monotonic() - start if start is not None else 0.0

        # Everything a successful run owes the world happens here, before the
        # status says "succeeded" and while the lock is *not* held: writing the
        # timing file touches a possibly-slow shared drive, and a status poll
        # must not queue behind it. Publishing first would be a lie the callers
        # act on -- a watcher that sees a terminal state is entitled to start
        # another sync or tear the database directory down, and both would race
        # this work.
        if state == STATE_SUCCEEDED:
            if db_path:
                record_run_timing(db_path, seconds=duration, share=share, counts=counts)
            if not dry_run:
                self._announce_success()

        with self._lock:
            self._job.update(
                {
                    "state": state,
                    "phase": None,
                    "phase_label": "Finished" if state == STATE_SUCCEEDED else "Stopped",
                    "progress": 1.0 if state == STATE_SUCCEEDED else self._job.get("progress"),
                    "finished_at": _utc_now_iso(),
                    "elapsed_seconds": round(duration, 1),
                    "summary": summary,
                    "error": error,
                }
            )
            if state != STATE_SUCCEEDED:
                return
            for key in (PHASE_TASKS, PHASE_ASSETS):
                value = counts.get(key)
                if isinstance(value, int) and value > 0:
                    self._observed[key] = value

    def _announce_success(self) -> None:
        """Tell the host process a sync changed the database under it."""

        if self._on_success is None:
            return
        try:
            self._on_success()
        except Exception as exc:  # noqa: BLE001 - a stale cache must not fail a good sync
            self._append_message(f"Warning: could not refresh the app's cached data ({exc}).")

    # -- progress ----------------------------------------------------------
    def _on_progress(self, phase: str, current: int | None, total: int | None) -> None:
        """Fold one phase report into the overall fraction. Runs on the worker."""

        with self._lock:
            if self._job.get("state") != STATE_RUNNING:
                return
            active: tuple[Phase, ...] = self._job.get("_active") or PHASES
            expected: dict[str, int] = self._job.get("expected") or {}

            done = 0.0
            index = 0
            fraction = self._job.get("progress") or 0.0
            for position, entry in enumerate(active, start=1):
                if entry.key != phase:
                    done += entry.weight
                    continue
                index = position
                target = total or expected.get(phase)
                within = 0.0
                if current is not None and target:
                    # Cap below 1: a phase is only finished when the next one
                    # starts, and the estimate of `target` can be too low.
                    within = min(max(current / target, 0.0), 0.99)
                fraction = (done + entry.weight * within) / sum(item.weight for item in active)
                break

            if current is not None:
                counts = dict(self._job.get("counts") or {})
                counts[phase] = current
                self._job["counts"] = counts
            if total:
                targets = dict(self._job.get("targets") or {})
                targets[phase] = total
                self._job["targets"] = targets
            self._job["phase"] = phase
            self._job["phase_label"] = _phase_label(phase)
            self._job["phase_index"] = index
            # Never let the bar go backwards: phases report their own counts, so
            # a later phase with an over-generous estimate could otherwise
            # compute a smaller fraction than the phase before it.
            self._job["progress"] = round(max(fraction, self._job.get("progress") or 0.0), 4)

    def _remember_batch(self, batch_id: int) -> None:
        """Note the import_batch this run opened, so it is not mistaken for another's."""

        with self._lock:
            if self._job.get("state") == STATE_RUNNING:
                self._job["_batch_id"] = batch_id

    def _make_logger(self, log: Callable[[str], None]) -> Callable[[str], None]:
        """Tee a component's log lines to the status feed and the server console."""

        def _log(message: str) -> None:
            self._append_message(str(message))
            try:
                log(f"[sync] {message}")
            except Exception:  # noqa: BLE001 - a broken console must not stop a sync
                pass

        return _log

    def _append_message(self, text: str) -> None:
        with self._lock:
            self._messages.append({"at": _utc_now_iso(), "text": text})

    # -- estimation --------------------------------------------------------
    def _expected_counts(self, db_path: Path, history: dict[str, Any], timings: dict[str, Any]) -> dict[str, int]:
        """Best guess at how many records each fetch phase will see.

        Preference order is most-specific first: what this process actually
        observed last time, then what a previous run recorded, then the row
        count the last completed import wrote, then the size of the raw table.
        All of them are guesses -- the bar caps each phase below 100% so an
        undercount cannot look like completion.
        """

        expected: dict[str, int] = {}
        # Least specific first: each source overwrites the one before it, so the
        # counts this process saw itself win over the ones read off disk.
        for source in (timings.get("counts") or {}, self._observed):
            for key, value in source.items():
                if isinstance(value, int) and value > 0:
                    expected[key] = value
        if PHASE_TASKS not in expected:
            fallback = history.get("last_row_count") or _raw_record_count(db_path)
            if isinstance(fallback, int) and fallback > 0:
                expected[PHASE_TASKS] = fallback
        return expected

    def _estimate(self, timings: dict[str, Any], share: float) -> tuple[float | None, str | None]:
        """Estimate this run's duration from previous runs, scaled by its phases."""

        median = timings.get("median_full_seconds")
        runs = timings.get("runs") or 0
        if not median:
            return None, None
        plural = "s" if runs != 1 else ""
        return round(median * share, 1), f"median of the last {runs} timed sync{plural}"


def _phase_label(phase: str) -> str:
    for entry in PHASES:
        if entry.key == phase:
            return entry.label
    return phase


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
