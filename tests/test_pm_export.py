"""The manager export: the window it covers, the figures, and the download.

Same database override as test_pm_calendar.py, for the same reason given at
the top of that file: the real default is a Windows path, and letting it
stand on a POSIX runner would create ``C:\\GREMLIN`` as a folder in the tree.

"Today" is pinned here too. The export reports on a window measured back
from today and classifies a PM as missed or merely not-yet-due by comparing
against it, so every expectation below would start drifting the day after it
was written.
"""

import importlib
from datetime import date

import pytest

import services.pm_calendar_service as pm_calendar_service_module
from services.pm_calendar_service import PmCalendarService
from services.pm_export_service import (
    LATE,
    MISSED,
    ON_TIME,
    OPEN,
    PmExportService,
    window_for,
)
from services import html_to_pdf

TODAY = date(2026, 9, 21)


@pytest.fixture(autouse=True)
def _fixed_today(monkeypatch):
    monkeypatch.setattr(pm_calendar_service_module, "_today", lambda: TODAY)


def _app(monkeypatch, tmp_path, *, pm_db=None):
    monkeypatch.setenv("GREMLIN_ACCESS_DB_PATH", str(tmp_path / "accesscontrol.db"))
    monkeypatch.setenv("GREMLIN_DB_PATH", str(tmp_path / "gremlin.db"))
    monkeypatch.setenv("GREMLIN_BUGS_DB_PATH", str(tmp_path / "auxillary.db"))
    monkeypatch.setenv("GREMLIN_PM_CALENDAR_DB_PATH", str(pm_db or tmp_path / "pm.db"))
    monkeypatch.setenv("GREMLIN_ADMIN_USERNAME", "root")
    monkeypatch.setenv("GREMLIN_ADMIN_PIN", "secret")
    import app

    return importlib.reload(app)


def _signed_in(module, department="operations_maintenance"):
    module.access_control.save_user(None, "tester", "2468", "viewer", department, "all")
    client = module.app.test_client()
    assert client.post(
        "/auth/login", json={"username": "tester", "pin": "2468"}
    ).status_code == 200
    return client


def _task(task_id, asset_id, name, due, completed=None, asset_name="Laser"):
    return {
        "task_id": task_id,
        "asset_id": asset_id,
        "asset_number": asset_id,
        "asset_name": asset_name,
        "task_name": name,
        "status_raw": "done" if completed else "open",
        "due_date": due,
        "completed_date": completed,
        "is_completed": 1 if completed else 0,
    }


def _seeded(tmp_path, rows=None):
    """One asset with a mixed record: on time, late, missed and not yet due."""

    service = PmCalendarService(tmp_path / "pm.db")
    service._ensure_schema()
    service.repo.replace_assets([
        {"asset_id": "100", "asset_name": "Laser", "parent_asset_id": None,
         "root_asset_id": "100", "building_asset_id": None, "level": 0, "has_children": 0},
    ])
    service.repo.upsert_tasks(rows if rows is not None else [
        # Done the day it was due.
        _task("1", "100", "100 - M - Laser", "2026-05-08", "2026-05-08"),
        # Done eleven days late.
        _task("2", "100", "100 - M - Laser", "2026-06-05", "2026-06-16"),
        # Due in the past, never done.
        _task("3", "100", "100 - Q - Laser", "2026-07-02"),
        # Due after today: not a failure, just not yet.
        _task("4", "100", "100 - M - Laser", "2026-09-30"),
        # Long before the window, and the asset's last completion before it.
        _task("5", "100", "100 - SA - Laser", "2025-02-10", "2025-02-11"),
    ])
    return service


def _export(tmp_path, **kwargs):
    return PmExportService(_seeded(tmp_path, **kwargs))


# ----------------------------------------------------------------------
# The window
# ----------------------------------------------------------------------
def test_the_window_covers_whole_months():
    """A window ending mid-month would draw a half month on the calendar.

    The grids are a month at a time, and half of September reads as though
    nothing was scheduled in the second half of it.
    """

    start, end, _ = window_for("year_to_date", today=TODAY)
    assert start == "2026-01-01"
    assert end == "2026-09-30"


def test_the_six_month_preset_counts_back_from_this_month():
    start, end, label = window_for("last_6_months", today=TODAY)
    assert (start, end) == ("2026-04-01", "2026-09-30")
    assert label == "April 2026 to September 2026"


def test_the_twelve_month_preset_includes_this_month():
    start, end, _ = window_for("last_12_months", today=TODAY)
    assert (start, end) == ("2025-10-01", "2026-09-30")


def test_custom_dates_are_snapped_outwards():
    start, end, _ = window_for(None, "2026-03-14", "2026-05-02", today=TODAY)
    assert (start, end) == ("2026-03-01", "2026-05-31")


def test_a_backwards_window_is_turned_around():
    """Two month pickers make this easy to do and pointless to refuse."""

    start, end, _ = window_for(None, "2026-05-01", "2026-03-01", today=TODAY)
    assert (start, end) == ("2026-03-01", "2026-05-31")


def test_one_month_is_labelled_as_one_month():
    _, _, label = window_for(None, "2026-03-01", "2026-03-20", today=TODAY)
    assert label == "March 2026"


def test_an_unknown_preset_falls_back_rather_than_failing():
    """The export is a read; a typo in a query string should still answer."""

    assert window_for("last_decade", today=TODAY) == window_for("last_6_months", today=TODAY)


# ----------------------------------------------------------------------
# The figures
# ----------------------------------------------------------------------
def test_each_pm_is_classified_by_what_happened_to_it(tmp_path):
    report = _export(tmp_path).report(["100"], preset="last_12_months")
    by_task = {row["task_id"]: row["status"] for row in report["rows"]}
    assert by_task["1"] == ON_TIME
    assert by_task["2"] == LATE
    assert by_task["3"] == MISSED
    assert by_task["4"] == OPEN


def test_a_pm_not_yet_due_is_left_out_of_the_percentages(tmp_path):
    """Otherwise a window running into the future looks like a collapse.

    Four PMs were due in the window and one of them has not come due yet, so
    the rates are measured against the three that have.
    """

    totals = _export(tmp_path).report(["100"], preset="last_12_months")["totals"]
    assert totals["due"] == 4
    assert totals["open"] == 1
    assert totals["answerable"] == 3
    assert totals["on_time_rate"] == pytest.approx(33.3)
    assert totals["completion_rate"] == pytest.approx(66.7)


def test_days_late_is_counted_from_the_due_date(tmp_path):
    report = _export(tmp_path).report(["100"], preset="last_12_months")
    late = next(row for row in report["rows"] if row["task_id"] == "2")
    assert late["days"] == 11
    assert report["totals"]["worst_days_late"] == 11
    assert report["totals"]["average_days_late"] == 11.0


def test_an_outstanding_pm_counts_the_days_it_has_been_outstanding(tmp_path):
    missed = next(
        row for row in _export(tmp_path).report(["100"], preset="last_12_months")["rows"]
        if row["task_id"] == "3"
    )
    assert missed["days"] == (TODAY - date(2026, 7, 2)).days


def test_a_pm_due_in_the_window_counts_there_however_late_it_was_done(tmp_path):
    """The spine of the report, and the thing most likely to be got wrong.

    A PM due in May and finished in October belongs to May's figures --
    crediting October would count last period's backlog as this period's
    compliance.
    """

    service = _seeded(tmp_path, rows=[
        _task("1", "100", "100 - M - Laser", "2026-05-08", "2026-08-30"),
    ])
    report = PmExportService(service).report(
        ["100"], start="2026-05-01", end="2026-05-31"
    )
    assert report["totals"]["due"] == 1
    assert report["rows"][0]["status"] == LATE
    assert report["rows"][0]["days"] == 114


def test_the_detail_table_puts_the_worst_rows_first(tmp_path):
    """A manager reads the top of a table, not the middle of it."""

    order = [row["status"] for row in _export(tmp_path).report(["100"], preset="last_12_months")["rows"]]
    assert order == [MISSED, LATE, OPEN, ON_TIME]


def test_last_completed_ignores_the_window(tmp_path):
    """"When was it last done" is not a question about the reporting period.

    An asset with nothing in the window is exactly the one a reader wants
    that date for, so it is read over the whole history.
    """

    report = _export(tmp_path).report(["100"], start="2026-08-01", end="2026-08-31")
    assert report["totals"]["due"] == 0
    assert report["per_asset"][0]["last_completed_date"] == "2026-06-16"


def test_an_asset_that_has_never_been_completed_says_so(tmp_path):
    service = _seeded(tmp_path, rows=[_task("1", "100", "100 - M - Laser", "2026-05-08")])
    report = PmExportService(service).report(["100"], preset="last_12_months")
    assert report["per_asset"][0]["last_completed_date"] is None


def test_the_cadence_filter_narrows_every_part_of_the_report(tmp_path):
    report = _export(tmp_path).report(["100"], preset="last_12_months", codes=["Q"])
    assert [row["task_id"] for row in report["rows"]] == ["3"]
    assert report["totals"]["due"] == 1
    assert report["cadences"]["filtered"] is True
    assert report["cadences"]["label"] == "Quarterly"
    # The one quarterly PM was never completed, so with the filter on there
    # is no last completion to report either.
    assert report["per_asset"][0]["last_completed_date"] is None


def test_no_filter_is_labelled_as_all_frequencies(tmp_path):
    report = _export(tmp_path).report(["100"], preset="last_12_months")
    assert report["cadences"] == {"codes": [], "label": "All frequencies", "filtered": False}


def test_months_are_counted_by_due_date_for_the_chart(tmp_path):
    report = _export(tmp_path).report(["100"], preset="last_12_months")
    by_month = {month["key"]: month for month in report["by_month"]}
    assert by_month["2026-05"]["on_time"] == 1
    assert by_month["2026-06"]["late"] == 1
    assert by_month["2026-07"]["missed"] == 1
    # Every month in the window is present, so the chart has no gaps in it.
    assert len(report["by_month"]) == 12


# ----------------------------------------------------------------------
# The calendar
# ----------------------------------------------------------------------
def test_the_calendar_carries_only_days_with_something_on_them(tmp_path):
    """Printed month grids are mostly empty cells; these are not grids."""

    report = _export(tmp_path).report(["100"], start="2026-05-01", end="2026-05-31")
    month = next(block for block in report["calendar"] if block["kind"] == "month")
    assert [day["date"] for day in month["days"]] == ["2026-05-08"]
    assert month["active_days"] == 1
    assert month["quiet_days"] == 30


def test_a_completed_pm_is_drawn_on_the_day_it_was_done(tmp_path):
    """The same rule the live page follows, so the two agree."""

    service = _seeded(tmp_path, rows=[
        _task("1", "100", "100 - M - Laser", "2026-05-08", "2026-06-16"),
    ])
    report = PmExportService(service).report(["100"], start="2026-05-01", end="2026-06-30")
    months = {block["key"]: block for block in report["calendar"] if block["kind"] == "month"}
    assert "2026-05" not in months
    assert [day["date"] for day in months["2026-06"]["days"]] == ["2026-06-16"]


def test_empty_months_are_collapsed_into_one_line(tmp_path):
    """Ten "no PM activity this month" blocks is padding, not information."""

    service = _seeded(tmp_path, rows=[
        _task("1", "100", "100 - M - Laser", "2026-01-12", "2026-01-12"),
        _task("2", "100", "100 - M - Laser", "2026-05-08", "2026-05-08"),
    ])
    report = PmExportService(service).report(["100"], start="2026-01-01", end="2026-05-31")
    kinds = [block["kind"] for block in report["calendar"]]
    assert kinds == ["month", "quiet", "month"]
    quiet = report["calendar"][1]
    assert quiet["count"] == 3
    assert quiet["label"] == "February 2026 to April 2026"


def test_a_single_quiet_month_is_named_once(tmp_path):
    service = _seeded(tmp_path, rows=[
        _task("1", "100", "100 - M - Laser", "2026-01-12", "2026-01-12"),
        _task("2", "100", "100 - M - Laser", "2026-03-08", "2026-03-08"),
    ])
    report = PmExportService(service).report(["100"], start="2026-01-01", end="2026-03-31")
    assert report["calendar"][1]["label"] == "February 2026"


# ----------------------------------------------------------------------
# What is coming
# ----------------------------------------------------------------------
def test_coming_up_gives_one_row_per_asset(tmp_path):
    report = _export(tmp_path).report(["100"], preset="last_12_months")
    assert len({row["asset_id"] for row in report["upcoming"]}) == len(report["upcoming"])


def test_coming_up_marks_an_estimate_as_an_estimate(tmp_path):
    """An asset whose next work order is not raised yet still has a next PM.

    Leaving it out would read as "nothing due", which is the opposite of
    what the data says.
    """

    service = _seeded(tmp_path, rows=[
        _task("1", "100", "100 - M - Laser", "2026-06-05", "2026-06-05"),
        _task("2", "100", "100 - M - Laser", "2026-07-03", "2026-07-03"),
        _task("3", "100", "100 - M - Laser", "2026-08-31", "2026-08-31"),
        _task("4", "100", "100 - M - Laser", "2026-09-28", "2026-09-28"),
    ])
    report = PmExportService(service).report(["100"], preset="last_6_months")
    assert report["upcoming"]
    assert report["upcoming"][0]["estimated"] is True


# ----------------------------------------------------------------------
# Nothing to report
# ----------------------------------------------------------------------
def test_an_empty_selection_still_produces_a_report(tmp_path):
    """Every asset can be unticked in the dialog; that is not an error."""

    report = _export(tmp_path).report([], preset="last_12_months")
    assert report["assets"] == []
    assert report["totals"]["due"] == 0
    assert report["rows"] == []
    assert report["window"]["label"]


def test_hiding_every_sub_asset_is_the_same_as_choosing_none(tmp_path):
    report = _export(tmp_path).report(["100"], exclude=["100"], preset="last_12_months")
    assert report["per_asset"] == []


# ----------------------------------------------------------------------
# The endpoint
# ----------------------------------------------------------------------
def test_the_export_needs_a_selection(monkeypatch, tmp_path):
    """Without one this would be a read of every PM on the account."""

    module = _app(monkeypatch, tmp_path)
    client = _signed_in(module)
    assert client.get("/pm-calendar/api/export").status_code == 400


def test_the_export_refuses_a_format_it_cannot_produce(monkeypatch, tmp_path):
    module = _app(monkeypatch, tmp_path)
    client = _signed_in(module)
    response = client.get("/pm-calendar/api/export?assets=100&format=docx")
    assert response.status_code == 400


def test_the_html_export_is_served_as_a_download(monkeypatch, tmp_path):
    """Attached to an email, not left at a URL nobody can find again."""

    _seeded(tmp_path)
    module = _app(monkeypatch, tmp_path)
    client = _signed_in(module)
    response = client.get(
        "/pm-calendar/api/export?assets=100&preset=last_12_months&format=html"
    )
    assert response.status_code == 200
    assert response.headers["Content-Type"].startswith("text/html")
    assert "attachment" in response.headers["Content-Disposition"]
    assert "PM_report_2025-10_to_2026-09.html" in response.headers["Content-Disposition"]


def test_the_html_export_stands_on_its_own(monkeypatch, tmp_path):
    """No script, no stylesheet, no font and no image fetched from anywhere.

    The file has to render the same from an email attachment on a machine
    with no network, and the PDF is printed from this string with no base
    URL to resolve anything against.
    """

    _seeded(tmp_path)
    module = _app(monkeypatch, tmp_path)
    client = _signed_in(module)
    body = client.get(
        "/pm-calendar/api/export?assets=100&preset=last_12_months&format=html"
    ).get_data(as_text=True)
    assert "<script" not in body
    assert "<link" not in body
    assert "<img" not in body
    assert "url(" not in body
    assert "100 - M - Laser" in body


def test_the_export_explains_itself_to_someone_who_has_not_seen_the_page(monkeypatch, tmp_path):
    """The audience has never used GREMLIN, so the terms are spelled out."""

    _seeded(tmp_path)
    module = _app(monkeypatch, tmp_path)
    client = _signed_in(module)
    body = client.get(
        "/pm-calendar/api/export?assets=100&preset=last_12_months&format=html"
    ).get_data(as_text=True)
    assert "How to read this" in body
    # Cadence codes are translated, never printed raw.
    assert "Semi-annual" in body or "Monthly" in body
    assert "Quarterly" in body


def test_a_missing_browser_is_reported_with_the_command_that_fixes_it(monkeypatch, tmp_path):
    """The normal way to get this wrong is pip without the browser download.

    A 503 naming the command is the difference between an administrator
    fixing it in a minute and a stack trace nobody sees.
    """

    _seeded(tmp_path)
    module = _app(monkeypatch, tmp_path)

    def _unavailable(_html, **_kwargs):
        raise html_to_pdf.PdfUnavailable("Chromium is not installed.")

    monkeypatch.setattr(module.html_to_pdf, "render", _unavailable)
    client = _signed_in(module)
    response = client.get("/pm-calendar/api/export?assets=100&preset=last_12_months")
    assert response.status_code == 503
    payload = response.get_json()
    assert "Chromium" in payload["error"]
    assert "playwright install chromium" in payload["hint"]


def test_the_pdf_is_the_html_printed(monkeypatch, tmp_path):
    """One layout, not two -- so what is checked is that the PDF *is* the page.

    The browser itself is not exercised here (it is a 150MB download that a
    test run should not depend on); what matters is that the bytes handed to
    the printer are the same bytes the HTML export serves.
    """

    _seeded(tmp_path)
    module = _app(monkeypatch, tmp_path)
    printed = {}

    def _capture(html, **kwargs):
        printed["html"] = html
        printed["kwargs"] = kwargs
        return b"%PDF-1.4 fake"

    monkeypatch.setattr(module.html_to_pdf, "render", _capture)
    client = _signed_in(module)
    query = "assets=100&preset=last_12_months"
    pdf = client.get(f"/pm-calendar/api/export?{query}")
    served = client.get(f"/pm-calendar/api/export?{query}&format=html").get_data(as_text=True)

    assert pdf.status_code == 200
    assert pdf.headers["Content-Type"] == "application/pdf"
    assert "PM_report_2025-10_to_2026-09.pdf" in pdf.headers["Content-Disposition"]
    assert printed["html"] == served


def test_the_pdf_is_the_default_format(monkeypatch, tmp_path):
    _seeded(tmp_path)
    module = _app(monkeypatch, tmp_path)
    monkeypatch.setattr(module.html_to_pdf, "render", lambda html, **kw: b"%PDF-1.4 fake")
    client = _signed_in(module)
    response = client.get("/pm-calendar/api/export?assets=100")
    assert response.headers["Content-Type"] == "application/pdf"


def test_the_export_is_offered_on_the_page(monkeypatch, tmp_path):
    module = _app(monkeypatch, tmp_path)
    client = _signed_in(module)
    body = client.get("/pm-task-tracker").get_data(as_text=True)
    assert 'id="pm-export-btn"' in body
    assert 'id="pm-export-modal"' in body
