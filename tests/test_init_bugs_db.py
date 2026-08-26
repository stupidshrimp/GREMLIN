"""The command that creates auxillary.db before anybody needs it.

The app makes the file on first use, so nothing here is what makes the feature
work. What it covers is the promise the command makes to whoever installs
GREMLIN: that --check answers without changing anything, that running it is safe
to repeat, and that it fails loudly -- and nameably -- when the share is not
there, rather than reporting success over a file it could not make.

Every test passes an explicit path. Letting the default stand would create
``Z:\\FACIL\\...`` as a folder in the working tree on a POSIX runner, for the
reason test_bug_reports.py gives at its top.
"""

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
TOOL_PATH = REPO_ROOT / "tools" / "init_bugs_db.py"


def _tool():
    """Import the CLI by path -- tools/ is a folder of scripts, not a package."""

    spec = importlib.util.spec_from_file_location("init_bugs_db", TOOL_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def tool():
    return _tool()


@pytest.fixture()
def db_path(tmp_path):
    """Under a folder of its own, so "the folder is missing too" is reachable."""

    return tmp_path / "GREMLIN Global DB" / "auxillary.db"


# ---------------------------------------------------------------------------
# Where it writes
# ---------------------------------------------------------------------------


def test_the_path_is_resolved_the_way_the_app_resolves_it(tool, monkeypatch, tmp_path):
    """A file created where the app does not look would leave two databases."""

    from services.bug_reports import DEFAULT_BUG_DB_PATH

    monkeypatch.delenv("GREMLIN_BUGS_DB_PATH", raising=False)
    assert tool.resolve_db_path(None) == Path(DEFAULT_BUG_DB_PATH)

    monkeypatch.setenv("GREMLIN_BUGS_DB_PATH", str(tmp_path / "from-env.db"))
    assert tool.resolve_db_path(None) == tmp_path / "from-env.db"

    # --db is the last word, for an administrator creating one somewhere else.
    assert tool.resolve_db_path(str(tmp_path / "explicit.db")) == tmp_path / "explicit.db"


# ---------------------------------------------------------------------------
# --check
# ---------------------------------------------------------------------------


def test_check_reports_a_missing_database_and_creates_nothing(tool, db_path, capsys):
    exit_code = tool.main(["--check", "--db", str(db_path)])

    out = capsys.readouterr().out
    assert exit_code == 1
    assert "MISSING" in out
    assert str(db_path) in out
    assert not db_path.exists()
    assert not db_path.parent.exists()


def test_check_says_so_when_the_folder_is_missing_too(tool, db_path, capsys):
    """The distinction that decides what to do next: make the folder, or map the drive."""

    tool.main(["--check", "--db", str(db_path)])
    assert "MISSING too" in capsys.readouterr().out

    db_path.parent.mkdir(parents=True)
    tool.main(["--check", "--db", str(db_path)])
    out = capsys.readouterr().out
    assert "MISSING too" not in out
    assert "Folder:   present" in out


def test_check_passes_once_the_database_is_there(tool, db_path, capsys):
    assert tool.main(["--db", str(db_path)]) == 0
    capsys.readouterr()

    assert tool.main(["--check", "--db", str(db_path)]) == 0
    assert "already present" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Creating
# ---------------------------------------------------------------------------


def test_it_creates_the_folder_the_file_and_the_schema(tool, db_path, capsys):
    from services.bug_reports import SCHEMA_TABLES

    exit_code = tool.main(["--db", str(db_path)])

    out = capsys.readouterr().out
    assert exit_code == 0
    assert db_path.is_file()
    assert "created" in out
    for table in SCHEMA_TABLES:
        assert table in out


def test_a_database_it_created_takes_reports_immediately(tool, db_path, capsys):
    """The point of running it at install time, stated as a test."""

    from services.bug_reports import BugReportStore

    tool.main(["--db", str(db_path)])
    capsys.readouterr()

    assert BugReportStore(db_path).submit(title="First", description="Something broke.") == 1


def test_running_it_again_reports_rather_than_repeats(tool, db_path, capsys):
    """Safe to leave in an install script that is run more than once."""

    from services.bug_reports import BugReportStore

    tool.main(["--db", str(db_path)])
    BugReportStore(db_path).submit(title="Filed already", description="Detail.")
    capsys.readouterr()

    assert tool.main(["--db", str(db_path)]) == 0
    out = capsys.readouterr().out
    assert "already present" in out
    assert "created" not in out
    # Nothing filed was disturbed by the second run.
    assert "1 total (1 open, 0 resolved)" in out


def test_it_adds_what_an_incomplete_database_is_missing(tool, db_path, capsys):
    """A copy taken before the limiter's table existed is completed, not rebuilt."""

    import sqlite3

    from services.bug_reports import BugReportStore

    tool.main(["--db", str(db_path)])
    BugReportStore(db_path).submit(title="Filed earlier", description="Detail.")
    with sqlite3.connect(db_path) as conn:
        conn.execute("DROP TABLE submission_attempts")
    capsys.readouterr()

    assert tool.main(["--check", "--db", str(db_path)]) == 1
    assert "INCOMPLETE" in capsys.readouterr().out

    assert tool.main(["--db", str(db_path)]) == 0
    out = capsys.readouterr().out
    assert "Schema:   complete" in out
    assert "1 total" in out


def test_it_adds_a_column_an_older_database_is_missing(tool, db_path, capsys):
    """Both tables present is not enough to call an install run done."""

    import sqlite3

    from services.bug_reports import BugReportStore

    tool.main(["--db", str(db_path)])
    BugReportStore(db_path).submit(title="Filed earlier", description="Detail.")
    with sqlite3.connect(db_path) as conn:
        conn.execute("ALTER TABLE bug_reports DROP COLUMN revision")
    capsys.readouterr()

    assert tool.main(["--check", "--db", str(db_path)]) == 1
    assert "missing column:  bug_reports.revision" in capsys.readouterr().out

    assert tool.main(["--db", str(db_path)]) == 0
    assert "Schema:   complete" in capsys.readouterr().out


def test_a_damaged_table_is_called_damaged_rather_than_complete(tool, db_path, capsys):
    """The case the command exists to catch, and the one it must not get wrong.

    A table that has lost a column opens, lists and counts like a healthy one,
    so a check that stopped at the table names would print "complete" over a
    database that refuses the next report filed against it.
    """

    import sqlite3

    tool.main(["--db", str(db_path)])
    with sqlite3.connect(db_path) as conn:
        conn.execute("ALTER TABLE bug_reports DROP COLUMN title")
    capsys.readouterr()

    assert tool.main(["--check", "--db", str(db_path)]) == 1
    out = capsys.readouterr().out
    assert "INCOMPLETE" in out
    assert "bug_reports.title" in out
    assert "complete (" not in out


def test_what_the_schema_cannot_mend_is_not_promised_as_mendable(tool, db_path, capsys):
    """CREATE TABLE IF NOT EXISTS leaves a broken table alone, however broken.

    Telling an administrator to run the command again would send them round the
    same loop and leave them no better off, so once the schema has been applied
    and something is still missing, say what that actually means.
    """

    import sqlite3

    tool.main(["--db", str(db_path)])
    with sqlite3.connect(db_path) as conn:
        conn.execute("ALTER TABLE bug_reports DROP COLUMN title")
    capsys.readouterr()

    assert tool.main(["--db", str(db_path)]) == 1
    out = capsys.readouterr().out
    assert "DAMAGED" in out
    assert "Run this without --check" not in out
    assert "Move it aside" in out


def test_a_schema_that_will_not_apply_is_reported_rather_than_blamed_on_the_drive(
    tool, db_path, capsys
):
    """The path an administrator reaches by doing what --check just told them to.

    A column can be dropped once the index over it is gone, and then the schema
    cannot be reapplied -- recreating that index needs the column. The store can
    only say the file could not be opened, which sends them to check a drive
    that is working, about a file sitting right there. What has to come out is
    what is actually wrong with it.
    """

    import sqlite3

    tool.main(["--db", str(db_path)])
    with sqlite3.connect(db_path) as conn:
        conn.execute("DROP INDEX idx_bug_reports_status_created")
        conn.execute("ALTER TABLE bug_reports DROP COLUMN status")
    capsys.readouterr()

    assert tool.main(["--db", str(db_path)]) == 1
    captured = capsys.readouterr()
    assert "DAMAGED" in captured.out
    assert "no such column: status" in captured.out
    assert "Move it aside" in captured.out
    # Not the store's message about the drive, which is what used to come out.
    assert "drive is mapped" not in captured.out + captured.err


def test_a_column_that_kept_its_name_and_lost_its_shape_is_caught(tool, db_path, capsys):
    """Names alone are not a schema, and this one costs reports.

    A table rebuilt by hand can keep every column name and lose
    `id INTEGER PRIMARY KEY`. Reports then file, return an id, and store NULL --
    so nothing can find them again to resolve or delete.
    """

    import sqlite3

    from services.bug_reports import BugReportStore

    tool.main(["--db", str(db_path)])
    with sqlite3.connect(db_path) as conn:
        columns = [(r[1], r[2]) for r in conn.execute("PRAGMA table_info(bug_reports)")]
        conn.execute("DROP TABLE bug_reports")
        body = ", ".join(f"{name} {kind}" for name, kind in columns)
        conn.execute(f"CREATE TABLE bug_reports ({body})")
    capsys.readouterr()

    assert tool.main(["--check", "--db", str(db_path)]) == 1
    out = capsys.readouterr().out
    assert "complete (" not in out
    assert "bug_reports.id (expected INTEGER PRIMARY KEY AUTOINCREMENT, found INTEGER)" in out

    # And the failure it stands for is real: the report cannot be found again.
    store = BugReportStore(db_path)
    report_id = store.submit(title="Charts blank", description="No bars.")
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT id FROM bug_reports").fetchone()[0] is None
    with pytest.raises(Exception, match=f"No bug report has id {report_id}"):
        store.set_status(report_id, "resolved", actor="root")


def test_a_wall_of_faults_is_bounded_and_leads_with_the_worst(tool, db_path, capsys):
    """A table rebuilt by hand is wrong in every column, and unreadable if listed."""

    import sqlite3

    tool.main(["--db", str(db_path)])
    with sqlite3.connect(db_path) as conn:
        columns = [(r[1], r[2]) for r in conn.execute("PRAGMA table_info(bug_reports)")]
        conn.execute("DROP TABLE bug_reports")
        body = ", ".join(f"{name} {kind}" for name, kind in columns)
        conn.execute(f"CREATE TABLE bug_reports ({body})")
    capsys.readouterr()

    tool.main(["--check", "--db", str(db_path)])
    out = capsys.readouterr().out

    listed = [line for line in out.splitlines() if line.strip().startswith("- ")]
    assert len(listed) == tool.MAX_GAPS_LISTED
    assert "... and 6 more" in out
    # Declaration order, so the cap trims the far end and id survives it.
    assert "bug_reports.id" in listed[0]


def test_a_table_that_lost_only_autoincrement_is_caught(tool, db_path, capsys):
    """Identical in table_info, and it silently misfiles resolutions."""

    import sqlite3

    tool.main(["--db", str(db_path)])
    with sqlite3.connect(db_path) as conn:
        declaration = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'bug_reports'"
        ).fetchone()[0]
        conn.execute("DROP TABLE bug_reports")
        conn.execute(declaration.replace(" AUTOINCREMENT", ""))
    capsys.readouterr()

    assert tool.main(["--check", "--db", str(db_path)]) == 1
    out = capsys.readouterr().out
    assert "complete (" not in out
    assert "expected INTEGER PRIMARY KEY AUTOINCREMENT, found INTEGER PRIMARY KEY" in out


def test_a_path_it_may_not_look_at_is_explained_not_traced(tool, db_path, capsys, monkeypatch):
    """The actionable message, not a traceback, for a share it cannot traverse."""

    def denied(self):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(type(db_path), "is_file", denied)

    assert tool.main(["--check", "--db", str(db_path)]) == 1
    captured = capsys.readouterr()
    assert str(db_path) in captured.err
    assert "drive is mapped and reachable" in captured.err
    assert "Traceback" not in captured.err


# ---------------------------------------------------------------------------
# When the share is not there
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("argv", [["--db"], ["--check", "--db"]])
def test_an_unreachable_share_names_the_path_and_fails(tool, capsys, argv):
    """Reporting success over a file it could not make is the one unacceptable outcome."""

    exit_code = tool.main([*argv, "/proc/nowhere/auxillary.db"])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "/proc/nowhere/auxillary.db" in captured.err + captured.out


def test_a_file_that_is_not_a_database_is_refused(tool, tmp_path, capsys):
    path = tmp_path / "auxillary.db"
    path.write_bytes(b"this is not a database")

    assert tool.main(["--db", str(path)]) == 1
    assert str(path) in capsys.readouterr().err


# ---------------------------------------------------------------------------
# As it is actually run
# ---------------------------------------------------------------------------


def test_it_runs_as_the_documented_command_line(db_path):
    """The docstring says ``python tools/init_bugs_db.py``; this is that, run.

    Covers the sys.path bootstrap at the top of the file, which the in-process
    tests cannot: they import services themselves.
    """

    result = subprocess.run(
        [sys.executable, str(TOOL_PATH), "--db", str(db_path)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "created" in result.stdout
    assert db_path.is_file()
