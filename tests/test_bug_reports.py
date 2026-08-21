"""The bug reporting feature: the public form, the store, and the admin dashboard.

Every test points GREMLIN_BUGS_DB_PATH at a tmp_path. That is not only isolation:
the real default is a Windows path on the shared drive, and on a POSIX test
runner ``Path(r"Z:\\FACIL\\...")`` is a *relative* path, so a test that let the
default stand would create that name as a folder in the working tree.
"""

import importlib
import sqlite3

import pytest

from services.bug_reports import (
    DEFAULT_BUG_DB_PATH,
    BugReportStore,
    BugReportStoreError,
    BugReportValidationError,
)


def _app(monkeypatch, tmp_path, *, bugs_db=None):
    monkeypatch.setenv("GREMLIN_ACCESS_DB_PATH", str(tmp_path / "accesscontrol.db"))
    monkeypatch.setenv("GREMLIN_DB_PATH", str(tmp_path / "gremlin.db"))
    monkeypatch.setenv("GREMLIN_BUGS_DB_PATH", str(bugs_db or tmp_path / "bugreports.db"))
    monkeypatch.setenv("GREMLIN_ADMIN_USERNAME", "root")
    monkeypatch.setenv("GREMLIN_ADMIN_PIN", "secret")
    import app

    return importlib.reload(app)


def _signed_in(module, username="root", pin="secret"):
    client = module.app.test_client()
    with client.session_transaction() as session:
        session["user"] = module.access_control.authenticate(username, pin)
    return client


def _csrf(client, path="/report-a-bug"):
    """The token the form carries, which is how an anonymous visitor gets one."""

    body = client.get(path).get_data(as_text=True)
    marker = 'name="csrf_token" value="'
    start = body.index(marker) + len(marker)
    return body[start : body.index('"', start)]


def _file_one(client, **fields):
    payload = {
        "csrf_token": _csrf(client),
        "title": "Charts are blank",
        "description": "Opened Metrics and the charts never drew.",
        "area": "Metrics",
        "severity": "major",
        "reporter": "Sam",
    }
    payload.update(fields)
    return client.post("/report-a-bug", data=payload)


# ---------------------------------------------------------------------------
# The store
# ---------------------------------------------------------------------------


def test_the_database_and_its_folder_are_created_on_first_use(tmp_path):
    """Nothing exists at the location beforehand -- the first report makes it."""

    path = tmp_path / "GREMLIN Global DB" / "bugreports.db"
    store = BugReportStore(path)
    assert not path.exists()
    assert store.submit(title="First", description="Something broke.") == 1
    assert path.is_file()


def test_the_default_location_is_the_shared_drive():
    """The one place bugreports.db belongs, spelled as the deployment sees it.

    Asserted against the string rather than PurePath.name: GREMLIN is deployed on
    Windows, and on a POSIX test runner the whole thing is a single path segment
    because a backslash is not a separator here. The string is what gets opened.
    """

    written = str(DEFAULT_BUG_DB_PATH)
    assert written.startswith(r"Z:\FACIL\MAIN-ENG")
    assert written.endswith(r"GREMLIN Program\GREMLIN Global DB\bugreports.db")


def test_a_new_report_starts_open(tmp_path):
    store = BugReportStore(tmp_path / "bugreports.db")
    report_id = store.submit(title="Broken", description="It broke.")
    stored = store.list_reports()[0]
    assert stored["id"] == report_id
    assert stored["status"] == "open"
    assert stored["resolved_at"] is None


@pytest.mark.parametrize(
    "fields",
    [
        {"title": "", "description": "Something"},
        {"title": "   ", "description": "Something"},
        {"title": "Something", "description": ""},
        {"title": "Something", "description": "Detail", "severity": "catastrophic"},
    ],
)
def test_a_report_that_says_nothing_is_refused(tmp_path, fields):
    store = BugReportStore(tmp_path / "bugreports.db")
    with pytest.raises(BugReportValidationError):
        store.submit(**fields)


def test_long_fields_are_truncated_rather_than_refused(tmp_path):
    """The file is on a share other people's work depends on, so nothing it stores is unbounded."""

    store = BugReportStore(tmp_path / "bugreports.db")
    store.submit(title="T" * 5000, description="D" * 50_000, reporter="R" * 500)
    stored = store.list_reports()[0]
    assert len(stored["title"]) == 200
    assert len(stored["description"]) == 8000
    assert len(stored["reporter"]) == 128


def test_resolving_records_who_did_it_and_reopening_clears_that(tmp_path):
    store = BugReportStore(tmp_path / "bugreports.db")
    report_id = store.submit(title="Broken", description="It broke.")

    resolved = store.set_status(report_id, "resolved", actor="root", note="Fixed in 1.4.")
    assert resolved["status"] == "resolved"
    assert resolved["resolved_by"] == "root"
    assert resolved["resolved_at"]
    assert resolved["resolution_note"] == "Fixed in 1.4."

    reopened = store.set_status(report_id, "open", note="Came back.")
    assert reopened["status"] == "open"
    # A resolution that no longer holds must not still be described.
    assert reopened["resolved_at"] is None
    assert reopened["resolved_by"] == ""
    assert reopened["resolution_note"] == "Came back."


def test_an_impossible_status_or_a_missing_report_is_refused(tmp_path):
    store = BugReportStore(tmp_path / "bugreports.db")
    report_id = store.submit(title="Broken", description="It broke.")
    with pytest.raises(BugReportValidationError):
        store.set_status(report_id, "wontfix")
    with pytest.raises(BugReportValidationError):
        store.set_status(9999, "resolved")
    with pytest.raises(BugReportValidationError):
        store.delete(9999)


def test_open_reports_lead_and_the_most_urgent_leads_them(tmp_path):
    """The top of the list is the queue to work through, not a list to re-sort."""

    store = BugReportStore(tmp_path / "bugreports.db")
    minor = store.submit(title="Typo", description="Says 'teh'.", severity="minor")
    blocking = store.submit(title="Stuck", description="Cannot sign in.", severity="blocking")
    major = store.submit(title="Slow", description="Export takes minutes.", severity="major")
    store.set_status(blocking, "resolved", actor="root")

    # Resolved sinks even though it is the most urgent severity.
    assert [row["id"] for row in store.list_reports()] == [major, minor, blocking]
    assert [row["id"] for row in store.list_reports(status="open")] == [major, minor]
    assert [row["id"] for row in store.list_reports(status="resolved")] == [blocking]


def test_search_covers_the_fields_a_reader_would_search_by(tmp_path):
    store = BugReportStore(tmp_path / "bugreports.db")
    store.submit(title="Charts blank", description="No bars drew.", area="Metrics", reporter="Sam")
    store.submit(title="Typo", description="Says 'teh'.", area="About", reporter="Alex")

    assert len(store.list_reports(search="charts")) == 1
    assert len(store.list_reports(search="CHARTS")) == 1  # case-insensitive
    assert len(store.list_reports(search="bars drew")) == 1  # description
    assert len(store.list_reports(search="Metrics")) == 1  # area
    assert len(store.list_reports(search="Alex")) == 1  # reporter
    assert len(store.list_reports(search="")) == 2


def test_a_wildcard_in_a_search_is_searched_for_rather_than_obeyed(tmp_path):
    """"100%" finds the report that says so, not every report."""

    store = BugReportStore(tmp_path / "bugreports.db")
    store.submit(title="Reads 100% when idle", description="Gauge is wrong.")
    store.submit(title="Something else", description="Unrelated.")

    assert len(store.list_reports(search="100%")) == 1
    # A bare wildcard is a literal too: it finds the report that contains that
    # character, rather than matching everything the way an unescaped LIKE would.
    assert [row["title"] for row in store.list_reports(search="%")] == ["Reads 100% when idle"]
    assert store.list_reports(search="_") == []


def test_an_unusable_status_filter_is_refused(tmp_path):
    store = BugReportStore(tmp_path / "bugreports.db")
    with pytest.raises(BugReportValidationError):
        store.list_reports(status="pending")


def test_the_summary_counts_what_the_tiles_show(tmp_path):
    store = BugReportStore(tmp_path / "bugreports.db")
    assert store.summary() == {
        "total": 0,
        "open": 0,
        "resolved": 0,
        "open_blocking": 0,
        "latest_report": None,
    }

    store.submit(title="Stuck", description="Cannot sign in.", severity="blocking")
    second = store.submit(title="Typo", description="Says 'teh'.", severity="minor")
    store.set_status(second, "resolved", actor="root")

    summary = store.summary()
    assert (summary["total"], summary["open"], summary["resolved"]) == (2, 1, 1)
    assert summary["open_blocking"] == 1
    assert summary["latest_report"]


def test_a_resolved_blocking_report_stops_counting_as_blocking(tmp_path):
    store = BugReportStore(tmp_path / "bugreports.db")
    report_id = store.submit(title="Stuck", description="Cannot sign in.", severity="blocking")
    assert store.summary()["open_blocking"] == 1
    store.set_status(report_id, "resolved", actor="root")
    assert store.summary()["open_blocking"] == 0


def test_an_unreachable_share_is_reported_rather_than_raised_as_sqlite(tmp_path):
    """A laptop off the network is the expected failure, and it names the path."""

    store = BugReportStore("/proc/nowhere/bugreports.db")
    for call in (store.summary, store.list_reports, lambda: store.submit(title="a", description="b")):
        with pytest.raises(BugReportStoreError) as caught:
            call()
        assert "/proc/nowhere/bugreports.db" in str(caught.value)
        assert not isinstance(caught.value, sqlite3.Error)


def test_a_share_that_comes_back_is_used_without_a_restart(tmp_path):
    """ensure_schema caches success only, so a failed attempt is retried."""

    path = tmp_path / "share" / "bugreports.db"
    store = BugReportStore(path)
    (tmp_path / "share").write_text("not a directory")  # stands in for the share being down
    with pytest.raises(BugReportStoreError):
        store.submit(title="Broken", description="It broke.")

    (tmp_path / "share").unlink()
    assert store.submit(title="Broken", description="It broke.") == 1


# ---------------------------------------------------------------------------
# The public form
# ---------------------------------------------------------------------------


def test_anyone_can_reach_and_file_from_the_form(monkeypatch, tmp_path):
    """No account: the people most likely to hit a bug are the ones who cannot get in."""

    module = _app(monkeypatch, tmp_path)
    client = module.app.test_client()
    assert client.get("/report-a-bug").status_code == 200

    response = _file_one(client)
    # Redirect rather than render, so refreshing cannot file the same report twice.
    assert response.status_code == 302
    assert response.headers["Location"] == "/report-a-bug?submitted=1"

    stored = module.bug_reports.list_reports()[0]
    assert stored["title"] == "Charts are blank"
    assert stored["reporter"] == "Sam"
    assert stored["status"] == "open"
    assert stored["reporter_user_id"] is None


def test_the_confirmation_gives_the_reporter_their_number(monkeypatch, tmp_path):
    module = _app(monkeypatch, tmp_path)
    client = module.app.test_client()
    location = _file_one(client).headers["Location"]
    body = client.get(location).get_data(as_text=True)
    assert "#1" in body
    # The form is gone; there is nothing to resubmit.
    assert 'name="description"' not in body


def test_a_report_filed_while_signed_in_says_who_filed_it(monkeypatch, tmp_path):
    module = _app(monkeypatch, tmp_path)
    client = _signed_in(module)
    # The name is prefilled rather than asked for.
    assert 'value="root"' in client.get("/report-a-bug").get_data(as_text=True)

    _file_one(client, reporter="root")
    stored = module.bug_reports.list_reports()[0]
    assert stored["reporter"] == "root"
    assert stored["reporter_user_id"] == module.access_control.authenticate("root", "secret")["id"]


def test_an_incomplete_report_keeps_what_was_typed(monkeypatch, tmp_path):
    """Losing a paragraph of description to a missing title is how a reporter gives up."""

    module = _app(monkeypatch, tmp_path)
    client = module.app.test_client()
    response = _file_one(client, title="", description="Three paragraphs of detail.")
    assert response.status_code == 400
    body = response.get_data(as_text=True)
    assert "Three paragraphs of detail." in body
    assert "Give the report a short title." in body
    assert module.bug_reports.list_reports() == []


def test_a_post_from_another_site_is_refused(monkeypatch, tmp_path):
    module = _app(monkeypatch, tmp_path)
    client = module.app.test_client()
    response = client.post("/report-a-bug", data={"title": "x", "description": "y"})
    assert response.status_code == 403
    assert module.bug_reports.list_reports() == []


def test_the_form_offers_the_pages_the_sidebar_does(monkeypatch, tmp_path):
    """So a report arrives sorted by where it happened, against one list of areas."""

    module = _app(monkeypatch, tmp_path)
    body = module.app.test_client().get("/report-a-bug").get_data(as_text=True)
    for link in module.NAV_LINKS:
        assert f'value="{link["label"]}"' in body, link["label"]


def test_an_unreachable_share_tells_the_reporter_the_report_was_not_stored(monkeypatch, tmp_path):
    module = _app(monkeypatch, tmp_path, bugs_db="/proc/nowhere/bugreports.db")
    client = module.app.test_client()
    response = _file_one(client, description="Worth keeping.")
    # 503, not 500: the code is fine and the drive is not.
    assert response.status_code == 503
    body = response.get_data(as_text=True)
    assert "could not be opened" in body
    assert "Worth keeping." in body


def test_gremlin_still_starts_when_the_share_is_unreachable(monkeypatch, tmp_path):
    """Nothing in the store runs at import, so a drive that is not mapped is not fatal."""

    module = _app(monkeypatch, tmp_path, bugs_db="/proc/nowhere/bugreports.db")
    assert module.app.test_client().get("/").status_code == 200
    assert module.app.test_client().get("/report-a-bug").status_code == 200


# ---------------------------------------------------------------------------
# The developer dashboard
# ---------------------------------------------------------------------------


def test_the_dashboard_is_admin_only(monkeypatch, tmp_path):
    module = _app(monkeypatch, tmp_path)
    anonymous = module.app.test_client()
    assert anonymous.get("/developer/bugs").status_code == 403
    assert anonymous.get("/developer/api/bugs").status_code == 403
    assert anonymous.post("/developer/api/bugs/1/status", json={"status": "resolved"}).status_code == 403

    module.access_control.save_user(None, "operator", "2468", "editor")
    editor = _signed_in(module, "operator", "2468")
    assert editor.get("/developer/bugs").status_code == 403
    assert editor.get("/developer/api/bugs").status_code == 403


def test_the_dashboard_renders_and_every_developer_page_carries_the_tab(monkeypatch, tmp_path):
    module = _app(monkeypatch, tmp_path)
    response = _signed_in(module).get("/developer/bugs")
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "Bug reports" in body
    # The page says which file it is reading, as the other developer pages do.
    assert str(tmp_path / "bugreports.db") in body

    for page in ("/developer", "/developer/database", "/developer/activity", "/developer/access"):
        assert "/developer/bugs" in _signed_in(module).get(page).get_data(as_text=True), page


def test_the_endpoint_answers_the_whole_page(monkeypatch, tmp_path):
    module = _app(monkeypatch, tmp_path)
    _file_one(module.app.test_client(), title="Charts blank", severity="blocking")
    payload = _signed_in(module).get("/developer/api/bugs").get_json()

    assert payload["summary"]["open"] == 1
    assert payload["summary"]["open_blocking"] == 1
    assert payload["reports"][0]["title"] == "Charts blank"
    assert payload["bugs_db_path"] == str(tmp_path / "bugreports.db")
    assert payload["statuses"] == ["open", "resolved"]


def test_the_dashboard_filters_and_searches_on_the_server(monkeypatch, tmp_path):
    module = _app(monkeypatch, tmp_path)
    module.bug_reports.submit(title="Charts blank", description="No bars.", severity="major")
    resolved = module.bug_reports.submit(title="Typo", description="Says 'teh'.")
    module.bug_reports.set_status(resolved, "resolved", actor="root")
    admin = _signed_in(module)

    assert len(admin.get("/developer/api/bugs?status=open").get_json()["reports"]) == 1
    assert len(admin.get("/developer/api/bugs?status=resolved").get_json()["reports"]) == 1
    assert len(admin.get("/developer/api/bugs?status=all").get_json()["reports"]) == 2
    assert len(admin.get("/developer/api/bugs?search=charts").get_json()["reports"]) == 1
    assert admin.get("/developer/api/bugs?status=pending").status_code == 400


def test_an_administrator_can_resolve_and_reopen(monkeypatch, tmp_path):
    module = _app(monkeypatch, tmp_path)
    module.bug_reports.submit(title="Charts blank", description="No bars.")
    admin = _signed_in(module)

    resolved = admin.post("/developer/api/bugs/1/status", json={"status": "resolved", "note": "Fixed."})
    assert resolved.status_code == 200
    assert resolved.get_json()["report"]["status"] == "resolved"
    # Who resolved it comes from the session, not the request body.
    assert resolved.get_json()["report"]["resolved_by"] == "root"
    assert admin.get("/developer/api/bugs").get_json()["summary"]["resolved"] == 1

    reopened = admin.post("/developer/api/bugs/1/status", json={"status": "open", "note": "Came back."})
    assert reopened.get_json()["report"]["status"] == "open"
    assert reopened.get_json()["report"]["resolved_at"] is None
    assert admin.get("/developer/api/bugs").get_json()["summary"]["open"] == 1


def test_a_report_can_be_deleted(monkeypatch, tmp_path):
    module = _app(monkeypatch, tmp_path)
    module.bug_reports.submit(title="Duplicate", description="Filed twice.")
    admin = _signed_in(module)
    assert admin.post("/developer/api/bugs/1/delete", json={}).status_code == 200
    assert admin.get("/developer/api/bugs").get_json()["summary"]["total"] == 0


def test_a_cross_site_form_cannot_resolve_a_report(monkeypatch, tmp_path):
    """A form POST cannot set this content type without a preflight the browser refuses."""

    module = _app(monkeypatch, tmp_path)
    module.bug_reports.submit(title="Charts blank", description="No bars.")
    admin = _signed_in(module)

    assert admin.post("/developer/api/bugs/1/status", data={"status": "resolved"}).status_code == 415
    assert admin.post("/developer/api/bugs/1/delete", data={}).status_code == 415
    assert module.bug_reports.list_reports()[0]["status"] == "open"


@pytest.mark.parametrize("body", [{"status": "wontfix"}, {"status": ""}, {}])
def test_an_impossible_status_change_is_a_bad_request(monkeypatch, tmp_path, body):
    module = _app(monkeypatch, tmp_path)
    module.bug_reports.submit(title="Charts blank", description="No bars.")
    admin = _signed_in(module)
    assert admin.post("/developer/api/bugs/1/status", json=body).status_code == 400
    assert module.bug_reports.list_reports()[0]["status"] == "open"


def test_changing_a_report_lands_in_the_audit_trail_but_reading_does_not(monkeypatch, tmp_path):
    """Who resolved what is answerable later, and opening the page is not a change."""

    module = _app(monkeypatch, tmp_path)
    module.bug_reports.submit(title="Charts blank", description="No bars.")
    admin = _signed_in(module)

    admin.get("/developer/bugs")
    admin.get("/developer/api/bugs")
    assert admin.get("/developer/api/activity").get_json()["summary"]["changes"] == 0

    admin.post("/developer/api/bugs/1/status", json={"status": "resolved"})
    activity = admin.get("/developer/api/activity").get_json()
    assert activity["summary"]["changes"] == 1
    assert activity["recent_changes"][0]["action"] == "POST /developer/api/bugs/1/status"


def test_the_dashboard_reports_an_unreachable_share_rather_than_failing(monkeypatch, tmp_path):
    module = _app(monkeypatch, tmp_path, bugs_db="/proc/nowhere/bugreports.db")
    admin = _signed_in(module)
    # The page itself still draws -- it is the fetch behind it that cannot.
    assert admin.get("/developer/bugs").status_code == 200
    response = admin.get("/developer/api/bugs")
    assert response.status_code == 503
    assert "/proc/nowhere/bugreports.db" in response.get_json()["error"]


def test_the_dashboard_is_findable_from_the_global_search(monkeypatch, tmp_path):
    module = _app(monkeypatch, tmp_path)
    entries = module._search_index(is_admin=True, can_edit=True, has_account=True)
    assert any(entry["url"] == "/developer/bugs" for entry in entries)
    # Not offered to somebody who could not open it.
    viewer = module._search_index(is_admin=False, can_edit=False, has_account=True)
    assert all(entry["url"] != "/developer/bugs" for entry in viewer)
