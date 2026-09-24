"""The PM calendar: where its database lives, and what happens when it can't be opened.

Every test points GREMLIN_PM_CALENDAR_DB_PATH at a tmp_path, for the reason
test_bug_reports.py gives about its own override: the real default is a Windows
path, and on a POSIX test runner ``Path(r"C:\\GREMLIN")`` is a *relative* path, so
a test that let the default stand would create that name as a folder in the
working tree.
"""

import importlib
import sqlite3
from datetime import date

import pytest

import services.pm_calendar_service as pm_calendar_service_module
from repositories.pm_calendar_repo import (
    DEFAULT_PM_CALENDAR_DB_PATH,
    PM_CALENDAR_DB_FILENAME,
    PmCalendarRepository,
    PmCalendarUnavailableError,
)
from services.pm_calendar_service import PmCalendarService


@pytest.fixture(autouse=True)
def _fixed_today(monkeypatch):
    """Pin "today" for every test here.

    Projection stops for a PM line that has gone quiet for a few cycles,
    measured from today -- so without this, the fixtures below (written
    around September 2026) would quietly turn stale as the calendar moves on.
    """

    monkeypatch.setattr(pm_calendar_service_module, "_today", lambda: date(2026, 9, 21))


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


def test_a_connection_that_fails_to_initialise_is_closed(tmp_path, monkeypatch):
    """connect() must not leak the handle when a step after the open fails.

    Forced rather than found: the two steps between opening and returning are a
    row_factory assignment and PRAGMA busy_timeout, which is a connection-level
    setting that never reads the file -- so no ordinary bad database reaches
    this path. It is guarded anyway because the failure it would cause is
    invisible: the retry this error path invites would leak one handle per
    attempt, with nothing in the message to say so.
    """

    import repositories.pm_calendar_repo as repo_module

    closes: list[str] = []
    real_connect = sqlite3.connect

    class _Spy(sqlite3.Connection):
        def execute(self, *args, **kwargs):  # noqa: D102 - the failure under test
            raise sqlite3.OperationalError("disk I/O error")

        def close(self):
            closes.append("closed")
            super().close()

    monkeypatch.setattr(
        repo_module.sqlite3,
        "connect",
        lambda path, **kwargs: real_connect(path, factory=_Spy, **kwargs),
    )

    with pytest.raises(PmCalendarUnavailableError):
        PmCalendarRepository(tmp_path / "pm.db").connect()

    assert closes == ["closed"]


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


# ----------------------------------------------------------------------
# Projecting future PMs beyond whatever Limble has already generated
# ----------------------------------------------------------------------
def _seeded_monthly_series(tmp_path, *, open_due_date):
    """Three completed monthly occurrences, plus a fourth still-open one.

    ``open_due_date`` is the lever the tests below pull: it is the one
    field a scheduler is free to drag around in Limble while a PM is still
    outstanding, and the whole point of anchoring on completed_date is that
    dragging it should not touch what gets projected past it.
    """

    service = PmCalendarService(tmp_path / "pm.db")
    service._ensure_schema()
    service.repo.upsert_tasks([
        {
            "task_id": "1", "asset_id": "3103", "asset_number": "3103",
            "asset_name": "Salvagnini Laser", "task_name": "3103 - M - Salvagnini Laser",
            "status_raw": "done", "due_date": "2026-06-05",
            "completed_date": "2026-06-04", "is_completed": 1,
        },
        {
            "task_id": "2", "asset_id": "3103", "asset_number": "3103",
            "asset_name": "Salvagnini Laser", "task_name": "3103 - M - Salvagnini Laser",
            "status_raw": "done", "due_date": "2026-07-03",
            "completed_date": "2026-07-02", "is_completed": 1,
        },
        {
            "task_id": "3", "asset_id": "3103", "asset_number": "3103",
            "asset_name": "Salvagnini Laser", "task_name": "3103 - M - Salvagnini Laser",
            "status_raw": "done", "due_date": "2026-07-31",
            "completed_date": "2026-07-30", "is_completed": 1,
        },
        {
            "task_id": "4", "asset_id": "3103", "asset_number": "3103",
            "asset_name": "Salvagnini Laser", "task_name": "3103 - M - Salvagnini Laser",
            "status_raw": "open", "due_date": open_due_date,
            "completed_date": None, "is_completed": 0,
        },
    ])
    return service


def test_future_months_are_filled_with_projected_pms(tmp_path):
    """A month past whatever Limble has generated still shows something.

    Without projection this window is empty: no real row's due_date falls
    in it. Every 28 days (M = 4 weeks) from the last completion, up to
    _OPEN_LINE_PROJECTION_LIMIT cycles since this line still has an open
    work order -- the fourth cycle (2026-11-19) is past that cap, so it
    isn't among these even though the window runs through November.
    """

    service = _seeded_monthly_series(tmp_path, open_due_date="2026-08-28")

    events = service.events(asset_ids=["3103"], start_date="2026-09-01", end_date="2026-11-30")

    assert [e["due_date"] for e in events] == ["2026-09-24", "2026-10-22"]
    assert all(e["is_projected"] for e in events)


def test_dragging_the_open_tasks_due_date_does_not_move_the_projected_series(tmp_path):
    """The exact scenario the feature exists to survive.

    Two services, identical completed history, differing only in where the
    still-open next occurrence's due date happens to sit right now -- one
    left where the cadence would naturally put it, the other dragged five
    months out, standing in for a reschedule. Projected pills for a window
    neither due date touches must come out identical either way: the
    series is built from completed_date alone and never reads the open
    row's due_date at all.
    """

    on_schedule = _seeded_monthly_series(tmp_path / "a", open_due_date="2026-08-27")
    dragged = _seeded_monthly_series(tmp_path / "b", open_due_date="2027-03-01")

    window = dict(asset_ids=["3103"], start_date="2026-09-01", end_date="2026-12-31")
    on_schedule_dates = [e["due_date"] for e in on_schedule.events(**window)]
    dragged_dates = [e["due_date"] for e in dragged.events(**window)]

    assert on_schedule_dates == dragged_dates == ["2026-09-24", "2026-10-22"]


def test_a_projected_pill_yields_to_a_real_row_already_covering_its_slot(tmp_path):
    """The one place a real due date *is* allowed to matter: its own slot.

    The open row's due date (2026-10-25) sits close enough to where the
    unbroken cadence would have projected its third cycle (2026-10-22) that
    showing both would just be the same PM twice. Only that one slot
    yields -- the cycle before it keeps projecting on schedule. (The window
    stays within _OPEN_LINE_PROJECTION_LIMIT cycles of the anchor; further
    out, this line's open work order caps projection off entirely -- see
    test_projection_for_an_open_line_never_exceeds_the_cap.)
    """

    service = _seeded_monthly_series(tmp_path, open_due_date="2026-10-25")

    events = service.events(asset_ids=["3103"], start_date="2026-09-01", end_date="2026-11-15")

    by_date = {e["due_date"]: e for e in events}
    # Real rows don't carry an is_projected key at all -- only synthetic
    # ones do -- so "not set" is what a real row winning looks like here.
    assert not by_date["2026-10-25"].get("is_projected")
    assert "2026-10-22" not in by_date  # the projected slot it absorbed
    assert by_date["2026-09-24"]["is_projected"] is True  # neighbour unaffected


@pytest.mark.parametrize(
    "code, weeks",
    [("2W", 2), ("M", 4), ("Q", 12), ("SA", 26), ("A", 52), ("3Y", 156)],
)
def test_each_code_projects_at_its_fixed_interval(tmp_path, code, weeks):
    """One fixed interval per code -- the table, not the template's own setting.

    SA is 26 weeks here even though some SA templates in Limble are set to 24:
    the calendar projects the standard cadence a code stands for.
    """

    service = PmCalendarService(tmp_path / "pm.db")
    service._ensure_schema()
    service.repo.upsert_tasks([{
        "task_id": "1", "asset_id": "1435", "asset_number": "1435",
        "asset_name": "Stokes Tablet Machine", "task_name": f"1435 - {code} - Stokes Tablet",
        "status_raw": "done", "due_date": "2026-09-18",
        "completed_date": "2026-09-18", "is_completed": 1,
    }])

    events = service.events(asset_ids=["1435"], start_date="2026-09-19", end_date="2030-12-31")

    first = date.fromisoformat(events[0]["due_date"])
    assert (first - date(2026, 9, 18)).days == weeks * 7


def test_two_pm_lines_with_the_same_code_on_one_asset_are_projected_separately(tmp_path):
    """A series is one PM line, not one cadence code.

    An asset can carry two monthly PMs (a laser and its chiller, say). Keyed
    on (asset, code) the two histories were pooled: one anchor, one name, and
    the other line vanished from every future month while the survivor wore
    whichever name happened to come first.
    """

    service = PmCalendarService(tmp_path / "pm.db")
    service._ensure_schema()
    service.repo.upsert_tasks([
        {
            "task_id": "1", "asset_id": "3103", "asset_number": "3103",
            "asset_name": "Salvagnini Laser", "task_name": "3103 - M - Laser Optics",
            "status_raw": "done", "due_date": "2026-07-03",
            "completed_date": "2026-07-03", "is_completed": 1,
        },
        {
            "task_id": "2", "asset_id": "3103", "asset_number": "3103",
            "asset_name": "Salvagnini Laser", "task_name": "3103 - M - Chiller",
            "status_raw": "done", "due_date": "2026-07-17",
            "completed_date": "2026-07-17", "is_completed": 1,
        },
    ])

    events = service.events(asset_ids=["3103"], start_date="2026-09-01", end_date="2026-10-09")

    by_name = {e["task_name"]: e["due_date"] for e in events}
    assert by_name == {
        "3103 - M - Laser Optics": "2026-09-25",  # 2026-07-03 + 12 weeks
        "3103 - M - Chiller": "2026-10-09",  # 2026-07-17 + 12 weeks
    }
    assert len({e["task_id"] for e in events}) == len(events)


def test_a_pm_lines_name_is_matched_ignoring_case_and_spacing(tmp_path):
    """Stray capitals or double spaces in Limble don't split one line in two."""

    service = PmCalendarService(tmp_path / "pm.db")
    service._ensure_schema()
    service.repo.upsert_tasks([
        {
            "task_id": "1", "asset_id": "3103", "asset_number": "3103",
            "asset_name": "Salvagnini Laser", "task_name": "3103 - M - Salvagnini Laser",
            "status_raw": "done", "due_date": "2026-07-03",
            "completed_date": "2026-07-03", "is_completed": 1,
        },
        {
            "task_id": "2", "asset_id": "3103", "asset_number": "3103",
            "asset_name": "Salvagnini Laser", "task_name": "3103 - M -  salvagnini laser",
            "status_raw": "done", "due_date": "2026-07-31",
            "completed_date": "2026-07-31", "is_completed": 1,
        },
    ])

    events = service.events(asset_ids=["3103"], start_date="2026-10-01", end_date="2026-10-31")

    assert [e["due_date"] for e in events] == ["2026-10-23"]


def test_events_without_an_asset_filter_do_not_read_the_whole_history(tmp_path, monkeypatch):
    """No ?assets= means every asset, and projection would need every row.

    The endpoint bounds the real rows by start/end, but the history read that
    projection needs has no date bound at all -- for "every asset" that is
    the whole table. The page always sends its selection, so an unfiltered
    request gets real rows only.
    """

    service = _seeded_monthly_series(tmp_path, open_due_date="2026-08-28")
    calls = []
    real_fetch = service.repo.fetch_tasks

    def spy(**kwargs):
        calls.append(kwargs)
        return real_fetch(**kwargs)

    monkeypatch.setattr(service.repo, "fetch_tasks", spy)

    events = service.events(asset_ids=None, start_date="2026-09-01", end_date="2026-11-30")

    assert events == []  # no real rows in the window, and no projections
    assert all(call.get("due_since") and call.get("due_until") for call in calls)


def _single_line(tmp_path, *, name, due, completed):
    service = PmCalendarService(tmp_path / "pm.db")
    service._ensure_schema()
    service.repo.upsert_tasks([{
        "task_id": "1", "asset_id": "777", "asset_number": "777",
        "asset_name": "Retired Press", "task_name": name,
        "status_raw": "done" if completed else "open", "due_date": due,
        "completed_date": completed, "is_completed": 1 if completed else 0,
    }])
    return service


def test_a_line_that_stopped_recurring_years_ago_is_not_projected(tmp_path):
    """The review's repro: an annual last completed in 2019.

    Without a staleness bound this drew pills in 2027 and 2028, eight years
    past the last real occurrence -- a scrapped asset, a deleted template or
    a renamed line all look like this from here.
    """

    service = _single_line(
        tmp_path, name="777 - A - Annual inspection",
        due="2019-05-01", completed="2019-05-01",
    )

    events = service.events(asset_ids=["777"], start_date="2027-01-01", end_date="2028-12-31")

    assert events == []


def test_a_line_is_still_projected_within_three_of_its_own_intervals(tmp_path):
    """Quiet for under three cycles is late, not retired."""

    # Last seen 2026-07-03; three monthly cycles later is 2026-09-25, and
    # today is pinned to 2026-09-21 -- just inside.
    service = _single_line(
        tmp_path, name="777 - M - Press lube",
        due="2026-07-03", completed="2026-07-03",
    )

    events = service.events(asset_ids=["777"], start_date="2026-10-01", end_date="2026-10-31")

    assert [e["due_date"] for e in events] == ["2026-10-23"]


def test_a_line_quiet_for_more_than_three_intervals_is_not_projected(tmp_path):
    """Three missed monthlies: treated as no longer running."""

    service = _single_line(
        tmp_path, name="777 - M - Press lube",
        due="2026-06-05", completed="2026-06-05",
    )

    events = service.events(asset_ids=["777"], start_date="2026-10-01", end_date="2026-12-31")

    assert events == []


def test_an_open_work_order_still_lets_a_line_project_up_to_the_cap(tmp_path):
    """A late but not-ancient open work order: projection still runs, capped.

    Monthly, last completed 2026-08-01; the open work order (due 2026-08-15)
    is over a month overdue by the pinned "today" of 2026-09-21. That's
    still within _OPEN_LINE_PROJECTION_LIMIT cycles of the anchor, so the
    remaining cycles inside the cap show up -- the first cycle (2026-08-29)
    doesn't, simply because it's already in the past.
    """

    service = PmCalendarService(tmp_path / "pm.db")
    service._ensure_schema()
    service.repo.upsert_tasks([
        {
            "task_id": "1", "asset_id": "777", "asset_number": "777",
            "asset_name": "Press", "task_name": "777 - M - Press lube",
            "status_raw": "done", "due_date": "2026-08-01",
            "completed_date": "2026-08-01", "is_completed": 1,
        },
        {
            "task_id": "2", "asset_id": "777", "asset_number": "777",
            "asset_name": "Press", "task_name": "777 - M - Press lube",
            "status_raw": "open", "due_date": "2026-08-15",
            "completed_date": None, "is_completed": 0,
        },
    ])

    events = service.events(asset_ids=["777"], start_date="2026-09-01", end_date="2026-11-30")

    assert [e["due_date"] for e in events if e.get("is_projected")] == [
        "2026-09-26", "2026-10-24",
    ]


def test_projection_for_an_open_line_never_exceeds_the_cap(tmp_path):
    """However far ahead the window looks, an open line only ever shows three.

    Last completed just before "today", with the next work order already
    open -- nothing overdue about this one. Even with a window running
    into 2027, only the first _OPEN_LINE_PROJECTION_LIMIT cycles past the
    anchor ever appear; a fourth cycle that would otherwise fit the window
    (2027-01-10) is deliberately withheld until the open work order closes.
    """

    service = PmCalendarService(tmp_path / "pm.db")
    service._ensure_schema()
    service.repo.upsert_tasks([
        {
            "task_id": "1", "asset_id": "777", "asset_number": "777",
            "asset_name": "Press", "task_name": "777 - M - Press lube",
            "status_raw": "done", "due_date": "2026-09-20",
            "completed_date": "2026-09-20", "is_completed": 1,
        },
        {
            "task_id": "2", "asset_id": "777", "asset_number": "777",
            "asset_name": "Press", "task_name": "777 - M - Press lube",
            "status_raw": "open", "due_date": "2026-10-01",
            "completed_date": None, "is_completed": 0,
        },
    ])

    events = service.events(asset_ids=["777"], start_date="2026-09-01", end_date="2027-12-31")

    projected = [e["due_date"] for e in events if e.get("is_projected")]
    assert projected == ["2026-10-18", "2026-11-15", "2026-12-13"]
    assert "2027-01-10" not in projected


def test_a_very_overdue_open_work_order_produces_no_stale_estimates(tmp_path):
    """So overdue that even the projection cap has already elapsed.

    Quarterly, last completed 260 days before "today" -- well past
    _STALE_AFTER_INTERVALS worth of silence. The open work order keeps this
    line from being dropped outright as retired, but its
    _OPEN_LINE_PROJECTION_LIMIT cycles past that old anchor all land in the
    past too, so nothing is projected until the open work order is
    completed and gives this line a fresh anchor to build from.
    """

    service = PmCalendarService(tmp_path / "pm.db")
    service._ensure_schema()
    service.repo.upsert_tasks([
        {
            "task_id": "1", "asset_id": "777", "asset_number": "777",
            "asset_name": "Press", "task_name": "777 - Q - Press inspection",
            "status_raw": "done", "due_date": "2026-01-04",
            "completed_date": "2026-01-04", "is_completed": 1,
        },
        {
            "task_id": "2", "asset_id": "777", "asset_number": "777",
            "asset_name": "Press", "task_name": "777 - Q - Press inspection",
            "status_raw": "open", "due_date": "2026-01-18",
            "completed_date": None, "is_completed": 0,
        },
    ])

    events = service.events(asset_ids=["777"], start_date="2026-09-01", end_date="2027-12-31")

    assert [e for e in events if e.get("is_projected")] == []


def test_projecting_far_ahead_does_not_walk_every_cycle_from_the_anchor(tmp_path, monkeypatch):
    """A window years out jumps straight there instead of stepping 2W at a time."""

    service = _single_line(
        tmp_path, name="777 - 2W - Filter swap",
        due="2026-09-11", completed="2026-09-11",
    )
    checked = []
    real_near = pm_calendar_service_module._near_any

    def counting(candidate, known, tolerance_days):
        checked.append(candidate)
        return real_near(candidate, known, tolerance_days)

    monkeypatch.setattr(pm_calendar_service_module, "_near_any", counting)

    events = service.events(asset_ids=["777"], start_date="2030-01-01", end_date="2030-01-31")

    assert len(events) == 2
    assert len(checked) == len(events)


def test_a_pm_name_that_does_not_match_the_cadence_convention_is_left_alone(tmp_path):
    """No code to parse means no projection -- not a crash, not a guess."""

    service = PmCalendarService(tmp_path / "pm.db")
    service._ensure_schema()
    service.repo.upsert_tasks([{
        "task_id": "1", "asset_id": "9", "asset_number": "9",
        "asset_name": "Mystery Asset", "task_name": "Replace worn belt",
        "status_raw": "done", "due_date": "2026-06-01",
        "completed_date": "2026-06-01", "is_completed": 1,
    }])

    events = service.events(asset_ids=["9"], start_date="2026-06-01", end_date="2027-06-01")

    assert len(events) == 1
    assert events[0]["task_id"] == "1"


# ----------------------------------------------------------------------
# Parent assets bring their sub-assets with them
# ----------------------------------------------------------------------
def _pm(task_id, asset_id, name, due, *, completed=None):
    return {
        "task_id": task_id, "asset_id": asset_id, "asset_number": asset_id,
        "asset_name": name, "task_name": f"{asset_id} - Q - {name}",
        "status_raw": "done" if completed else "open", "due_date": due,
        "completed_date": completed, "is_completed": 1 if completed else 0,
    }


def _panel_line(tmp_path):
    """4002 with two sub-assets, one of which has a sub-asset of its own.

    4002 has PMs of its own; so do S1 and S1A. S2 has none, and nor does its
    own child -- it's in the tree only because the test gives it one PM.
    Asset 9 is an unrelated machine that must never come along.
    """

    service = PmCalendarService(tmp_path / "pm.db")
    service._ensure_schema()
    service.repo.upsert_tasks([
        _pm("1", "4002", "Panel Finishing System", "2026-10-05"),
        _pm("2", "S1", "Sander", "2026-10-06"),
        _pm("3", "S1A", "Sander Dust Collector", "2026-10-07"),
        _pm("4", "S2", "Oven", "2026-10-08"),
        _pm("5", "9", "Unrelated Press", "2026-10-09"),
    ])
    service.repo.replace_assets([
        {"asset_id": "4002", "asset_name": "Panel Finishing System", "parent_asset_id": None},
        {"asset_id": "S1", "asset_name": "Sander", "parent_asset_id": "4002"},
        {"asset_id": "S1A", "asset_name": "Sander Dust Collector", "parent_asset_id": "S1"},
        {"asset_id": "S2", "asset_name": "Oven", "parent_asset_id": "4002"},
        {"asset_id": "9", "asset_name": "Unrelated Press", "parent_asset_id": None},
    ])
    return service


def _real_asset_ids(events):
    return sorted({e["asset_id"] for e in events if not e.get("is_projected")})


def test_picking_a_parent_shows_its_own_pms_and_every_sub_assets(tmp_path):
    service = _panel_line(tmp_path)

    events = service.events(asset_ids=["4002"], start_date="2026-10-01", end_date="2026-10-31")

    assert _real_asset_ids(events) == ["4002", "S1", "S1A", "S2"]


def test_picking_a_sub_asset_shows_only_that_branch(tmp_path):
    """Expansion only goes down: the parent and siblings stay out."""

    service = _panel_line(tmp_path)

    assert _real_asset_ids(
        service.events(asset_ids=["S1"], start_date="2026-10-01", end_date="2026-10-31")
    ) == ["S1", "S1A"]
    assert _real_asset_ids(
        service.events(asset_ids=["S2"], start_date="2026-10-01", end_date="2026-10-31")
    ) == ["S2"]


def test_picking_a_parent_and_one_of_its_children_counts_nothing_twice(tmp_path):
    service = _panel_line(tmp_path)

    events = service.events(asset_ids=["4002", "S1"], start_date="2026-10-01", end_date="2026-10-31")

    assert len([e for e in events if not e.get("is_projected")]) == 4


def test_the_summary_covers_the_whole_branch_too(tmp_path):
    """The tiles and the grid have to be counting the same PMs.

    The completed PM is dated "today" as summary() sees it (pinned above),
    so it's inside the year to date.
    """

    service = _panel_line(tmp_path)
    today = pm_calendar_service_module._today().isoformat()
    service.repo.upsert_tasks([
        _pm("10", "S1A", "Sander Dust Collector", today, completed=today),
    ])

    assert service.summary(asset_ids=["4002"])["completed"] == 1
    assert service.summary(asset_ids=["S2"])["completed"] == 0


def test_a_loop_in_the_hierarchy_does_not_hang_a_request(tmp_path):
    service = _panel_line(tmp_path)
    service.repo.replace_assets([
        {"asset_id": "4002", "asset_name": "Panel Finishing System", "parent_asset_id": "S1"},
        {"asset_id": "S1", "asset_name": "Sander", "parent_asset_id": "4002"},
    ])

    events = service.events(asset_ids=["4002"], start_date="2026-10-01", end_date="2026-10-31")

    assert _real_asset_ids(events) == ["4002", "S1"]


def test_the_picker_lists_each_parent_before_its_children(tmp_path):
    service = _panel_line(tmp_path)

    options = service.asset_options()

    assert [(o["asset_id"], o["depth"], o["descendant_count"]) for o in options] == [
        ("4002", 0, 3),
        ("S2", 1, 0),  # "Oven" sorts before "Sander"
        ("S1", 1, 1),
        ("S1A", 2, 0),
        ("9", 0, 0),
    ]
    assert {o["asset_id"]: o["parent_asset_id"] for o in options}["S1A"] == "S1"


def test_before_the_first_sync_the_picker_stays_flat(tmp_path):
    """A database synced before this shipped has no hierarchy yet.

    Every asset with PMs is listed on its own, and picking one shows only
    that asset -- exactly the old behaviour, until a sync fills pm_asset.
    """

    service = PmCalendarService(tmp_path / "pm.db")
    service._ensure_schema()
    service.repo.upsert_tasks([
        _pm("1", "4002", "Panel Finishing System", "2026-10-05"),
        _pm("2", "S1", "Sander", "2026-10-06"),
    ])

    assert [(o["asset_id"], o["depth"], o["descendant_count"]) for o in service.asset_options()] == [
        ("4002", 0, 0),
        ("S1", 0, 0),
    ]
    assert _real_asset_ids(
        service.events(asset_ids=["4002"], start_date="2026-10-01", end_date="2026-10-31")
    ) == ["4002"]


def test_an_asset_with_pms_missing_from_the_hierarchy_is_still_listed(tmp_path):
    service = _panel_line(tmp_path)
    service.repo.upsert_tasks([_pm("20", "77", "Brand New Lathe", "2026-10-10")])

    options = {o["asset_id"]: o for o in service.asset_options()}

    assert options["77"]["depth"] == 0
    assert options["77"]["asset_name"] == "Brand New Lathe"


def test_an_existing_database_gains_the_hierarchy_table(tmp_path):
    """No migration step: the new table is created on first use."""

    db = tmp_path / "pm.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE pm_task (task_id TEXT PRIMARY KEY, asset_id TEXT, asset_number TEXT, "
        "asset_name TEXT, task_name TEXT, status_raw TEXT, due_date TEXT, completed_date TEXT, "
        "is_completed INTEGER NOT NULL DEFAULT 0, synced_at TEXT NOT NULL DEFAULT (datetime('now')))"
    )
    conn.execute(
        "INSERT INTO pm_task (task_id, asset_id, asset_number, asset_name) VALUES ('1', '7', '7', 'Pump')"
    )
    conn.commit()
    conn.close()

    service = PmCalendarService(db)

    assert [o["asset_id"] for o in service.asset_options()] == ["7"]
    assert service.repo.fetch_assets() == []


def test_the_hierarchy_keeps_parents_without_pms_and_drops_everything_else():
    """What a sync stores, from Limble's /assets payload.

    4002 has no PMs of its own here, but its sub-asset does, so 4002 is kept
    and pickable. 500 has no PMs anywhere under it and is left out. A parent
    id that isn't in /assets at all leaves its child at the top level.
    """

    assets = [
        {"assetID": 4002, "name": "Panel Finishing System", "parentAssetID": 0},
        {"assetID": 4101, "name": "Sander", "parentAssetID": 4002},
        {"assetID": 500, "name": "Spare Parts Cage", "parentAssetID": 0},
        {"assetID": 501, "name": "Shelf", "parentAssetID": 500},
        {"assetID": 600, "name": "Orphan", "parentAssetID": 99999},
    ]

    rows = PmCalendarService._asset_hierarchy(assets, {"4101", "600"})

    assert sorted((r["asset_id"], r["parent_asset_id"]) for r in rows) == [
        ("4002", None),
        ("4101", "4002"),
        ("600", None),
    ]


def test_a_loop_in_limbles_asset_data_is_cut_when_stored():
    assets = [
        {"assetID": 1, "name": "A", "parentAssetID": 2},
        {"assetID": 2, "name": "B", "parentAssetID": 1},
    ]

    rows = {r["asset_id"]: r["parent_asset_id"] for r in PmCalendarService._asset_hierarchy(assets, {"1"})}

    # One of the two has to become the root; either way, walking up ends.
    assert sorted(rows) == ["1", "2"]
    assert None in rows.values()


def test_a_sync_stores_the_hierarchy(tmp_path, monkeypatch):
    """End to end through _run_sync, with Limble replaced by a stub."""

    class _StubClient:
        def __init__(self, config):
            pass

        def get_tasks(self, on_page=None):
            return [
                {"taskID": 1, "type": 1, "assetID": 4101, "name": "4101 - M - Sander",
                 "dueDate": 1790000000, "dateCompleted": 0},
            ]

        def get_assets(self):
            return [
                {"assetID": 4002, "name": "Panel Finishing System", "parentAssetID": 0},
                {"assetID": 4101, "name": "Sander", "parentAssetID": 4002},
                {"assetID": 500, "name": "Spare Parts Cage", "parentAssetID": 0},
            ]

    monkeypatch.setattr(pm_calendar_service_module, "LimbleClient", _StubClient)
    monkeypatch.setattr(pm_calendar_service_module.LimbleConfig, "from_env", classmethod(lambda cls: None))
    monkeypatch.setattr(pm_calendar_service_module, "load_dotenv_files", lambda **kwargs: None)

    service = PmCalendarService(tmp_path / "pm.db")
    service._run_sync()

    assert service.status()["state"] == "succeeded", service.status()
    assert sorted((r["asset_id"], r["parent_asset_id"]) for r in service.repo.fetch_assets()) == [
        ("4002", None),
        ("4101", "4002"),
    ]
    assert [(o["asset_id"], o["descendant_count"]) for o in service.asset_options()] == [
        ("4002", 1),
        ("4101", 0),
    ]


def test_a_line_grouped_through_parents_without_pms_is_picked_as_a_whole(tmp_path):
    """The Salvagnini shape from Limble: two grouping levels with no PMs.

    3101-3107 Salvagnini has no PMs; nor do its "Forming Side" and "Laser
    Side" groupings. Every PM sits on the numbered machines underneath.
    Picking the top still has to bring all seven in, and picking one side
    only that side's machines.
    """

    assets = [
        {"assetID": 914, "name": "Dept 914 Machinery Maint", "parentAssetID": 0},
        {"assetID": 706, "name": "Building 706", "parentAssetID": 914},
        {"assetID": 9000, "name": "3101-3107 Salvagnini", "parentAssetID": 706},
        {"assetID": 9001, "name": "Salvagnini Forming Side", "parentAssetID": 9000},
        {"assetID": 9002, "name": "Salvagnini Laser Side", "parentAssetID": 9000},
    ]
    assets += [{"assetID": a, "name": str(a), "parentAssetID": 9001} for a in (3102, 3105, 3106, 3107)]
    assets += [{"assetID": a, "name": str(a), "parentAssetID": 9002} for a in (3101, 3103, 3104)]
    machines = ["3101", "3102", "3103", "3104", "3105", "3106", "3107"]

    service = PmCalendarService(tmp_path / "pm.db")
    service._ensure_schema()
    service.repo.upsert_tasks([_pm(a, a, a, "2026-10-05") for a in machines])
    service.repo.replace_assets(PmCalendarService._asset_hierarchy(assets, set(machines)))

    window = dict(start_date="2026-10-01", end_date="2026-10-31")
    assert _real_asset_ids(service.events(asset_ids=["9000"], **window)) == machines
    assert _real_asset_ids(service.events(asset_ids=["9002"], **window)) == ["3101", "3103", "3104"]
    counts = {o["asset_id"]: o["descendant_count"] for o in service.asset_options()}
    assert (counts["9000"], counts["9001"], counts["9002"]) == (9, 4, 3)


# ----------------------------------------------------------------------
# "Last done": the most recently completed PM for a chip
# ----------------------------------------------------------------------
def test_last_done_is_the_most_recently_completed_pm(tmp_path):
    """By completion date, not due date: the one signed off last wins."""

    service = PmCalendarService(tmp_path / "pm.db")
    service._ensure_schema()
    service.repo.upsert_tasks([
        _pm("1", "7", "Pump", "2026-08-01", completed="2026-08-20"),
        _pm("2", "7", "Pump", "2026-08-15", completed="2026-08-16"),
        _pm("3", "7", "Pump", "2026-09-10"),  # still open
    ])

    pm = service.last_completed(["7"])

    assert pm["task_id"] == "1"
    assert pm["due_date"] == "2026-08-01"


def test_last_done_on_a_parent_covers_its_sub_assets(tmp_path):
    service = _panel_line(tmp_path)
    service.repo.upsert_tasks([
        _pm("30", "4002", "Panel Finishing System", "2026-07-01", completed="2026-07-01"),
        _pm("31", "S1A", "Sander Dust Collector", "2026-08-01", completed="2026-08-03"),
        _pm("32", "9", "Unrelated Press", "2026-09-01", completed="2026-09-01"),
    ])

    assert service.last_completed(["4002"])["task_id"] == "31"
    assert service.last_completed(["S2"]) is None


def test_last_done_without_a_completed_pm_is_none(tmp_path):
    service = _seeded(tmp_path)

    assert service.last_completed(["7"]) is None
    assert service.last_completed([]) is None


def test_the_last_done_endpoint(monkeypatch, tmp_path):
    module = _app(monkeypatch, tmp_path)
    module.pm_calendar_service._ensure_schema()
    module.pm_calendar_service.repo.upsert_tasks([
        _pm("1", "7", "Pump", "2026-08-01", completed="2026-08-02"),
    ])
    client = module.app.test_client()

    found = client.get("/pm-calendar/api/last-completed?assets=7")
    assert found.status_code == 200
    assert found.get_json()["pm"]["task_id"] == "1"

    assert client.get("/pm-calendar/api/last-completed?assets=8").get_json() == {"pm": None}
    # Required: "every asset" has no one last PM to jump to.
    assert client.get("/pm-calendar/api/last-completed").status_code == 400
    assert client.get("/pm-calendar/api/last-completed?assets=").status_code == 400


# ----------------------------------------------------------------------
# Hiding sub-assets from a parent chip's expanded view
# ----------------------------------------------------------------------
def test_hidden_sub_assets_are_left_off_the_calendar(tmp_path):
    service = _panel_line(tmp_path)

    events = service.events(
        asset_ids=["4002"], start_date="2026-10-01", end_date="2026-10-31", exclude=["S1A", "S2"]
    )

    assert _real_asset_ids(events) == ["4002", "S1"]


def test_hiding_is_exact_so_a_re_ticked_machine_under_a_hidden_group_still_shows(tmp_path):
    """The page sends every unticked id; the server hides exactly those."""

    service = _panel_line(tmp_path)

    events = service.events(
        asset_ids=["4002"], start_date="2026-10-01", end_date="2026-10-31", exclude=["4002", "S1", "S2"]
    )

    assert _real_asset_ids(events) == ["S1A"]


def test_hiding_everything_shows_nothing_rather_than_every_asset(tmp_path):
    """An empty list reaching the repository means "every asset" there."""

    service = _panel_line(tmp_path)
    everything = ["4002", "S1", "S1A", "S2"]

    assert service.events(
        asset_ids=["4002"], start_date="2026-10-01", end_date="2026-10-31", exclude=everything
    ) == []
    assert service.summary(asset_ids=["4002"], exclude=everything)["scheduled"] == 0
    assert service.last_completed(["4002"], exclude=everything) is None


def test_the_summary_and_last_done_skip_hidden_sub_assets(tmp_path):
    """Its rows are dated "today" as summary() sees it (pinned above)."""

    service = _panel_line(tmp_path)
    today = pm_calendar_service_module._today().isoformat()
    service.repo.upsert_tasks([
        _pm("40", "S1A", "Sander Dust Collector", today, completed=today),
        _pm("41", "4002", "Panel Finishing System", today, completed=today),
        _pm("42", "S1", "Sander", "2026-05-01", completed="2026-05-01"),
        _pm("43", "S1A", "Sander Dust Collector", "2020-06-01", completed="2020-06-01"),
    ])

    # S1A's PM completed today leaves the tile when S1A is hidden.
    assert service.summary(asset_ids=["4002"], exclude=["S1A"])["completed"] == (
        service.summary(asset_ids=["4002"])["completed"] - 1
    )
    assert service.last_completed(["S1"])["task_id"] == "40"
    assert service.last_completed(["S1"], exclude=["S1A"])["task_id"] == "42"


def test_the_endpoints_pass_exclude_through(monkeypatch, tmp_path):
    module = _app(monkeypatch, tmp_path)
    service = module.pm_calendar_service
    service._ensure_schema()
    service.repo.upsert_tasks([
        _pm("1", "P", "Parent", "2026-10-05"),
        _pm("2", "C", "Child", "2026-10-06", completed="2026-10-06"),
    ])
    service.repo.replace_assets([
        {"asset_id": "P", "asset_name": "Parent", "parent_asset_id": None},
        {"asset_id": "C", "asset_name": "Child", "parent_asset_id": "P"},
    ])
    client = module.app.test_client()

    shown = client.get("/pm-calendar/api/events?assets=P&start=2026-10-01&end=2026-10-31&exclude=C").get_json()
    assert {e["asset_id"] for e in shown["events"] if not e.get("is_projected")} == {"P"}
    assert client.get("/pm-calendar/api/last-completed?assets=P&exclude=C").get_json() == {"pm": None}
    assert client.get("/pm-calendar/api/summary?assets=P&exclude=P,C").get_json()["summary"]["scheduled"] == 0


def test_the_hierarchy_is_read_once_until_the_next_replace(tmp_path):
    service = _panel_line(tmp_path)
    assert service.asset_options()  # warms the cache

    # A cached read never opens the database.
    def no_connection():
        raise AssertionError("fetch_assets went back to the database")

    service.repo.connect = no_connection
    try:
        assert {row["asset_id"] for row in service.repo.fetch_assets()} == {"4002", "S1", "S1A", "S2", "9"}
    finally:
        del service.repo.connect

    # A replace is seen straight away: S2 moved under S1.
    service.repo.replace_assets([
        {"asset_id": "4002", "asset_name": "Panel Finishing System", "parent_asset_id": None},
        {"asset_id": "S1", "asset_name": "Sander", "parent_asset_id": "4002"},
        {"asset_id": "S2", "asset_name": "Oven", "parent_asset_id": "S1"},
    ])
    parents = {row["asset_id"]: row["parent_asset_id"] for row in service.repo.fetch_assets()}
    assert parents == {"4002": None, "S1": "4002", "S2": "S1"}


def test_a_caller_changing_a_row_does_not_change_the_cached_tree(tmp_path):
    service = _panel_line(tmp_path)
    service.repo.fetch_assets()[0]["parent_asset_id"] = "tampered"

    assert "tampered" not in {row["parent_asset_id"] for row in service.repo.fetch_assets()}


def test_limble_links_use_the_configured_app_host(monkeypatch, tmp_path):
    monkeypatch.delenv("LIMBLE_APP_URL", raising=False)
    module = _app(monkeypatch, tmp_path)
    page = module.app.test_client().get("/pm-calendar").get_data(as_text=True)
    assert 'const LIMBLE_APP_URL = "https://app.limblecmms.com";' in page

    monkeypatch.setenv("LIMBLE_APP_URL", "https://eu.example-limble.test/")
    page = module.app.test_client().get("/pm-calendar").get_data(as_text=True)
    assert 'const LIMBLE_APP_URL = "https://eu.example-limble.test";' in page


def test_only_parent_asset_id_is_read_as_the_parent():
    """The bug this guards: `parentID` on this account is not the parent.

    Limble sends a `parentID` on assets that sit at the top of the
    hierarchy too, and reading it hung unrelated machines under whichever
    asset had the matching ID -- 4002 Panel Finishing System showed up as a
    sub-asset of a decommissioned water system, and picking that water
    system dragged in dozens of other assets' PMs.
    """

    assets = [
        {"assetID": 900, "name": "4002-S18 Deionized Water System", "parentAssetID": 0, "parentID": 0},
        {"assetID": 13042, "name": "4002 Panel Finishing System", "parentAssetID": 0, "parentID": 900},
        {"assetID": 6274, "name": "6274 Sullair Air Compressor", "parentAssetID": 0, "parentID": 900},
        {"assetID": 4101, "name": "4002-S01 Sander", "parentAssetID": 13042, "parentID": 900},
    ]

    rows = {
        r["asset_id"]: r["parent_asset_id"]
        for r in PmCalendarService._asset_hierarchy(assets, {"13042", "6274", "4101"})
    }

    # The sander hangs under 4002 and nothing else hangs anywhere: the water
    # system isn't a parent, so it isn't even stored (it has no PMs of its own).
    assert rows == {"13042": None, "6274": None, "4101": "13042"}


def test_an_asset_that_names_itself_as_its_parent_is_top_level():
    assets = [{"assetID": 5, "name": "Loop", "parentAssetID": 5}]

    assert PmCalendarService._asset_hierarchy(assets, {"5"}) == [
        {
            "asset_id": "5", "asset_name": "Loop", "parent_asset_id": None,
            "root_asset_id": "5", "building_asset_id": None, "level": 0, "has_children": 0,
        }
    ]


# ----------------------------------------------------------------------
# The hierarchy the sync materialises (parent, root, level, has children)
# ----------------------------------------------------------------------
def test_the_hierarchy_records_root_level_and_children_for_each_asset():
    """The four answers the Excel hierarchy sheet materialises, per asset."""

    assets = [
        {"assetID": 9000, "name": "3101-3107 Salvagnini", "parentAssetID": 0},
        {"assetID": 9002, "name": "Salvagnini Laser Side", "parentAssetID": 9000},
        {"assetID": 3103, "name": "3103 Salvagnini Fiber Laser", "parentAssetID": 9002},
        {"assetID": 7000, "name": "Unrelated branch", "parentAssetID": 0},
        {"assetID": 7001, "name": "Unrelated machine", "parentAssetID": 7000},
    ]

    rows = {r["asset_id"]: r for r in PmCalendarService._asset_hierarchy(assets, {"3103"})}

    assert set(rows) == {"3103", "9002", "9000"}  # the branch that leads to a PM
    assert [(r["asset_id"], r["level"], r["root_asset_id"], r["has_children"]) for r in rows.values()] == [
        ("3103", 2, "9000", 0),
        ("9000", 0, "9000", 1),
        ("9002", 1, "9000", 1),
    ]


def test_ids_that_differ_only_in_spelling_are_the_same_asset():
    """A parent link written as 4002.0, or with spaces, still connects.

    Limble sends ids as numbers and as strings, and a whole number that has
    been through a float arrives as "4002.0". Two spellings of one id would
    break every link between them.
    """

    assets = [
        {"assetID": 4002.0, "name": "Panel Finishing System", "parentAssetID": None},
        {"assetID": "4101", "name": "Sander", "parentAssetID": " 4002 "},
        {"assetID": 4102, "name": "Chiller", "parentAssetID": {"assetID": "4002"}},
    ]

    rows = {r["asset_id"]: r["parent_asset_id"] for r in PmCalendarService._asset_hierarchy(assets, {"4101", "4102"})}

    assert rows == {"4002": None, "4101": "4002", "4102": "4002"}


def test_a_deep_branch_is_walked_once_per_asset():
    """Level and root are memoised, not re-walked from every asset.

    A chain 60 deep would be ~1,800 steps re-walked per asset; each asset's
    answer is worked out once and reused by everything below it.
    """

    assets = [{"assetID": 1, "name": "root", "parentAssetID": 0}]
    assets += [{"assetID": i, "name": str(i), "parentAssetID": i - 1} for i in range(2, 61)]

    rows = {r["asset_id"]: r for r in PmCalendarService._asset_hierarchy(assets, {"60"})}

    assert rows["60"]["level"] == 59
    assert rows["60"]["root_asset_id"] == "1"
    assert rows["1"]["has_children"] == 1 and rows["60"]["has_children"] == 0


def test_a_hierarchy_longer_than_the_hop_cap_still_returns():
    """Bad data costs a wrong-looking parent, not a hung request."""

    assets = [{"assetID": 1, "name": "root", "parentAssetID": 0}]
    assets += [{"assetID": i, "name": str(i), "parentAssetID": i - 1} for i in range(2, 401)]

    rows = {r["asset_id"]: r for r in PmCalendarService._asset_hierarchy(assets, {"400"})}

    assert rows["400"]["level"] < pm_calendar_service_module._MAX_HIERARCHY_HOPS
    # Whatever was cut off, every asset kept points at assets that were kept.
    assert all(row["root_asset_id"] in rows for row in rows.values())
    assert all(row["parent_asset_id"] in rows or row["parent_asset_id"] is None for row in rows.values())


# ----------------------------------------------------------------------
# Departments and buildings are labels, not rows
# ----------------------------------------------------------------------
def _plant(tmp_path, *, dept_pm=False):
    """The real shape: Dept > Building > machine line > side > machines."""

    assets = [
        {"assetID": 914, "name": "Dept 914 Machinery Maint", "parentAssetID": 0},
        {"assetID": 706, "name": "Building 706", "parentAssetID": 914},
        {"assetID": 13042, "name": "4002 Panel Finishing System", "parentAssetID": 706},
        {"assetID": 5001, "name": "4002-S01 Chemical Spray", "parentAssetID": 13042},
        {"assetID": 9453, "name": "9453 CARRIER AHU 6-4", "parentAssetID": 706},
    ]
    with_pms = {"5001", "9453", "13042"}
    if dept_pm:
        with_pms.add("914")

    service = PmCalendarService(tmp_path / "pm.db")
    service._ensure_schema()
    service.repo.upsert_tasks([_pm(a, a, f"Asset {a}", "2026-10-05") for a in sorted(with_pms)])
    service.repo.replace_assets(PmCalendarService._asset_hierarchy(assets, with_pms))
    return service


def test_the_department_and_building_are_not_rows_in_the_picker(tmp_path):
    """They group thousands of assets; nobody picks a PM schedule by them."""

    options = _plant(tmp_path).asset_options()

    assert [o["asset_id"] for o in options] == ["13042", "5001", "9453"]
    # The line is the first row of its branch, its sub-asset one step in.
    assert [o["depth"] for o in options] == [0, 1, 0]


def test_each_row_says_which_building_and_department_it_is_in(tmp_path):
    """The macro's "Building Name (Dept Name)" label, per row."""

    labels = {o["asset_id"]: o["group_label"] for o in _plant(tmp_path).asset_options()}

    assert labels == {
        "13042": "Building 706 (Dept 914 Machinery Maint)",
        "5001": "Building 706 (Dept 914 Machinery Maint)",
        "9453": "Building 706 (Dept 914 Machinery Maint)",
    }


def test_a_department_with_pms_of_its_own_stays_pickable(tmp_path):
    """A label can't be ticked, so hiding it would lose its own PMs."""

    options = _plant(tmp_path, dept_pm=True).asset_options()

    assert "914" in [o["asset_id"] for o in options]


def test_a_two_level_branch_keeps_its_top_asset(tmp_path):
    """A machine with sub-assets and nothing above it is not a department."""

    assets = [
        {"assetID": 7, "name": "Standalone Press", "parentAssetID": 0},
        {"assetID": 8, "name": "Press Hydraulics", "parentAssetID": 7},
    ]
    service = PmCalendarService(tmp_path / "pm.db")
    service._ensure_schema()
    service.repo.upsert_tasks([_pm("8", "8", "Press Hydraulics", "2026-10-05")])
    service.repo.replace_assets(PmCalendarService._asset_hierarchy(assets, {"8"}))

    options = service.asset_options()

    assert [(o["asset_id"], o["depth"], o["group_label"]) for o in options] == [
        ("7", 0, ""),
        ("8", 1, "Standalone Press"),
    ]
