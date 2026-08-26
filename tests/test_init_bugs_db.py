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
    assert "missing the revision column" in capsys.readouterr().out

    assert tool.main(["--db", str(db_path)]) == 0
    assert "Schema:   complete" in capsys.readouterr().out


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
