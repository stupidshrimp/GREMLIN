r"""Sync Limble CMMS data into GREMLIN.db.

This is the script that replaces the old Excel import: it pulls tasks (and
assets, for enrichment) from the Limble API and upserts them into the raw import
tables of GREMLIN.db, then refreshes the mapped layer the app reads.

Usage
-----
    python -m jobs.sync_limble            # preferred
    python jobs/sync_limble.py            # also works

Configuration
-------------
Credentials (first non-empty wins), via environment, a ``.env`` file, or flags:

    LIMBLE_CLIENT_ID      / LIMBLE_API_CLIENTID   (or --client-id)
    LIMBLE_CLIENT_SECRET  / LIMBLE_API_KEY        (or --client-secret)
    LIMBLE_BASE_URL                               (or --base-url)

Database location (first non-empty wins):

    --db <path>           explicit path
    GREMLIN_DB_PATH       environment variable
    (otherwise C:\GREMLIN\GREMLIN.db is used and must already exist)

Common examples
---------------
    # Full sync into a specific database file
    GREMLIN_DB_PATH=/data/GREMLIN.db python -m jobs.sync_limble

    # Only tasks touched since a date, no asset enrichment, preview only
    python -m jobs.sync_limble --since 2026-01-01 --no-assets --dry-run
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

if __package__ in (None, ""):
    # Allow script-mode execution from the repo root.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from integrations.limble import LimbleClient, LimbleConfig
from repositories.raw_repo import RawRepository
from services.ingestion_service import PHASE_ASSETS, PHASE_TASKS, IngestionService
from services.ingestion_service import DEFAULT_INSTRUCTIONS_LIMIT
from services.sync_service import (
    SyncOptions,
    load_dotenv_files,
    parse_since,
    phase_share,
    record_run_timing,
)


def _parse_since(value: str | None) -> int | None:
    """Parse ``--since`` (ISO date/datetime or a Unix timestamp) into Unix seconds.

    The parsing itself is shared with the dashboard's on-demand sync
    (:mod:`services.sync_service`); this wrapper only restates a bad value as a
    CLI error, in the CLI's own vocabulary.
    """

    try:
        return parse_since(value)
    except ValueError as exc:
        raise SystemExit(
            f"--since must be a date (YYYY-MM-DD), ISO datetime, or Unix timestamp; got {value!r}"
        ) from exc


def _resolve_db_path(explicit: str | None, *, must_exist: bool, create: bool) -> Path:
    db_path = explicit or os.getenv("GREMLIN_DB_PATH")
    was_explicit = bool(db_path)
    if not db_path:
        try:
            from services.life_data_service import DEFAULT_DB_PATH

            db_path = str(DEFAULT_DB_PATH)
        except Exception:  # noqa: BLE001 - fall through to the error below
            db_path = None
    if not db_path:
        raise SystemExit("No database path. Set GREMLIN_DB_PATH or pass --db /path/to/GREMLIN.db.")
    path = Path(db_path)
    if create:
        path.parent.mkdir(parents=True, exist_ok=True)
        return path
    if must_exist and not path.is_file():
        hint = "" if was_explicit else " (the default database location)"
        raise SystemExit(
            f"GREMLIN.db not found at: {path}{hint}\n"
            "Pass --db to point at an existing database, set GREMLIN_DB_PATH, "
            "or pass --create to create a new database at that path."
        )
    return path


def _positive_count(value: str) -> int:
    """A count of tasks, for --instructions-limit.

    argparse would take ``-1`` happily, and a cap that is not a cap is the worst
    reading of it: an unbounded request-per-task walk of the whole history, hours
    long, from a flag whose whole purpose was to bound the run.
    """

    try:
        count = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected a whole number of tasks, got {value!r}") from None
    if count < 0:
        raise argparse.ArgumentTypeError(f"cannot be negative, got {count}")
    return count


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Sync Limble CMMS data into GREMLIN.db.")
    parser.add_argument("--db", help="Path to GREMLIN.db (overrides GREMLIN_DB_PATH).")
    parser.add_argument("--client-id", help="Limble API client id (overrides env).")
    parser.add_argument("--client-secret", help="Limble API client secret (overrides env).")
    parser.add_argument("--base-url", help="Limble API base URL (default https://api.limblecmms.com/v2).")
    parser.add_argument(
        "--since",
        help=(
            "Only import tasks touched on/after this date (YYYY-MM-DD), ISO datetime, or "
            "Unix timestamp. Note this narrows what is *imported*, not what is fetched: "
            "/tasks has no server-side date filter, so the whole history is pulled and "
            "then filtered here. It does not make the fetch shorter."
        ),
    )
    parser.add_argument("--page-limit", type=int, default=200, help="Records per API page (default 200).")
    parser.add_argument("--no-assets", action="store_true", help="Skip the /assets fetch used for name/hierarchy enrichment.")
    parser.add_argument("--no-map", action="store_true", help="Skip refreshing mapped_cmms_record after import.")
    parser.add_argument(
        "--include-templates",
        action="store_true",
        help="Import Limble template tasks too (excluded by default, matching the legacy export).",
    )
    parser.add_argument(
        "--no-instructions",
        action="store_true",
        help=(
            "Skip reading the Area Affected / Condition / Cause / Action boxes. They live in "
            "each work order's instructions, which Limble serves one task at a time, so this "
            "is the flag to reach for when a sync needs to be as quick as possible."
        ),
    )
    parser.add_argument(
        "--instructions-limit",
        type=_positive_count,
        default=DEFAULT_INSTRUCTIONS_LIMIT,
        help=(
            f"How many work orders to read instructions for (default {DEFAULT_INSTRUCTIONS_LIMIT}). "
            "The most recently completed are read first and each run picks up where the last "
            "stopped, so raise this to work through a backlog faster."
        ),
    )
    parser.add_argument("--dry-run", action="store_true", help="Fetch and transform, but make no database changes.")
    parser.add_argument("--create", action="store_true", help="Create the database file if it does not exist.")
    return parser


def run(args: argparse.Namespace) -> dict:
    load_dotenv_files(force=True)
    updated_since = _parse_since(args.since)
    # Dry-run never writes, so it doesn't require an existing database.
    if args.dry_run:
        db_path = _resolve_db_path(args.db, must_exist=False, create=False)
    else:
        db_path = _resolve_db_path(args.db, must_exist=not args.create, create=args.create)

    config = LimbleConfig.from_env(
        client_id=args.client_id,
        client_secret=args.client_secret,
        base_url=args.base_url,
        page_limit=args.page_limit,
    )
    client = LimbleClient(config)
    raw_repo = RawRepository(db_path)
    service = IngestionService(
        limble_client=client,
        raw_repo=raw_repo,
        fetch_assets=not args.no_assets,
        refresh_mapping=not args.no_map,
        exclude_templates=not args.include_templates,
        fetch_instructions=not args.no_instructions,
        instructions_limit=args.instructions_limit,
        progress=_print_progress,
    )

    print(f"Database: {db_path}")
    print(f"Limble base URL: {config.base_url}")
    if updated_since is not None:
        print(f"Filtering to tasks touched since: {datetime.fromtimestamp(updated_since, tz=timezone.utc).isoformat()}")

    started = time.monotonic()
    summary = service.sync_all(updated_since=updated_since, dry_run=args.dry_run)
    # Time the scheduled run too, so the dashboard's "how long will this take"
    # is answered from the runs that actually happen -- most of which happen
    # here, at night, and never touch the web app. Best effort: a directory that
    # cannot be written to costs an estimate, not this import.
    #
    # The instructions phase is taken back out rather than the whole run being
    # skipped. It is paced by the API's rate limit instead of by how much work
    # the sync did, so leaving it in would put a backfill's hours into the
    # estimate shown before an ordinary run -- but skipping those runs entirely,
    # now that reading instructions is what a sync normally does, would leave the
    # estimate with almost nothing to learn from.
    elapsed = time.monotonic() - started - float(summary.get("instructions_seconds") or 0.0)
    record_run_timing(
        db_path,
        seconds=max(0.0, elapsed),
        share=phase_share(_options_for(args)),
        counts={PHASE_TASKS: summary.get("fetched_tasks"), PHASE_ASSETS: summary.get("fetched_assets")},
    )
    return summary


# How often the fetch phases say something. Limble's list endpoints are paged at
# 200 and spaced a second apart, so a full task pull is thousands of seconds of
# silence otherwise -- long enough to look hung and be killed halfway.
_PROGRESS_EVERY = 1000
_last_reported: dict[str, int] = {}


def _print_progress(phase: str, current: int | None, total: int | None) -> None:
    """Narrate a long phase, sparsely enough not to bury the summary."""

    if current is None:
        return
    previous = _last_reported.get(phase, 0)
    finished = total is not None and current >= total
    if not finished and current - previous < _PROGRESS_EVERY:
        return
    _last_reported[phase] = current
    if total:
        print(f"  {phase}: {current} of {total}", flush=True)
    else:
        # The list endpoints do not report a total, so say what is known rather
        # than inventing a denominator.
        print(f"  {phase}: {current} so far ...", flush=True)


def _options_for(args: argparse.Namespace) -> SyncOptions:
    """Restate the CLI flags as SyncOptions, so both entry points weigh a run alike."""

    return SyncOptions(
        dry_run=args.dry_run,
        fetch_assets=not args.no_assets,
        refresh_mapping=not args.no_map,
        include_templates=args.include_templates,
        fetch_instructions=not args.no_instructions,
        instructions_limit=args.instructions_limit,
    )


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    try:
        summary = run(args)
    except SystemExit:
        raise
    except ValueError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - top-level CLI guard
        print(f"Sync failed: {exc}", file=sys.stderr)
        return 1
    print("\nSync complete:")
    for key, value in summary.items():
        print(f"  {key}: {value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
