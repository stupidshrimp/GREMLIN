"""The PM calendar: where its database lives, and what happens when it can't be opened.

Every test points GREMLIN_PM_CALENDAR_DB_PATH at a tmp_path, for the reason
test_bug_reports.py gives about its own override: the real default is a Windows
path, and on a POSIX test runner ``Path(r"C:\\GREMLIN")`` is a *relative* path, so
a test that let the default stand would create that name as a folder in the
working tree.
"""

import importlib
import sqlite3

import pytest

from repositories.pm_calendar_repo import (
    DEFAULT_PM_CALENDAR_DB_PATH,
    PM_CALENDAR_DB_FILENAME,
    PmCalendarRepository,
    PmCalendarUnavailableError,
)
from services.pm_calendar_service import PmCalendarService


def _app(monkeypatch, tmp_path, *, pm_db=None):
    monkeypatch.setenv("GREMLIN_ACCESS_DB_PATH", str(tmp_path / "accesscontrol.db"))
    monkeypatch.setenv("GREMLIN_DB_PATH", str(tmp_path / "gremlin.db"))
    monkeypatch.setenv("GREMLIN_BUGS_DB_PATH", str(tmp_path / "auxillary.db"))
    monkeypatch.setenv("GREMLIN_PM_CALENDAR_DB_PATH", str(pm_db or tmp_path / "pm.db"))
    monkeypatch.setenv("GREMLIN_ADMIN_USERNAME", "root")
    monkeypatch.setenv("GREMLIN_ADMIN_PIN", "secret")
    import app

    return importlib.reload(app)


# ----------------------------------------------------------------------
# Where the database lives
# ----------------------------------------------------------------------
def test_the_default_database_is_not_inside_a_user_profile():
    """The default has to be reachable by whatever account GREMLIN runs as.

    It once pointed at one developer's OneDrive folder. Every other machine
    then failed at *startup* -- not on the calendar page -- because building
    the service created the folder, and no other account may create a folder
    under someone else's profile.
    """

    parts = [part.lower() for part in DEFAULT_PM_CALENDAR_DB_PATH.parts]
    assert "users" not in parts
    assert not any("onedrive" in part for part in parts)


def test_the_default_sits_beside_gremlin_db():
    """Compared as Windows paths on purpose.

    Both constants are Windows paths, and on a POSIX runner each is a single
    relative component -- ``Path(r"C:\\GREMLIN\\GREMLIN.db").with_name(...)``
    would drop the folder and the assertion would be about nothing. Reading
    them as PureWindowsPath makes the test say the same thing on either runner.
    """

    from pathlib import PureWindowsPath

    from services.life_data_service import DEFAULT_DB_PATH

    assert PureWindowsPath(DEFAULT_PM_CALENDAR_DB_PATH) == PureWindowsPath(
        DEFAULT_DB_PATH
    ).with_name(PM_CALENDAR_DB_FILENAME)


def test_the_documented_override_is_readable_from_a_dotenv_file():
    """Advertising GREMLIN_PM_CALENDAR_DB_PATH means the .env loader has to accept it."""

    from services.sync_service import APP_ENV_KEYS

    assert "GREMLIN_PM_CALENDAR_DB_PATH" in APP_ENV_KEYS


def test_the_override_decides_which_file_the_app_opens(monkeypatch, tmp_path):
    chosen = tmp_path / "somewhere else" / "pm.db"
    module = _app(monkeypatch, tmp_path, pm_db=chosen)

    assert module.PM_CALENDAR_DB_PATH == chosen
    assert module.pm_calendar_service.repo.db_path == chosen


def test_without_an_override_the_file_lands_beside_the_configured_database(monkeypatch, tmp_path):
    """A deployment that has set GREMLIN_DB_PATH needs to set nothing else."""

    monkeypatch.delenv("GREMLIN_PM_CALENDAR_DB_PATH", raising=False)
    monkeypatch.setenv("GREMLIN_ACCESS_DB_PATH", str(tmp_path / "accesscontrol.db"))
    monkeypatch.setenv("GREMLIN_DB_PATH", str(tmp_path / "gremlin.db"))
    monkeypatch.setenv("GREMLIN_BUGS_DB_PATH", str(tmp_path / "auxillary.db"))
    monkeypatch.setenv("GREMLIN_ADMIN_USERNAME", "root")
    monkeypatch.setenv("GREMLIN_ADMIN_PIN", "secret")
    import app

    module = importlib.reload(app)

    assert module.PM_CALENDAR_DB_PATH == tmp_path / PM_CALENDAR_DB_FILENAME


# ----------------------------------------------------------------------
# An unreachable database costs one page, not the app
# ----------------------------------------------------------------------
def test_building_the_service_touches_no_disk(tmp_path):
    """The constructor runs at import; it must not be what fails."""

    never = tmp_path / "not-created" / "pm.db"
    PmCalendarService(never)

    assert not never.parent.exists()


def test_the_app_starts_even_when_the_database_cannot_be_opened(monkeypatch, tmp_path):
    """The regression this whole change is about: a bad path took down startup."""

    blocked = tmp_path / "blocked"
    blocked.write_text("this is a file, so no folder can be created inside it")

    module = _app(monkeypatch, tmp_path, pm_db=blocked / "sub" / "pm.db")

    # Importing worked at all, and the page itself still renders.
    assert module.app.test_client().get("/pm-calendar").status_code == 200


def test_an_unreachable_database_reports_503_rather_than_500(monkeypatch, tmp_path):
    blocked = tmp_path / "blocked"
    blocked.write_text("not a folder")
    module = _app(monkeypatch, tmp_path, pm_db=blocked / "sub" / "pm.db")
    client = module.app.test_client()

    for url in (
        "/pm-calendar/api/assets",
        "/pm-calendar/api/summary?assets=7",
        "/pm-calendar/api/events?assets=7&start=2026-01-01&end=2026-01-31",
    ):
        response = client.get(url)
        assert response.status_code == 503, url
        # The message has to name the path tried and the way to move it,
        # or an administrator cannot act on it.
        error = response.get_json()["error"]
        assert "GREMLIN_PM_CALENDAR_DB_PATH" in error
        assert str(blocked) in error


def test_the_error_names_the_setting_rather_than_leaking_a_winerror(tmp_path):
    blocked = tmp_path / "blocked"
    blocked.write_text("not a folder")
    repo = PmCalendarRepository(blocked / "sub" / "pm.db")

    with pytest.raises(PmCalendarUnavailableError) as caught:
        repo.ensure_schema()

    assert "could not be opened" in str(caught.value)
    # The cause is kept for the log, not swallowed.
    assert isinstance(caught.value.__cause__, (OSError, sqlite3.Error))


def test_a_corrupt_database_is_reported_rather_than_raised(monkeypatch, tmp_path):
    """sqlite3.connect() is lazy, so this is not caught by guarding the open.

    Nothing reads the file until the first statement, so connect() succeeds
    against a file that is not a database at all and the error arrives from
    ``PRAGMA journal_mode`` inside write_connection(). This is the same handler
    a read-only share reaches, which fails at that PRAGMA or at BEGIN IMMEDIATE
    for the same reason: the open is not the thing that touches the file.
    """

    corrupt = tmp_path / "PM_Calendar_local.db"
    corrupt.write_bytes(b"this is not a sqlite database")
    module = _app(monkeypatch, tmp_path, pm_db=corrupt)
    client = module.app.test_client()

    for url in (
        "/pm-calendar/api/assets",
        "/pm-calendar/api/summary?assets=7",
        "/pm-calendar/api/events?assets=7&start=2026-01-01&end=2026-01-31",
    ):
        response = client.get(url)
        assert response.status_code == 503, url
        assert "GREMLIN_PM_CALENDAR_DB_PATH" in response.get_json()["error"]


def test_a_database_that_breaks_mid_session_is_still_reported(tmp_path):
    """The schema check is cached after it succeeds, so only the read runs.

    A share that drops after the first successful request would otherwise reach
    the endpoint as a raw sqlite3.Error -- past the point any guard on the open
    or on the schema could have translated it.
    """

    db = tmp_path / "pm.db"
    service = PmCalendarService(db)
    assert service.asset_options() == []

    db.write_bytes(b"clobbered while the app was running")

    with pytest.raises(PmCalendarUnavailableError):
        service.events(asset_ids=["7"], start_date="2026-01-01", end_date="2026-12-31")
    with pytest.raises(PmCalendarUnavailableError):
        service.asset_options()


def test_a_sync_reports_a_broken_database_instead_of_dying_in_the_thread(tmp_path):
    """The sync thread's own error handling should show the actionable message."""

    corrupt = tmp_path / "pm.db"
    corrupt.write_bytes(b"not a database")
    service = PmCalendarService(corrupt)

    service._run_sync()  # synchronously, so the test does not race the thread

    status = service.status()
    assert status["state"] == "failed"
    assert "GREMLIN_PM_CALENDAR_DB_PATH" in status["error"]


def test_a_path_that_becomes_reachable_later_is_retried(tmp_path):
    """Only success is cached, so a share that was down at startup recovers."""

    blocked = tmp_path / "target"
    blocked.write_text("not a folder yet")
    service = PmCalendarService(blocked / "pm.db")

    with pytest.raises(PmCalendarUnavailableError):
        service.asset_options()

    blocked.unlink()
    blocked.mkdir()

    assert service.asset_options() == []


# ----------------------------------------------------------------------
# The reads the page depends on still work
# ----------------------------------------------------------------------
def _seeded(tmp_path):
    service = PmCalendarService(tmp_path / "pm.db")
    service._ensure_schema()
    service.repo.upsert_tasks([
        {
            "task_id": "1", "asset_id": "7", "asset_number": "7",
            "asset_name": "Pump", "task_name": "Monthly lube",
            "status_raw": "open", "due_date": "2026-03-10",
            "completed_date": None, "is_completed": 0,
        },
        {
            "task_id": "2", "asset_id": "8", "asset_number": "8",
            "asset_name": "Fan", "task_name": "Belt check",
            "status_raw": "open", "due_date": "2026-03-20",
            "completed_date": "2026-03-19", "is_completed": 1,
        },
    ])
    return service


def test_events_are_filtered_by_asset_and_date_range(tmp_path):
    service = _seeded(tmp_path)

    events = service.events(asset_ids=["7"], start_date="2026-03-01", end_date="2026-03-31")

    assert [event["task_id"] for event in events] == ["1"]


def test_selecting_no_assets_returns_nothing_and_opens_no_database(tmp_path):
    """The page sends `assets=` for an empty selection; it must not mean 'all'."""

    never = tmp_path / "not-created" / "pm.db"
    service = PmCalendarService(never)

    assert service.events(asset_ids=[], start_date="2026-01-01", end_date="2026-12-31") == []
    assert service.summary(asset_ids=[]) == {
        "scheduled": 0, "completed": 0, "overdue": 0, "compliance": 0.0
    }
    assert not never.parent.exists()


def test_asset_options_lists_each_synced_asset_once(tmp_path):
    service = _seeded(tmp_path)

    assert [asset["asset_name"] for asset in service.asset_options()] == ["Fan", "Pump"]
