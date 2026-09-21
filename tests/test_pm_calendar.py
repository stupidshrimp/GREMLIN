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

from repositories.pm_calendar_repo import (
    DEFAULT_PM_CALENDAR_DB_PATH,
    PM_CALENDAR_DB_FILENAME,
    PmCalendarRepository,
    PmCalendarUnavailableError,
)
import repositories.pm_calendar_repo as pm_calendar_repo
import services.pm_calendar_service as pm_calendar_service
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
def _pin_today(monkeypatch, iso_day):
    """Freeze what the projection considers "now".

    The staleness cutoff measures an anchor against today, so without this
    every test below would start failing on a date determined by when it is
    run rather than by what it seeds.
    """

    monkeypatch.setattr(pm_calendar_service, "_today", lambda: iso_day)


def _service(tmp_path, rows):
    service = PmCalendarService(tmp_path / "pm.db")
    service._ensure_schema()
    service.repo.upsert_tasks(rows)
    return service


def _row(task_id, asset_id, name, due, completed=None, *, parent=None, **extra):
    return {
        "task_id": task_id, "asset_id": asset_id, "asset_number": asset_id,
        "asset_name": extra.get("asset_name", f"Asset {asset_id}"),
        "parent_asset_id": parent,
        "task_name": name, "status_raw": "done" if completed else "open",
        "due_date": due, "completed_date": completed,
        "is_completed": 1 if completed else 0,
    }


def _seeded_monthly_series(tmp_path, *, open_due_date):
    """Three completed monthly occurrences, plus a fourth still-open one.

    ``open_due_date`` is the lever the tests below pull: it is the one
    field a scheduler is free to drag around in Limble while a PM is still
    outstanding, and the whole point of anchoring on completed_date is that
    dragging it should not touch what gets projected past it.
    """

    name = "3103 - M - Salvagnini Laser"
    return _service(tmp_path, [
        _row("1", "3103", name, "2026-06-05", "2026-06-04", asset_name="Salvagnini Laser"),
        _row("2", "3103", name, "2026-07-03", "2026-07-02", asset_name="Salvagnini Laser"),
        _row("3", "3103", name, "2026-07-31", "2026-07-30", asset_name="Salvagnini Laser"),
        _row("4", "3103", name, open_due_date, None, asset_name="Salvagnini Laser"),
    ])


def test_future_months_are_filled_with_projected_pms(tmp_path, monkeypatch):
    """A month past whatever Limble has generated still shows something.

    Without projection this window is empty: no real row's due_date falls
    in it. One calendar month at a time from the last completion
    (2026-07-30) is the whole point of the feature.
    """

    _pin_today(monkeypatch, "2026-09-01")
    service = _seeded_monthly_series(tmp_path, open_due_date="2026-08-28")

    events = service.events(asset_ids=["3103"], start_date="2026-09-01", end_date="2026-11-30")

    assert [e["due_date"] for e in events] == ["2026-09-30", "2026-10-30", "2026-11-30"]
    assert all(e["is_projected"] for e in events)


def test_dragging_the_open_tasks_due_date_does_not_move_the_projected_series(tmp_path, monkeypatch):
    """The exact scenario the feature exists to survive.

    Two services, identical completed history, differing only in where the
    still-open next occurrence's due date happens to sit right now -- one
    left where the cadence would naturally put it, the other dragged five
    months out, standing in for a reschedule. Projected pills for a window
    neither due date touches must come out identical either way: the
    series is built from completed_date alone and never reads the open
    row's due_date at all.
    """

    _pin_today(monkeypatch, "2026-09-01")
    on_schedule = _seeded_monthly_series(tmp_path / "a", open_due_date="2026-08-27")
    dragged = _seeded_monthly_series(tmp_path / "b", open_due_date="2027-03-01")

    window = dict(asset_ids=["3103"], start_date="2026-09-01", end_date="2026-12-31")
    on_schedule_dates = [e["due_date"] for e in on_schedule.events(**window)]
    dragged_dates = [e["due_date"] for e in dragged.events(**window)]

    assert on_schedule_dates == dragged_dates == [
        "2026-09-30", "2026-10-30", "2026-11-30", "2026-12-30",
    ]


def test_a_projected_pill_yields_to_a_real_row_already_covering_its_slot(tmp_path, monkeypatch):
    """The one place a real due date *is* allowed to matter: its own slot.

    The dragged-out due date (2027-03-01) sits close enough to where the
    unbroken cadence would have projected one (2027-02-28) that showing
    both would just be the same PM twice. Only that one slot yields --
    everything on either side of it keeps projecting on schedule.
    """

    _pin_today(monkeypatch, "2026-09-01")
    service = _seeded_monthly_series(tmp_path, open_due_date="2027-03-01")

    events = service.events(asset_ids=["3103"], start_date="2027-01-01", end_date="2027-04-01")

    by_date = {e["due_date"]: e for e in events}
    # Real rows don't carry an is_projected key at all -- only synthetic
    # ones do -- so "not set" is what a real row winning looks like here.
    assert not by_date["2027-03-01"].get("is_projected")
    assert "2027-02-28" not in by_date  # the projected slot it absorbed
    assert by_date["2027-01-30"]["is_projected"] is True  # neighbours unaffected
    assert by_date["2027-03-30"]["is_projected"] is True


@pytest.mark.parametrize(
    "code, first_projection",
    [
        ("2W", "2026-01-16"),  # the one genuinely week-based cadence
        ("M", "2026-02-02"),
        ("Q", "2026-04-02"),
        ("SA", "2026-07-02"),
        ("A", "2027-01-02"),
        ("3Y", "2029-01-02"),
    ],
)
def test_each_code_projects_at_its_fixed_interval(tmp_path, monkeypatch, code, first_projection):
    """One fixed interval per code -- the table, not the template's own setting.

    SA is six months here even though some SA templates in Limble are set to
    24 weeks: the calendar projects the standard cadence a code stands for.
    """

    _pin_today(monkeypatch, "2026-01-03")
    service = _service(tmp_path, [
        _row("1", "1435", f"1435 - {code} - Stokes Tablet", "2026-01-02", "2026-01-02"),
    ])

    events = service.events(asset_ids=["1435"], start_date="2026-01-03", end_date="2030-12-31")

    assert events[0]["due_date"] == first_projection


def test_monthly_projections_track_the_calendar_not_a_28_day_cycle(tmp_path, monkeypatch):
    """Twelve occurrences in a year, each on the anchor's day of the month.

    Four weeks would put thirteen in a year and walk the date backwards
    through it -- fine in a one-month view, visibly wrong once a series runs
    out a few years.
    """

    _pin_today(monkeypatch, "2026-01-16")
    service = _service(tmp_path, [
        _row("1", "3103", "3103 - M - Salvagnini Laser", "2026-01-15", "2026-01-15"),
    ])

    dates = [e["due_date"] for e in service.events(
        asset_ids=["3103"], start_date="2026-01-16", end_date="2027-01-15")]

    assert len(dates) == 12
    assert {d[-2:] for d in dates} == {"15"}


def test_a_month_end_anchor_clamps_instead_of_spilling_into_the_next_month(tmp_path, monkeypatch):
    """The 31st stays the 31st wherever the month is long enough to have one.

    Each date is anchor + n months rather than one month past the previous
    projection, so February's clamp doesn't drag the rest of the series down
    to the 28th with it.
    """

    _pin_today(monkeypatch, "2026-02-01")
    service = _service(tmp_path, [
        _row("1", "3103", "3103 - M - Salvagnini Laser", "2026-01-31", "2026-01-31"),
    ])

    dates = [e["due_date"] for e in service.events(
        asset_ids=["3103"], start_date="2026-02-01", end_date="2026-05-31")]

    assert dates == ["2026-02-28", "2026-03-31", "2026-04-30", "2026-05-31"]


def test_two_pms_of_the_same_cadence_on_one_asset_project_as_separate_lines(tmp_path, monkeypatch):
    """Different work, same "M" -- two series, not one.

    Keying the series on (asset_id, code) merged these into a single stream:
    one line disappeared from every future month, and the survivor was
    labelled with whichever row sorted first while running on the other's
    schedule. The description is what tells them apart, so the whole name is
    the series identity.
    """

    _pin_today(monkeypatch, "2026-09-01")
    service = _service(tmp_path, [
        _row("1", "3103", "3103 - M - Laser Optics", "2026-08-05", "2026-08-05"),
        _row("2", "3103", "3103 - M - Laser Chiller", "2026-08-20", "2026-08-20"),
    ])

    events = service.events(asset_ids=["3103"], start_date="2026-09-01", end_date="2026-11-30")

    assert [(e["due_date"], e["task_name"]) for e in events] == [
        ("2026-09-05", "3103 - M - Laser Optics"),
        ("2026-09-20", "3103 - M - Laser Chiller"),
        ("2026-10-05", "3103 - M - Laser Optics"),
        ("2026-10-20", "3103 - M - Laser Chiller"),
        ("2026-11-05", "3103 - M - Laser Optics"),
        ("2026-11-20", "3103 - M - Laser Chiller"),
    ]
    # Each line anchored on its own last completion, not on the later of the two.
    assert len({e["task_id"] for e in events}) == 6


def test_a_line_that_stopped_recurring_stops_being_projected(tmp_path, monkeypatch):
    """A stale anchor is a retired PM, not a very overdue one.

    777 was last done in 2019 and nothing has happened since -- a scrapped
    asset, a deleted template, a renamed line. Extrapolating from it draws
    pills into 2027 that look exactly like an estimate anchored on last
    month's work. 778 is the control: same cadence, live anchor, still
    projects.
    """

    _pin_today(monkeypatch, "2026-09-01")
    service = _service(tmp_path, [
        _row("1", "777", "777 - A - Annual inspection", "2019-05-01", "2019-05-01"),
        _row("2", "778", "778 - A - Annual inspection", "2026-05-01", "2026-05-01"),
    ])

    events = service.events(
        asset_ids=["777", "778"], start_date="2027-01-01", end_date="2027-12-31")

    assert [(e["asset_id"], e["due_date"]) for e in events] == [("778", "2027-05-01")]


def test_events_without_an_asset_filter_never_reads_the_whole_table(tmp_path, monkeypatch):
    """``?assets=`` omitted must not turn a month view into SELECT * FROM pm_task.

    Projection needs each series' history, which with no asset filter is
    every row in the table -- materialised per request, on an endpoint a
    bare GET can reach. The page always sends its chipped-in assets, so the
    unscoped call keeps working and stays cheap; it just doesn't project.
    """

    _pin_today(monkeypatch, "2026-09-01")
    service = _service(tmp_path, [
        _row("1", "3103", "3103 - M - Laser Optics", "2026-08-05", "2026-08-05"),
        _row("2", "1435", "1435 - M - Stokes Tablet", "2026-08-06", "2026-08-06"),
    ])

    calls = []
    inner = service.repo.fetch_tasks
    monkeypatch.setattr(
        service.repo, "fetch_tasks",
        lambda **kw: (calls.append(kw), inner(**kw))[1],
    )

    events = service.events(asset_ids=None, start_date="2026-09-01", end_date="2026-09-30")

    assert not any(e.get("is_projected") for e in events)
    assert len(calls) == 1  # the window read only -- no history sweep
    assert calls[0]["due_since"] and calls[0]["due_until"]


def test_the_history_read_is_bounded_even_when_an_asset_filter_is_given(tmp_path, monkeypatch):
    """The anchor hunt looks back a bounded distance, not to the start of time."""

    _pin_today(monkeypatch, "2026-09-01")
    service = _seeded_monthly_series(tmp_path, open_due_date="2026-08-28")

    calls = []
    inner = service.repo.fetch_tasks
    monkeypatch.setattr(
        service.repo, "fetch_tasks",
        lambda **kw: (calls.append(kw), inner(**kw))[1],
    )

    service.events(asset_ids=["3103"], start_date="2026-09-01", end_date="2026-09-30")

    assert len(calls) == 2
    assert calls[1]["due_since"] is not None


def test_a_pm_name_that_does_not_match_the_cadence_convention_is_left_alone(tmp_path, monkeypatch):
    """No code to parse means no projection -- not a crash, not a guess."""

    _pin_today(monkeypatch, "2026-09-01")
    service = _service(tmp_path, [
        _row("1", "9", "Replace worn belt", "2026-06-01", "2026-06-01"),
    ])

    events = service.events(asset_ids=["9"], start_date="2026-06-01", end_date="2027-06-01")

    assert len(events) == 1
    assert events[0]["task_id"] == "1"


@pytest.mark.parametrize(
    "name",
    [
        "3103 — M — Salvagnini Laser",  # em dashes, not " - "
        "3103 - Mo - Salvagnini Laser",  # code that isn't in the table
        "Salvagnini Laser monthly",  # no segments at all
    ],
)
def test_a_drifted_pm_name_yields_no_cadence_code(name):
    """Every row reaching _cadence_code is a real PM, so a None is a PM lost.

    Not an error -- there is nothing to fall back on -- but the sync counts
    these so a drift in the naming convention is visible rather than showing
    up months later as an asset with no future PMs.
    """

    assert pm_calendar_service._cadence_code(name) is None


# ----------------------------------------------------------------------
# Asset hierarchy: picking a parent picks everything under it
# ----------------------------------------------------------------------
def _seeded_hierarchy(tmp_path):
    """4002 with two sub-assets, one of which has a sub-asset of its own.

    Mirrors the shape the real account has: a parent that carries PMs in its
    own right *and* children that carry theirs, so "did picking the parent
    include its own work as well as its children's" is answerable.
    """

    return _service(tmp_path, [
        _row("1", "4002", "4002 - M - PFS", "2026-09-01", "2026-09-01",
             asset_name="4002 Panel Finishing System"),
        _row("2", "4002-S05", "4002-S05 - M - Chemical Spray", "2026-09-02", "2026-09-02",
             parent="4002", asset_name="4002-S05 Chemical Spray"),
        _row("3", "4002-S19", "4002-S19 - M - Chain", "2026-09-03", "2026-09-03",
             parent="4002", asset_name="4002-S19 Chain"),
        _row("4", "4002-S19-A", "4002-S19-A - M - Chain Motor", "2026-09-04", "2026-09-04",
             parent="4002-S19", asset_name="4002-S19-A Chain Motor"),
        _row("5", "7001", "7001 - M - Unrelated", "2026-09-05", "2026-09-05",
             asset_name="7001 Unrelated Press"),
    ])


def _real_asset_ids(service, asset_ids, monkeypatch):
    _pin_today(monkeypatch, "2026-09-10")
    events = service.events(
        asset_ids=asset_ids, start_date="2026-09-01", end_date="2026-09-30")
    return sorted({e["asset_id"] for e in events if not e.get("is_projected")})


def test_picking_a_parent_includes_its_own_pms_and_every_descendant(tmp_path, monkeypatch):
    """One chip, the whole branch -- parent included, not just the children."""

    service = _seeded_hierarchy(tmp_path)

    assert _real_asset_ids(service, ["4002"], monkeypatch) == [
        "4002", "4002-S05", "4002-S19", "4002-S19-A",
    ]


def test_picking_a_child_leaves_its_siblings_and_parent_out(tmp_path, monkeypatch):
    """Sub-assets stay individually selectable; expansion only goes downwards."""

    service = _seeded_hierarchy(tmp_path)

    assert _real_asset_ids(service, ["4002-S05"], monkeypatch) == ["4002-S05"]


def test_picking_a_middle_asset_takes_its_own_branch_only(tmp_path, monkeypatch):
    """Expansion is the subtree under what was picked, not the whole tree."""

    service = _seeded_hierarchy(tmp_path)

    assert _real_asset_ids(service, ["4002-S19"], monkeypatch) == ["4002-S19", "4002-S19-A"]


def test_the_ytd_summary_counts_a_parents_descendants_too(tmp_path, monkeypatch):
    """The tiles and the grid have to agree about what a chip covers."""

    _pin_today(monkeypatch, "2026-09-10")
    service = _seeded_hierarchy(tmp_path)

    assert service.summary(asset_ids=["4002"])["completed"] == 4
    assert service.summary(asset_ids=["4002-S05"])["completed"] == 1


def test_a_parent_loop_in_the_data_does_not_hang_the_request(tmp_path, monkeypatch):
    """Limble's hierarchy is a tree in practice; a cycle must not spin a GET.

    This is reachable by a plain GET, so a bad parent link has to terminate
    rather than take the worker thread with it.
    """

    service = _service(tmp_path, [
        _row("1", "A", "A - M - One", "2026-09-01", "2026-09-01", parent="B"),
        _row("2", "B", "B - M - Two", "2026-09-02", "2026-09-02", parent="A"),
    ])

    assert _real_asset_ids(service, ["A"], monkeypatch) == ["A", "B"]


def test_a_parent_with_no_pms_of_its_own_is_not_a_parent_here(tmp_path, monkeypatch):
    """Only assets that reached pm_task can be picked, so only they can nest.

    9000 is named as the parent but has no PMs, so it never appears in the
    picker. Its child stands on its own rather than becoming unreachable.
    """

    service = _service(tmp_path, [
        _row("1", "9001", "9001 - M - Orphan", "2026-09-01", "2026-09-01", parent="9000"),
    ])

    assert [a["asset_id"] for a in service.asset_options()] == ["9001"]
    assert _real_asset_ids(service, ["9001"], monkeypatch) == ["9001"]


def test_asset_options_carries_the_parent_so_the_picker_can_nest(tmp_path):
    service = _seeded_hierarchy(tmp_path)

    parents = {a["asset_id"]: a["parent_asset_id"] for a in service.asset_options()}

    assert parents == {
        "4002": None, "4002-S05": "4002", "4002-S19": "4002",
        "4002-S19-A": "4002-S19", "7001": None,
    }


def test_the_sync_stores_each_tasks_parent_asset(tmp_path):
    """The hierarchy comes off the /assets payload the sync already fetches."""

    service = PmCalendarService(tmp_path / "pm.db")
    row = service._map_pm_task(
        {"taskID": "1", "assetID": "4002-S05", "type": "1", "name": "4002-S05 - M - Spray"},
        {"4002-S05": "4002-S05 Chemical Spray"},
        {"4002-S05": "4002"},
    )

    assert row["parent_asset_id"] == "4002"


def test_an_older_database_gains_the_parent_column_without_losing_rows(tmp_path):
    """pm_task predates parent_asset_id, and CREATE TABLE IF NOT EXISTS is a no-op.

    A database written by the previous version has to pick the column up on
    the next start rather than needing to be deleted and re-synced.
    """

    db_path = tmp_path / "pm.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE pm_task (task_id TEXT PRIMARY KEY, asset_id TEXT, "
        "asset_number TEXT, asset_name TEXT, task_name TEXT, status_raw TEXT, "
        "due_date TEXT, completed_date TEXT, is_completed INTEGER NOT NULL DEFAULT 0, "
        "synced_at TEXT NOT NULL DEFAULT (datetime('now')))"
    )
    conn.execute("INSERT INTO pm_task (task_id, asset_id) VALUES ('1', '4002')")
    conn.commit()
    conn.close()

    repo = PmCalendarRepository(db_path)
    repo.ensure_schema()

    assert repo.asset_parent_map() == {"4002": None}
    assert len(repo.fetch_tasks()) == 1


# ----------------------------------------------------------------------
# Reading a line's cadence from its own completion history
# ----------------------------------------------------------------------
def _monthly_history(asset_id, name):
    """Four completions a month apart -- enough gaps to read a cadence from."""

    return [
        _row("1", asset_id, name, "2026-05-15", "2026-05-15"),
        _row("2", asset_id, name, "2026-06-15", "2026-06-15"),
        _row("3", asset_id, name, "2026-07-15", "2026-07-15"),
        _row("4", asset_id, name, "2026-08-15", "2026-08-15"),
    ]


def test_a_pm_with_no_code_in_its_name_projects_from_its_own_history(tmp_path, monkeypatch):
    """The 84% case: a real name from this account, carrying no cadence code.

    Before this, "HYDMECH BAND SAW PM INSPECTION" could never be projected --
    nothing in it parses -- and that asset's future months stayed blank no
    matter how regularly the PM had actually been done.
    """

    _pin_today(monkeypatch, "2026-09-01")
    service = _service(tmp_path, _monthly_history("77", "HYDMECH BAND SAW PM INSPECTION"))

    events = service.events(asset_ids=["77"], start_date="2026-09-01", end_date="2026-11-30")

    assert [e["due_date"] for e in events] == ["2026-09-15", "2026-10-15", "2026-11-15"]
    assert all(e["is_projected"] for e in events)


def test_history_beats_the_code_in_the_name_when_they_disagree(tmp_path, monkeypatch):
    """A template whose real cadence drifted from what it was named.

    The name says annual; the line has in fact been done monthly for four
    months. What happened is the better authority than what it was called,
    so the projection follows the history.
    """

    _pin_today(monkeypatch, "2026-09-01")
    service = _service(tmp_path, _monthly_history("77", "77 - A - Mislabelled line"))

    events = service.events(asset_ids=["77"], start_date="2026-09-01", end_date="2026-11-30")

    assert [e["due_date"] for e in events] == ["2026-09-15", "2026-10-15", "2026-11-15"]


def test_gaps_that_disagree_fall_back_to_the_name(tmp_path, monkeypatch):
    """Two completions days apart and then nothing for months is not a cadence.

    A median over those gaps would invent one. The line still has a code in
    its name, so that is what it projects on -- annually, from the last
    completion, not monthly from a fabricated gap.
    """

    _pin_today(monkeypatch, "2026-09-01")
    service = _service(tmp_path, [
        _row("1", "77", "77 - A - Erratic", "2026-01-05", "2026-01-05"),
        _row("2", "77", "77 - A - Erratic", "2026-01-08", "2026-01-08"),
        _row("3", "77", "77 - A - Erratic", "2026-08-15", "2026-08-15"),
    ])

    events = service.events(asset_ids=["77"], start_date="2027-01-01", end_date="2027-12-31")

    assert [e["due_date"] for e in events] == ["2027-08-15"]


def test_no_usable_history_and_no_code_projects_nothing(tmp_path, monkeypatch):
    """Neither source available is the one case that still draws a blank."""

    _pin_today(monkeypatch, "2026-09-01")
    service = _service(tmp_path, [
        _row("1", "77", "HYDMECH BAND SAW PM INSPECTION", "2026-01-05", "2026-01-05"),
        _row("2", "77", "HYDMECH BAND SAW PM INSPECTION", "2026-01-08", "2026-01-08"),
        _row("3", "77", "HYDMECH BAND SAW PM INSPECTION", "2026-08-15", "2026-08-15"),
    ])

    events = service.events(asset_ids=["77"], start_date="2026-09-01", end_date="2027-12-31")

    assert not any(e.get("is_projected") for e in events)


def test_too_few_completions_falls_back_to_the_name(tmp_path, monkeypatch):
    """Two completions make one gap, and one gap is not a pattern."""

    _pin_today(monkeypatch, "2026-09-01")
    service = _service(tmp_path, [
        _row("1", "77", "77 - A - Sparse", "2026-07-15", "2026-07-15"),
        _row("2", "77", "77 - A - Sparse", "2026-08-15", "2026-08-15"),
    ])

    events = service.events(asset_ids=["77"], start_date="2027-01-01", end_date="2027-12-31")

    assert [e["due_date"] for e in events] == ["2027-08-15"]


def test_an_inferred_interval_near_a_standard_cadence_snaps_to_it(tmp_path, monkeypatch):
    """~61 days is "every two months", and should stay on its day of the month.

    Left as a raw 61-day step it would walk off the 10th within a year. The
    2M entry exists for exactly this: no PM here is *named* 2M, but plenty
    repeat on it.
    """

    _pin_today(monkeypatch, "2026-09-01")
    service = _service(tmp_path, [
        _row("1", "77", "BIMONTHLY FILTER CHANGE", "2026-04-10", "2026-04-10"),
        _row("2", "77", "BIMONTHLY FILTER CHANGE", "2026-06-10", "2026-06-10"),
        _row("3", "77", "BIMONTHLY FILTER CHANGE", "2026-08-10", "2026-08-10"),
    ])

    dates = [e["due_date"] for e in service.events(
        asset_ids=["77"], start_date="2026-09-01", end_date="2027-02-28")]

    assert dates == ["2026-10-10", "2026-12-10", "2027-02-10"]


def test_an_interval_matching_no_standard_cadence_keeps_its_measured_days(tmp_path, monkeypatch):
    """45 days is not a calendar period, and pretending otherwise would lie."""

    _pin_today(monkeypatch, "2026-09-01")
    service = _service(tmp_path, [
        _row("1", "77", "45 DAY INSPECTION", "2026-05-01", "2026-05-01"),
        _row("2", "77", "45 DAY INSPECTION", "2026-06-15", "2026-06-15"),
        _row("3", "77", "45 DAY INSPECTION", "2026-07-30", "2026-07-30"),
    ])

    dates = [e["due_date"] for e in service.events(
        asset_ids=["77"], start_date="2026-09-01", end_date="2026-12-31")]

    assert dates == ["2026-09-13", "2026-10-28", "2026-12-12"]


def test_the_sync_counts_unprojectable_lines_not_unprojectable_rows(tmp_path):
    """One busy PM must not drown out the silent ones in the reported number."""

    projectable = _monthly_history("77", "PROJECTABLE FROM HISTORY")
    silent = [
        _row("9", "88", "SILENT LINE PM", "2026-01-05", "2026-01-05"),
        _row("10", "88", "SILENT LINE PM", "2026-01-08", "2026-01-08"),
        _row("11", "88", "SILENT LINE PM", "2026-08-15", "2026-08-15"),
    ]

    assert PmCalendarService._count_unprojectable_lines(projectable + silent) == 1


# ----------------------------------------------------------------------
# Reading a cadence out of the several ways this account writes a name
# ----------------------------------------------------------------------
@pytest.mark.parametrize(
    "task_name, expected",
    [
        # The spelling the convention was written against.
        ("3209 - Q - Schmidt Scribing Machine", "Q"),
        # The same convention without spaces, which is most of the account.
        ("8945-Q-DEHUMIDIFIER FANTECH", "Q"),
        ("3359-M-Sandblaster", "M"),
        # Mixed spacing, and lower case.
        ("3209 -sa- Schmidt", "SA"),
        # A sub-asset brings its own hyphen: S19 sits where a positional rule
        # would look, and the cadence is one field further along.
        ("4002-S19-M-Chain Drive", "M"),
        # Some lines put the cadence last.
        ("11000 HVAC 103-1 - Q", "Q"),
        # Spellings of a cadence the table already holds.
        ("1804-BIM-Magneform", "2M"),
        ("8688-BIW-ITT B&G Heat Exchanger", "2W"),
        # Nothing that resembles a code.
        ("HYDMECH BAND SAW PM INSPECTION", None),
        ("Econo Lift Tables PM Inspection", None),
        # Fields that are not cadences must not be mistaken for them.
        ("3458- AC-RTU 5.1 ROOF TOP UNIT BLDG. 5", None),
        ("3414-RF 5-6 RETURN FAN BLDG 5", None),
        ("4002-S09 Dry Off Oven", None),
        ("", None),
        (None, None),
    ],
)
def test_the_cadence_code_is_found_however_the_name_is_punctuated(task_name, expected):
    assert pm_calendar_service._cadence_code(task_name) == expected


def test_a_hyphen_written_name_projects_like_a_spaced_one(tmp_path, monkeypatch):
    """End to end: the punctuation must not decide whether a PM is projected."""

    _pin_today(monkeypatch, "2026-09-01")
    spaced = _service(tmp_path / "a", [
        _row("1", "77", "77 - M - Boiler", "2026-08-15", "2026-08-15")])
    hyphened = _service(tmp_path / "b", [
        _row("1", "77", "77-M-Boiler", "2026-08-15", "2026-08-15")])

    window = dict(asset_ids=["77"], start_date="2026-09-01", end_date="2026-11-30")
    assert [e["due_date"] for e in spaced.events(**window)] == \
           [e["due_date"] for e in hyphened.events(**window)] == \
           ["2026-09-15", "2026-10-15", "2026-11-15"]


# ----------------------------------------------------------------------
# Selecting a whole department: more asset ids than one statement can hold
# ----------------------------------------------------------------------
def test_a_selection_too_large_for_one_statement_still_returns_every_row(tmp_path, monkeypatch):
    """Picking a department expands to hundreds of assets.

    SQLite before 3.32 allows 999 bound parameters and this account's largest
    department already covers 783, so the id list has to be batched -- and
    going over is a hard "too many SQL variables" error, not a slow query.
    The batch size is shrunk here rather than seeding a thousand assets.
    """

    monkeypatch.setattr(pm_calendar_repo, "_ASSET_ID_BATCH", 3)
    ids = [str(n) for n in range(10)]
    service = _service(tmp_path, [
        # Due dates deliberately descending, so a batch-by-batch result that
        # was never merged would come back out of order.
        _row(str(n), str(n), f"{n} - M - Line", f"2026-09-{20 - n:02d}", None)
        for n in range(10)
    ])

    rows = service.repo.fetch_tasks(asset_ids=ids)

    assert len(rows) == 10
    assert [r["due_date"] for r in rows] == sorted(r["due_date"] for r in rows)


def test_batching_does_not_disturb_a_selection_that_fits_in_one_statement(tmp_path):
    service = _service(tmp_path, [
        _row("1", "5", "5 - M - One", "2026-09-02", None),
        _row("2", "6", "6 - M - Two", "2026-09-01", None),
    ])

    rows = service.repo.fetch_tasks(asset_ids=["5", "6"])

    assert [r["task_id"] for r in rows] == ["2", "1"]
