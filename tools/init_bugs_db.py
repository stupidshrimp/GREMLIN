r"""Create the bug reports database, or report on the one that is already there.

GREMLIN does not need this to work. :class:`services.bug_reports.BugReportStore`
creates the folder, the file and the tables the first time anything touches it,
so the first person to file a report makes ``auxillary.db`` on the way past.

This exists because that is a bad moment to discover the share is not writable.
Whoever installs GREMLIN wants the file made deliberately -- while they are at a
machine with the drive mapped, can set permissions on it, and can see that it
worked -- rather than by a reporter who gets an error instead of filing, on a
day when something is already broken enough to be worth reporting.

It is also the answer to "there is no .db file on the share": run this and there
is one, or run it with --check and find out in one line why there is not.

Usage::

    python tools/init_bugs_db.py                     # create if missing, then report
    python tools/init_bugs_db.py --check             # report only; never creates
    python tools/init_bugs_db.py --db D:\GREMLIN Global DB\auxillary.db

The path is taken from --db, then GREMLIN_BUGS_DB_PATH, then the shared-drive
default -- the same order the web app resolves it in, so what this creates is
what the app will open.

Exit status is 0 when the database is there and complete, and 1 when it is not:
missing under --check, or unreachable either way.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.bug_reports import (  # noqa: E402
    DEFAULT_BUG_DB_PATH,
    SCHEMA_TABLES,
    BugReportStore,
    BugReportStoreError,
)


def resolve_db_path(explicit: str | None) -> Path:
    """Where the reports live, resolved the way app.py resolves it.

    Deliberately the same order of precedence as the web app: a tool that
    created the file somewhere the app does not look would leave an
    administrator with two databases and a page that still says it is empty.
    """

    return Path(explicit or os.environ.get("GREMLIN_BUGS_DB_PATH") or DEFAULT_BUG_DB_PATH)


def _format_bytes(size: int | None) -> str:
    if not isinstance(size, int):
        return "unknown"
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


def build_report(info: dict, *, created: bool) -> str:
    """The status, as the lines printed to the terminal."""

    lines = ["Bug reports database", f"  Path:     {info['path']}"]

    if not info["exists"]:
        lines.append("  Status:   MISSING -- nothing has been filed and no file has been made")
        if not info["parent_exists"]:
            # The distinction that decides what to do next: a missing folder on
            # a mapped drive is made by running this without --check, while a
            # missing drive is not this tool's to fix.
            lines.append(
                "  Folder:   MISSING too -- check the drive is mapped and reachable, "
                "then run this without --check to create both"
            )
        else:
            lines.append("  Folder:   present -- run this without --check to create the file")
        return "\n".join(lines)

    lines.append("  Status:   created" if created else "  Status:   already present")
    lines.append(f"  Size:     {_format_bytes(info['size_bytes'])}")
    lines.append(f"  Tables:   {', '.join(info['tables']) or 'none'}")

    # A file that opens but is missing a piece is the confusing case: it looks
    # installed and then fails on use. Say which piece, and say that running
    # this fixes it, because ensure_schema only ever adds to what is there.
    gaps = list(info["missing_tables"]) + [
        f"the {name} column" for name in info["missing_columns"]
    ]
    if gaps:
        lines.append(
            f"  Schema:   INCOMPLETE -- missing {', '.join(gaps)}. "
            "Run this without --check to add what is missing; nothing filed is disturbed."
        )
    else:
        lines.append(f"  Schema:   complete ({len(SCHEMA_TABLES)} tables)")

    summary = info["summary"]
    if summary:
        lines.append(
            f"  Reports:  {summary['total']} total "
            f"({summary['open']} open, {summary['resolved']} resolved)"
        )
        if summary["latest_report"]:
            lines.append(f"  Latest:   {summary['latest_report']} UTC")
    return "\n".join(lines)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create the GREMLIN bug reports database (auxillary.db), or report on it."
    )
    parser.add_argument("--db", help="Path to auxillary.db (overrides GREMLIN_BUGS_DB_PATH).")
    parser.add_argument(
        "--check",
        action="store_true",
        help=(
            "Report where things stand and create nothing -- the only mode that "
            "needs no write access to the share. Exits 1 if the database is "
            "missing or incomplete."
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    store = BugReportStore(resolve_db_path(args.db))

    try:
        before = store.describe()
        created = False
        if not args.check:
            # Unconditionally, rather than only when describe() reports a gap:
            # ensure_schema is idempotent, and deciding here which databases
            # need it would mean keeping a second opinion about what a complete
            # schema is, in the one place that only runs at install time.
            #
            # It is also what the app itself runs, rather than a second copy of
            # the CREATE TABLE statements living here. A schema written twice is
            # a schema that drifts.
            store.ensure_schema()
            created = not before["exists"]
        info = store.describe()
    except BugReportStoreError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    print(build_report(info, created=created))

    if not info["exists"] or not info["schema_ready"]:
        return 1
    if created:
        print("\nCreated. The Report a Bug page and the developer dashboard can both use it now.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
