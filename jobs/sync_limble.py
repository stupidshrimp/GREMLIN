"""Sync Limble CMMS data into GREMLIN.db.

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
    (otherwise the shared GREMLIN.db default is probed and must already exist)

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
from datetime import datetime, timezone
from pathlib import Path

if __package__ in (None, ""):
    # Allow script-mode execution from the repo root.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from integrations.limble import LimbleClient, LimbleConfig
from repositories.raw_repo import RawRepository
from services.ingestion_service import IngestionService


def _load_dotenv() -> None:
    """Minimal ``.env`` loader (no third-party dependency).

    Reads ``.env`` from the current directory and the repo root, setting any
    variable that is not already present in the environment.
    """

    candidates = [Path.cwd() / ".env", Path(__file__).resolve().parents[1] / ".env"]
    seen: set[Path] = set()
    for path in candidates:
        if path in seen or not path.is_file():
            continue
        seen.add(path)
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


def _parse_since(value: str | None) -> int | None:
    """Parse ``--since`` (ISO date/datetime or a Unix timestamp) into Unix seconds."""

    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return int(value)
    text = value.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        try:
            dt = datetime.strptime(value, "%Y-%m-%d")
        except ValueError as exc:
            raise SystemExit(f"--since must be a date (YYYY-MM-DD), ISO datetime, or Unix timestamp; got {value!r}") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def _resolve_db_path(explicit: str | None, *, must_exist: bool, create: bool) -> Path:
    db_path = explicit or os.getenv("GREMLIN_DB_PATH")
    was_explicit = bool(db_path)
    if not db_path:
        try:
            from services.life_data_service import resolve_default_db_path

            db_path = str(resolve_default_db_path())
        except Exception:  # noqa: BLE001 - fall through to the error below
            db_path = None
    if not db_path:
        raise SystemExit("No database path. Set GREMLIN_DB_PATH or pass --db /path/to/GREMLIN.db.")
    path = Path(db_path)
    if create:
        path.parent.mkdir(parents=True, exist_ok=True)
        return path
    if must_exist and not path.is_file():
        hint = "" if was_explicit else " (the shared default could not be reached)"
        raise SystemExit(
            f"GREMLIN.db not found at: {path}{hint}\n"
            "Pass --db to point at an existing database, set GREMLIN_DB_PATH, "
            "or pass --create to create a new database at that path."
        )
    return path


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Sync Limble CMMS data into GREMLIN.db.")
    parser.add_argument("--db", help="Path to GREMLIN.db (overrides GREMLIN_DB_PATH).")
    parser.add_argument("--client-id", help="Limble API client id (overrides env).")
    parser.add_argument("--client-secret", help="Limble API client secret (overrides env).")
    parser.add_argument("--base-url", help="Limble API base URL (default https://api.limblecmms.com/v2).")
    parser.add_argument(
        "--since",
        help="Only import tasks touched on/after this date (YYYY-MM-DD), ISO datetime, or Unix timestamp.",
    )
    parser.add_argument(
        "--downtime-unit",
        choices=["minutes", "seconds", "hours"],
        default=None,
        help=(
            "Unit of Limble's task 'downtime' field; it is normalised to minutes on write. "
            "Falls back to the LIMBLE_DOWNTIME_UNIT env/.env value, then 'minutes'."
        ),
    )
    parser.add_argument("--page-limit", type=int, default=200, help="Records per API page (default 200).")
    parser.add_argument("--no-assets", action="store_true", help="Skip the /assets fetch used for name/hierarchy enrichment.")
    parser.add_argument("--no-map", action="store_true", help="Skip refreshing mapped_cmms_record after import.")
    parser.add_argument("--dry-run", action="store_true", help="Fetch and transform, but make no database changes.")
    parser.add_argument("--create", action="store_true", help="Create the database file if it does not exist.")
    return parser


def run(args: argparse.Namespace) -> dict:
    _load_dotenv()
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
    # Resolve the downtime unit only after .env is loaded so a value configured
    # there (LIMBLE_DOWNTIME_UNIT) is honoured rather than the bare default.
    downtime_unit = args.downtime_unit or os.getenv("LIMBLE_DOWNTIME_UNIT") or "minutes"
    client = LimbleClient(config)
    raw_repo = RawRepository(db_path)
    service = IngestionService(
        limble_client=client,
        raw_repo=raw_repo,
        downtime_unit=downtime_unit,
        fetch_assets=not args.no_assets,
        refresh_mapping=not args.no_map,
    )

    print(f"Database: {db_path}")
    print(f"Limble base URL: {config.base_url}")
    if updated_since is not None:
        print(f"Filtering to tasks touched since: {datetime.fromtimestamp(updated_since, tz=timezone.utc).isoformat()}")
    return service.sync_all(updated_since=updated_since, dry_run=args.dry_run)


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
