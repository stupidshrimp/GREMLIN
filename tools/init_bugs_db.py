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
missing under --check, damaged, unreachable, or when the configured path cannot
be established at all. --db needs no .env read and no imports beyond the store
itself, so it still works where nothing else here does.
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


class ConfigurationError(RuntimeError):
    """The command could not work out which database it was being asked about.

    Kept apart from BugReportStoreError, which means a database that could not
    be reached. This one means not knowing which database to reach for -- and
    conflating them would have the command blame the share for a .env it could
    not read. Both end the same way: a message, and a non-zero exit.
    """


def resolve_db_path(explicit: str | None) -> Path:
    """Where the reports live, resolved the way app.py resolves it.

    Deliberately the same precedence as the web app, and through the same call
    rather than a second reading of it. A tool that created the file somewhere
    the app does not look would leave an administrator with two databases and a
    page that still says it is empty -- and, falling back to the default, a
    stray database on the share nobody put there.

    The ``.env`` load is the part that is easy to miss. app.py applies it before
    reading the variable, so a deployment that configures the override in a file
    rather than exporting it is configured as far as the app is concerned, and a
    tool reading os.environ alone would quietly resolve to the shared-drive
    default instead -- then report success over the wrong database. Same
    function and same keys as app.py, so the two cannot come to disagree.

    Skipped entirely when --db is given, which outranks both and needs neither a
    file read nor an import to say so -- so the command still names a database
    on a machine where nothing else here would run.
    """

    if explicit:
        return Path(explicit)

    # Imported here rather than at the top because sync_service reaches the
    # Limble client and its HTTP dependencies, while the store this command is
    # about is pure standard library. A command for setting a database up should
    # not refuse to start because the API client's dependencies are absent --
    # and if they are, it says so and stops rather than guessing a path.
    try:
        from services.sync_service import APP_ENV_KEYS, load_dotenv_files
    except ImportError as exc:
        raise ConfigurationError(
            f"The .env override cannot be read here: {exc}. Install GREMLIN's "
            "dependencies (pip install -r requirements.txt), or pass --db to "
            "name the database directly, which needs nothing installed. "
            "Carrying on would risk setting up a database the app never opens."
        ) from exc

    try:
        # An exported variable still beats the file, which is load_dotenv_files'
        # own rule and therefore the app's.
        load_dotenv_files(only_keys=APP_ENV_KEYS)
    except OSError as exc:
        # A .env holds credentials, so being readable only by the account that
        # owns it is the sensible state, not a broken one -- and it is exactly
        # the file that may name the database. Refusing beats falling back:
        # the default path is the one place a stray database does real harm.
        raise ConfigurationError(
            f"A .env file is present but could not be read: {exc}. It may carry "
            "the GREMLIN_BUGS_DB_PATH override, so carrying on would risk "
            "checking a different database from the one the app opens. Fix its "
            "permissions, or pass --db to name the database directly."
        ) from exc

    return Path(os.environ.get("GREMLIN_BUGS_DB_PATH") or DEFAULT_BUG_DB_PATH)


def _format_bytes(size: int | None) -> str:
    if not isinstance(size, int):
        return "unknown"
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


# How many individual faults are spelled out before the rest are counted. A
# table rebuilt by hand is wrong in every column at once, and fourteen of those
# run together is a wall nobody reads -- which buries the one that matters.
# They are listed in the order the schema declares them, so the cap trims the
# far end rather than the id column at the front.
MAX_GAPS_LISTED = 8


def _count_gaps(info: dict) -> str:
    """The faults as a count, for the one line that has to summarise them."""

    counts = []
    for label, items in (
        ("table", info["missing_tables"]),
        ("column", info["missing_columns"]),
    ):
        if items:
            counts.append(f"{len(items)} {label}{'s' if len(items) != 1 else ''} missing")
    altered = info["altered_columns"]
    if altered:
        counts.append(f"{len(altered)} column{'s' if len(altered) != 1 else ''} altered")
    blocking = info["blocking_columns"]
    if blocking:
        counts.append(
            f"{len(blocking)} added column{'s' if len(blocking) != 1 else ''} "
            "that no report can satisfy"
        )
    lost = info["missing_constraints"]
    if lost:
        counts.append(f"{len(lost)} constraint{'s' if len(lost) != 1 else ''} lost")
    return ", ".join(counts)


def _gaps(info: dict) -> list[str]:
    """Each fault on its own line, bounded, or nothing when the schema is whole."""

    gaps = (
        [f"missing table:   {name}" for name in info["missing_tables"]]
        + [f"missing column:  {name}" for name in info["missing_columns"]]
        + [f"altered column:  {change}" for change in info["altered_columns"]]
        + [f"blocking column: {name}" for name in info["blocking_columns"]]
        + [f"lost constraint: {name}" for name in info["missing_constraints"]]
    )
    lines = [f"            - {gap}" for gap in gaps[:MAX_GAPS_LISTED]]
    if len(gaps) > MAX_GAPS_LISTED:
        lines.append(f"            ... and {len(gaps) - MAX_GAPS_LISTED} more")
    return lines


def build_report(
    info: dict, *, created: bool, schema_applied: bool, schema_error: str | None = None
) -> str:
    """The status, as the lines printed to the terminal."""

    lines = ["Bug reports database", f"  Path:     {info['path']}"]

    if not info["exists"]:
        lines.append("  Status:   MISSING -- nothing has been filed and no file has been made")
        if schema_error is not None:
            # The command has just tried to make it and could not -- an install
            # -time folder that is present but not writable is the ordinary way
            # here. Falling through to the advice below would name the very
            # command that just failed and bury the only line explaining why.
            lines.append(f"  Reason:   {schema_error}")
            lines.append(
                "            The database could not be created here. Put right what that "
                "names, or pass --db to make it somewhere this account can write."
            )
        elif not info["parent_exists"]:
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
    # installed, passes every glance, and then refuses the first report filed
    # against it -- with a message about the drive, because that is what a
    # failed write looks like from the outside. So say which piece is missing,
    # and say honestly what will and will not mend it.
    gaps = _gaps(info)
    damaged = (
        "This file cannot serve reports. Move it aside and run this again to make "
        "a fresh one; keep it if the reports in it still matter."
    )
    if schema_error is not None:
        # The schema would not go on. This is the case worth getting right,
        # because it is reached by an administrator doing what --check just told
        # them to do: exiting on the store's own message here would tell them to
        # check the drive is mapped, about a file sitting right there and
        # readable. Say what is actually wrong with it instead.
        lines.append("  Schema:   DAMAGED -- the schema could not be applied.")
        lines.append(f"  Reason:   {schema_error}")
        lines.append(f"            {damaged}")
    elif gaps and not schema_applied:
        lines.append(f"  Schema:   INCOMPLETE -- {_count_gaps(info)}.")
        # Deliberately not "run this to fix it": whether these are things the
        # schema adds is ensure_schema's to answer, and predicting it here would
        # mean keeping a second opinion about what it repairs. Under-promising
        # costs an administrator one command; over-promising sent them round a
        # loop.
        lines.append(
            "            Run this without --check to apply the schema; whatever it "
            "cannot add is reported then."
        )
    elif gaps:
        # The schema has just been applied and these are still there, so they are
        # not things it adds: CREATE TABLE IF NOT EXISTS leaves an existing table
        # alone however little of it is left, and nothing here rewrites a column
        # that is the wrong shape. Promising another run would fix it would send
        # an administrator round the same loop.
        lines.append(
            f"  Schema:   DAMAGED -- {_count_gaps(info)}, after applying the schema. "
            f"{damaged}"
        )
    else:
        lines.append(f"  Schema:   complete ({len(SCHEMA_TABLES)} tables)")
    lines += gaps

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
    try:
        store = BugReportStore(resolve_db_path(args.db))
    except ConfigurationError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    try:
        before = store.describe()
    except BugReportStoreError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    created = False
    schema_error: str | None = None
    if not args.check:
        try:
            # Unconditionally, rather than only when describe() reports a gap:
            # ensure_schema is idempotent, and deciding here which databases
            # need it would mean keeping a second opinion about what a complete
            # schema is, in the one place that only runs at install time.
            #
            # It is also what the app itself runs, rather than a second copy of
            # the CREATE TABLE statements living here. A schema written twice is
            # a schema that drifts.
            store.ensure_schema()
        except BugReportStoreError as exc:
            # Not fatal, and not reported in the store's words. A database
            # damaged enough that the schema will not go on is exactly the one
            # whose administrator was just told by --check to run this, and the
            # store can only say the file could not be opened -- which sends
            # them to the drive, about a file sitting right there. Carry the
            # sqlite cause into the report below, which can say what it means.
            schema_error = str(exc.__cause__ or exc)
        else:
            created = not before["exists"]

    try:
        info = store.describe()
    except BugReportStoreError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    print(
        build_report(
            info,
            created=created,
            schema_applied=not args.check,
            schema_error=schema_error,
        )
    )

    if not info["exists"] or not info["schema_ready"] or schema_error is not None:
        return 1
    if created:
        print("\nCreated. The Report a Bug page and the developer dashboard can both use it now.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
