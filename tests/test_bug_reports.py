"""The bug reporting feature: the public form, the store, and the admin dashboard.

Every test points GREMLIN_BUGS_DB_PATH at a tmp_path. That is not only isolation:
the real default is a Windows path on the shared drive, and on a POSIX test
runner ``Path(r"Z:\\FACIL\\...")`` is a *relative* path, so a test that let the
default stand would create that name as a folder in the working tree.
"""

import importlib
import sqlite3
from pathlib import PureWindowsPath

import pytest

from services.bug_reports import (
    DEFAULT_BUG_DB_PATH,
    LIST_LIMIT,
    SCHEMA_TABLES,
    SEVERITIES,
    SUBMISSION_LIMIT_PER_WINDOW,
    BugReportConflictError,
    BugReportRateLimitError,
    BugReportStore,
    BugReportStoreError,
    BugReportValidationError,
    _sqlite_file_uri,
)


def _app(monkeypatch, tmp_path, *, bugs_db=None):
    monkeypatch.setenv("GREMLIN_ACCESS_DB_PATH", str(tmp_path / "accesscontrol.db"))
    monkeypatch.setenv("GREMLIN_DB_PATH", str(tmp_path / "gremlin.db"))
    monkeypatch.setenv("GREMLIN_BUGS_DB_PATH", str(bugs_db or tmp_path / "auxillary.db"))
    monkeypatch.setenv("GREMLIN_ADMIN_USERNAME", "root")
    monkeypatch.setenv("GREMLIN_ADMIN_PIN", "secret")
    import app

    return importlib.reload(app)


def _reports(store, **filters):
    """Just the rows of one page -- list_reports returns the page around them."""

    return store.list_reports(**filters)["reports"]


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

    path = tmp_path / "GREMLIN Global DB" / "auxillary.db"
    store = BugReportStore(path)
    assert not path.exists()
    assert store.submit(title="First", description="Something broke.") == 1
    assert path.is_file()


def test_the_default_location_is_the_shared_drive():
    """The one place auxillary.db belongs, spelled as the deployment sees it.

    Asserted against the string rather than PurePath.name: GREMLIN is deployed on
    Windows, and on a POSIX test runner the whole thing is a single path segment
    because a backslash is not a separator here. The string is what gets opened.
    """

    written = str(DEFAULT_BUG_DB_PATH)
    assert written.startswith(r"Z:\FACIL\MAIN-ENG")
    assert written.endswith(r"GREMLIN Program\GREMLIN Global DB\auxillary.db")


def test_a_new_report_starts_open(tmp_path):
    store = BugReportStore(tmp_path / "auxillary.db")
    report_id = store.submit(title="Broken", description="It broke.")
    stored = _reports(store)[0]
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
    store = BugReportStore(tmp_path / "auxillary.db")
    with pytest.raises(BugReportValidationError):
        store.submit(**fields)


def test_long_fields_are_truncated_rather_than_refused(tmp_path):
    """The file is on a share other people's work depends on, so nothing it stores is unbounded."""

    store = BugReportStore(tmp_path / "auxillary.db")
    store.submit(title="T" * 5000, description="D" * 50_000, reporter="R" * 500)
    stored = _reports(store)[0]
    assert len(stored["title"]) == 200
    assert len(stored["description"]) == 8000
    assert len(stored["reporter"]) == 128


def test_resolving_records_who_did_it_and_reopening_clears_that(tmp_path):
    store = BugReportStore(tmp_path / "auxillary.db")
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
    store = BugReportStore(tmp_path / "auxillary.db")
    report_id = store.submit(title="Broken", description="It broke.")
    with pytest.raises(BugReportValidationError):
        store.set_status(report_id, "wontfix")
    with pytest.raises(BugReportValidationError):
        store.set_status(9999, "resolved")
    with pytest.raises(BugReportValidationError):
        store.delete(9999)


def test_open_reports_lead_and_the_most_urgent_leads_them(tmp_path):
    """The top of the list is the queue to work through, not a list to re-sort."""

    store = BugReportStore(tmp_path / "auxillary.db")
    minor = store.submit(title="Typo", description="Says 'teh'.", severity="minor")
    blocking = store.submit(title="Stuck", description="Cannot sign in.", severity="blocking")
    major = store.submit(title="Slow", description="Export takes minutes.", severity="major")
    store.set_status(blocking, "resolved", actor="root")

    # Resolved sinks even though it is the most urgent severity.
    assert [row["id"] for row in _reports(store)] == [major, minor, blocking]
    assert [row["id"] for row in _reports(store, status="open")] == [major, minor]
    assert [row["id"] for row in _reports(store, status="resolved")] == [blocking]


def test_search_covers_the_fields_a_reader_would_search_by(tmp_path):
    store = BugReportStore(tmp_path / "auxillary.db")
    store.submit(title="Charts blank", description="No bars drew.", area="Metrics", reporter="Sam")
    store.submit(title="Typo", description="Says 'teh'.", area="About", reporter="Alex")

    assert len(_reports(store, search="charts")) == 1
    assert len(_reports(store, search="CHARTS")) == 1  # case-insensitive
    assert len(_reports(store, search="bars drew")) == 1  # description
    assert len(_reports(store, search="Metrics")) == 1  # area
    assert len(_reports(store, search="Alex")) == 1  # reporter
    assert len(_reports(store, search="")) == 2


def test_a_wildcard_in_a_search_is_searched_for_rather_than_obeyed(tmp_path):
    """"100%" finds the report that says so, not every report."""

    store = BugReportStore(tmp_path / "auxillary.db")
    store.submit(title="Reads 100% when idle", description="Gauge is wrong.")
    store.submit(title="Something else", description="Unrelated.")

    assert len(_reports(store, search="100%")) == 1
    # A bare wildcard is a literal too: it finds the report that contains that
    # character, rather than matching everything the way an unescaped LIKE would.
    assert [row["title"] for row in _reports(store, search="%")] == ["Reads 100% when idle"]
    assert _reports(store, search="_") == []


def test_an_unusable_status_filter_is_refused(tmp_path):
    store = BugReportStore(tmp_path / "auxillary.db")
    with pytest.raises(BugReportValidationError):
        _reports(store, status="pending")


def test_the_summary_counts_what_the_tiles_show(tmp_path):
    store = BugReportStore(tmp_path / "auxillary.db")
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
    store = BugReportStore(tmp_path / "auxillary.db")
    report_id = store.submit(title="Stuck", description="Cannot sign in.", severity="blocking")
    assert store.summary()["open_blocking"] == 1
    store.set_status(report_id, "resolved", actor="root")
    assert store.summary()["open_blocking"] == 0


def test_an_unreachable_share_is_reported_rather_than_raised_as_sqlite(tmp_path):
    """A laptop off the network is the expected failure, and it names the path."""

    store = BugReportStore("/proc/nowhere/auxillary.db")
    for call in (store.summary, store.list_reports, lambda: store.submit(title="a", description="b")):
        with pytest.raises(BugReportStoreError) as caught:
            call()
        assert "/proc/nowhere/auxillary.db" in str(caught.value)
        assert not isinstance(caught.value, sqlite3.Error)


def test_a_share_that_comes_back_is_used_without_a_restart(tmp_path):
    """ensure_schema caches success only, so a failed attempt is retried."""

    path = tmp_path / "share" / "auxillary.db"
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

    stored = _reports(module.bug_reports)[0]
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
    stored = _reports(module.bug_reports)[0]
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
    assert _reports(module.bug_reports) == []


def test_a_post_from_another_site_is_refused(monkeypatch, tmp_path):
    module = _app(monkeypatch, tmp_path)
    client = module.app.test_client()
    response = client.post("/report-a-bug", data={"title": "x", "description": "y"})
    assert response.status_code == 403
    assert _reports(module.bug_reports) == []


def test_the_form_offers_the_pages_the_sidebar_does(monkeypatch, tmp_path):
    """So a report arrives sorted by where it happened, against one list of areas."""

    module = _app(monkeypatch, tmp_path)
    body = module.app.test_client().get("/report-a-bug").get_data(as_text=True)
    for link in module.NAV_LINKS:
        assert f'value="{link["label"]}"' in body, link["label"]


def test_an_unreachable_share_tells_the_reporter_the_report_was_not_stored(monkeypatch, tmp_path):
    module = _app(monkeypatch, tmp_path, bugs_db="/proc/nowhere/auxillary.db")
    client = module.app.test_client()
    response = _file_one(client, description="Worth keeping.")
    # 503, not 500: the code is fine and the drive is not.
    assert response.status_code == 503
    body = response.get_data(as_text=True)
    assert "could not be opened" in body
    assert "Worth keeping." in body


def test_gremlin_still_starts_when_the_share_is_unreachable(monkeypatch, tmp_path):
    """Nothing in the store runs at import, so a drive that is not mapped is not fatal."""

    module = _app(monkeypatch, tmp_path, bugs_db="/proc/nowhere/auxillary.db")
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
    assert str(tmp_path / "auxillary.db") in body

    for page in ("/developer", "/developer/database", "/developer/activity", "/developer/access"):
        assert "/developer/bugs" in _signed_in(module).get(page).get_data(as_text=True), page


def test_the_endpoint_answers_the_whole_page(monkeypatch, tmp_path):
    module = _app(monkeypatch, tmp_path)
    _file_one(module.app.test_client(), title="Charts blank", severity="blocking")
    payload = _signed_in(module).get("/developer/api/bugs").get_json()

    assert payload["summary"]["open"] == 1
    assert payload["summary"]["open_blocking"] == 1
    assert payload["reports"][0]["title"] == "Charts blank"
    assert payload["bugs_db_path"] == str(tmp_path / "auxillary.db")
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
    assert _reports(module.bug_reports)[0]["status"] == "open"


@pytest.mark.parametrize("body", [{"status": "wontfix"}, {"status": ""}, {}])
def test_an_impossible_status_change_is_a_bad_request(monkeypatch, tmp_path, body):
    module = _app(monkeypatch, tmp_path)
    module.bug_reports.submit(title="Charts blank", description="No bars.")
    admin = _signed_in(module)
    assert admin.post("/developer/api/bugs/1/status", json=body).status_code == 400
    assert _reports(module.bug_reports)[0]["status"] == "open"


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
    module = _app(monkeypatch, tmp_path, bugs_db="/proc/nowhere/auxillary.db")
    admin = _signed_in(module)
    # The page itself still draws -- it is the fetch behind it that cannot.
    assert admin.get("/developer/bugs").status_code == 200
    response = admin.get("/developer/api/bugs")
    assert response.status_code == 503
    assert "/proc/nowhere/auxillary.db" in response.get_json()["error"]


def test_the_dashboard_is_findable_from_the_global_search(monkeypatch, tmp_path):
    module = _app(monkeypatch, tmp_path)
    entries = module._search_index(is_admin=True, can_edit=True, has_account=True)
    assert any(entry["url"] == "/developer/bugs" for entry in entries)
    # Not offered to somebody who could not open it.
    viewer = module._search_index(is_admin=False, can_edit=False, has_account=True)
    assert all(entry["url"] != "/developer/bugs" for entry in viewer)


# ---------------------------------------------------------------------------
# Bounding the public write endpoint, and paging past the first screenful.
# These cover the review findings on the first revision of this feature.
# ---------------------------------------------------------------------------


def test_one_client_cannot_file_without_limit(tmp_path):
    """An unbounded public write would fill a shared drive and bury real reports."""

    store = BugReportStore(tmp_path / "auxillary.db")
    for index in range(SUBMISSION_LIMIT_PER_WINDOW):
        store.submit(title=f"Report {index}", description="Detail.", client_key="addr:10.0.0.1")

    with pytest.raises(BugReportRateLimitError) as caught:
        store.submit(title="One too many", description="Detail.", client_key="addr:10.0.0.1")
    assert caught.value.retry_after > 0
    assert len(_reports(store)) == SUBMISSION_LIMIT_PER_WINDOW


def test_the_limit_is_per_client_rather_than_global(tmp_path):
    """One runaway client must not stop everybody else reporting."""

    store = BugReportStore(tmp_path / "auxillary.db")
    for index in range(SUBMISSION_LIMIT_PER_WINDOW):
        store.submit(title=f"Report {index}", description="Detail.", client_key="addr:10.0.0.1")

    assert store.submit(title="Somebody else", description="Detail.", client_key="addr:10.0.0.2")
    assert store.submit(title="A signed-in reporter", description="Detail.", client_key="user:7")


def test_a_refused_submission_stores_nothing_and_costs_nothing(tmp_path):
    """The charge and the insert are one step, so neither happens without the other."""

    store = BugReportStore(tmp_path / "auxillary.db")
    for index in range(SUBMISSION_LIMIT_PER_WINDOW):
        store.submit(title=f"Report {index}", description="Detail.", client_key="addr:10.0.0.1")
    before = store.summary()["total"]

    for _ in range(3):
        with pytest.raises(BugReportRateLimitError):
            store.submit(title="Refused", description="Detail.", client_key="addr:10.0.0.1")
    assert store.summary()["total"] == before


def test_an_internal_caller_is_not_limited(tmp_path):
    """No client key means no limit -- the limit is a property of the public form."""

    store = BugReportStore(tmp_path / "auxillary.db")
    for index in range(SUBMISSION_LIMIT_PER_WINDOW + 5):
        store.submit(title=f"Report {index}", description="Detail.")
    assert store.summary()["total"] == SUBMISSION_LIMIT_PER_WINDOW + 5


def test_the_form_refuses_a_flood_and_keeps_what_was_typed(monkeypatch, tmp_path):
    module = _app(monkeypatch, tmp_path)
    client = module.app.test_client()
    for index in range(SUBMISSION_LIMIT_PER_WINDOW):
        assert _file_one(client, title=f"Report {index}").status_code == 302

    response = _file_one(client, title="One too many", description="Still worth keeping.")
    assert response.status_code == 429
    assert response.headers["Retry-After"]
    body = response.get_data(as_text=True)
    assert "Still worth keeping." in body
    assert "try again in about" in body


def test_a_listing_says_how_much_it_is_not_showing(tmp_path):
    """Stopping silently at the page size would hide the oldest reports for good."""

    store = BugReportStore(tmp_path / "auxillary.db")
    for index in range(LIST_LIMIT + 15):
        store.submit(title=f"Report {index}", description="Detail.")

    first = store.list_reports()
    assert len(first["reports"]) == LIST_LIMIT
    assert first["total"] == LIST_LIMIT + 15
    assert first["has_more"] is True
    assert first["next_cursor"]


def test_paging_reaches_every_report_exactly_once(tmp_path):
    store = BugReportStore(tmp_path / "auxillary.db")
    filed = [
        store.submit(title=f"Report {index}", description="Detail.")
        for index in range(LIST_LIMIT + 15)
    ]

    seen, cursor = [], None
    while True:
        page = store.list_reports(cursor=cursor)
        seen.extend(row["id"] for row in page["reports"])
        if not page["has_more"]:
            assert page["next_cursor"] is None
            break
        cursor = page["next_cursor"]

    assert sorted(seen) == sorted(filed)
    assert len(seen) == len(set(seen))  # no report served twice


def test_the_page_size_cannot_be_talked_past(tmp_path):
    store = BugReportStore(tmp_path / "auxillary.db")
    store.submit(title="Only one", description="Detail.")
    assert len(store.list_reports(limit=10_000)["reports"]) == 1
    assert store.list_reports(limit=0)["total"] == 1


def test_the_dashboard_pages_and_reports_the_total(monkeypatch, tmp_path):
    module = _app(monkeypatch, tmp_path)
    for index in range(LIST_LIMIT + 5):
        module.bug_reports.submit(title=f"Report {index}", description="Detail.")
    admin = _signed_in(module)

    first = admin.get("/developer/api/bugs").get_json()
    assert len(first["reports"]) == LIST_LIMIT
    assert first["total_matching"] == LIST_LIMIT + 5
    assert first["has_more"] is True

    rest = admin.get(f"/developer/api/bugs?cursor={first['next_cursor']}").get_json()
    assert len(rest["reports"]) == 5
    assert rest["has_more"] is False
    # The two pages together are the whole set, with nothing served twice.
    ids = [row["id"] for row in first["reports"]] + [row["id"] for row in rest["reports"]]
    assert len(set(ids)) == LIST_LIMIT + 5


@pytest.mark.parametrize("cursor", ["not-base64", "bm90LWEtY3Vyc29y", "a|b|c|d"])
def test_an_unusable_cursor_is_a_bad_request(monkeypatch, tmp_path, cursor):
    module = _app(monkeypatch, tmp_path)
    admin = _signed_in(module)
    assert admin.get(f"/developer/api/bugs?cursor={cursor}").status_code == 400


def test_a_stale_resolution_does_not_overwrite_the_real_one(tmp_path):
    """Two administrators, one queue: the second click must not erase the first."""

    store = BugReportStore(tmp_path / "auxillary.db")
    report_id = store.submit(title="Charts blank", description="Detail.")

    drawn = _reports(store)[0]["revision"]
    store.set_status(report_id, "resolved", actor="root", note="Fixed in 1.4.",
                     expected_revision=drawn)
    # The second administrator's page still showed the report as it was drawn.
    with pytest.raises(BugReportConflictError) as caught:
        store.set_status(report_id, "resolved", actor="other", note="Duplicate.",
                         expected_revision=drawn)
    assert caught.value.actual_status == "resolved"

    stored = _reports(store)[0]
    assert stored["resolved_by"] == "root"
    assert stored["resolution_note"] == "Fixed in 1.4."


def test_a_change_from_the_state_actually_stored_still_applies(tmp_path):
    store = BugReportStore(tmp_path / "auxillary.db")
    report_id = store.submit(title="Charts blank", description="Detail.")
    drawn = _reports(store)[0]["revision"]
    resolved = store.set_status(report_id, "resolved", actor="root", expected_revision=drawn)
    # Reopening from the revision the page really saw, so it applies.
    reopened = store.set_status(report_id, "open", note="Came back.",
                                expected_revision=resolved["revision"])
    assert reopened["status"] == "open"


def test_omitting_the_expected_revision_still_applies_unconditionally(tmp_path):
    """Internal callers and the tests do not have a page whose state could be stale."""

    store = BugReportStore(tmp_path / "auxillary.db")
    report_id = store.submit(title="Charts blank", description="Detail.")
    store.set_status(report_id, "resolved", actor="root")
    assert store.set_status(report_id, "resolved", actor="other")["resolved_by"] == "other"


def test_the_dashboard_answers_a_stale_change_with_a_conflict(monkeypatch, tmp_path):
    module = _app(monkeypatch, tmp_path)
    module.bug_reports.submit(title="Charts blank", description="Detail.")
    admin = _signed_in(module)

    drawn = admin.get("/developer/api/bugs").get_json()["reports"][0]["revision"]
    assert admin.post(
        "/developer/api/bugs/1/status",
        json={"status": "resolved", "note": "Fixed.", "expected_revision": drawn},
    ).status_code == 200

    stale = admin.post(
        "/developer/api/bugs/1/status",
        json={"status": "resolved", "note": "Duplicate.", "expected_revision": drawn},
    )
    assert stale.status_code == 409
    assert stale.get_json()["actual_status"] == "resolved"
    # The first administrator's note survived the second one's click.
    assert _reports(module.bug_reports)[0]["resolution_note"] == "Fixed."


def test_the_documented_override_is_readable_from_a_dotenv_file():
    """Advertising GREMLIN_BUGS_DB_PATH means the .env loader has to accept it."""

    from services.sync_service import APP_ENV_KEYS

    assert "GREMLIN_BUGS_DB_PATH" in APP_ENV_KEYS


def test_the_page_a_bug_was_filed_from_survives_a_correction(monkeypatch, tmp_path):
    """The browser cannot recover it: on a re-render the referrer is this form."""

    module = _app(monkeypatch, tmp_path)
    client = module.app.test_client()
    rejected = _file_one(client, title="", page_url="http://gremlin/metrics")
    assert rejected.status_code == 400
    assert 'value="http://gremlin/metrics"' in rejected.get_data(as_text=True)

    # The corrected retry files against the page that actually broke.
    _file_one(client, title="Charts blank", page_url="http://gremlin/metrics")
    assert _reports(module.bug_reports)[0]["page_url"] == "http://gremlin/metrics"


# ---------------------------------------------------------------------------
# The second review round: a status alone cannot detect resolve-then-reopen,
# and an offset cannot page a queue that is being worked while it is read.
# ---------------------------------------------------------------------------


def test_a_resolve_reopen_cycle_does_not_let_a_stale_click_through(tmp_path):
    """The ABA case: the status ends up where A left it, but the row moved twice."""

    store = BugReportStore(tmp_path / "auxillary.db")
    report_id = store.submit(title="Charts blank", description="Detail.")
    a_drew = _reports(store)[0]["revision"]

    resolved = store.set_status(report_id, "resolved", actor="B", note="B resolved it.",
                                expected_revision=a_drew)
    store.set_status(report_id, "open", actor="B", note="B reopened: still broken.",
                     expected_revision=resolved["revision"])

    # A's page is two changes out of date, but the status reads "open" again --
    # exactly what A saw. Only the revision can tell that apart from no change.
    with pytest.raises(BugReportConflictError):
        store.set_status(report_id, "resolved", actor="A", note="A's stale note.",
                         expected_revision=a_drew)

    stored = _reports(store)[0]
    assert stored["status"] == "open"
    assert stored["resolution_note"] == "B reopened: still broken."


def test_every_change_moves_the_revision_forward(tmp_path):
    store = BugReportStore(tmp_path / "auxillary.db")
    report_id = store.submit(title="Charts blank", description="Detail.")

    revisions = [_reports(store)[0]["revision"]]
    for status in ("resolved", "open", "resolved"):
        revisions.append(store.set_status(report_id, status, actor="root")["revision"])
    assert revisions == sorted(set(revisions))  # strictly increasing, never reused


def test_the_dashboard_refuses_a_stale_click_after_a_reopen(monkeypatch, tmp_path):
    module = _app(monkeypatch, tmp_path)
    module.bug_reports.submit(title="Charts blank", description="Detail.")
    admin = _signed_in(module)
    a_drew = admin.get("/developer/api/bugs").get_json()["reports"][0]["revision"]

    module.bug_reports.set_status(1, "resolved", actor="B", note="B resolved it.")
    module.bug_reports.set_status(1, "open", actor="B", note="B reopened: still broken.")

    stale = admin.post(
        "/developer/api/bugs/1/status",
        json={"status": "resolved", "note": "A's stale note.", "expected_revision": a_drew},
    )
    assert stale.status_code == 409
    assert _reports(module.bug_reports)[0]["resolution_note"] == "B reopened: still broken."


def test_a_malformed_revision_is_refused_rather_than_ignored(monkeypatch, tmp_path):
    """Dropping the guard on bad input is the one outcome that loses a resolution."""

    module = _app(monkeypatch, tmp_path)
    module.bug_reports.submit(title="Charts blank", description="Detail.")
    admin = _signed_in(module)

    response = admin.post(
        "/developer/api/bugs/1/status",
        json={"status": "resolved", "expected_revision": "not-a-number"},
    )
    assert response.status_code == 400
    assert _reports(module.bug_reports)[0]["status"] == "open"


def test_paging_skips_nothing_when_the_queue_is_worked_underneath_it(tmp_path):
    """Resolving a row on page one must not push an unseen report past page two."""

    store = BugReportStore(tmp_path / "auxillary.db")
    for index in range(LIST_LIMIT + 3):
        store.submit(title=f"Report {index}", description="Detail.")

    first = store.list_reports(status="open")
    seen = [row["id"] for row in first["reports"]]
    assert first["has_more"] is True

    # Another administrator resolves one of the rows already on screen, so it
    # leaves the filtered set and everything behind it shifts forward.
    store.set_status(seen[0], "resolved", actor="B")

    second = store.list_reports(status="open", cursor=first["next_cursor"])
    seen += [row["id"] for row in second["reports"]]

    still_open = {row["id"] for row in store.list_reports(status="open", limit=500)["reports"]}
    assert not still_open - set(seen), "an open report was never shown"
    assert len(seen) == len(set(seen)), "a report was served twice"


def test_paging_reaches_everything_across_a_mix_of_statuses_and_severities(tmp_path):
    """The cursor walks the same compound key the listing is ordered by."""

    store = BugReportStore(tmp_path / "auxillary.db")
    filed = [
        store.submit(
            title=f"Report {index}",
            description="Detail.",
            severity=SEVERITIES[index % len(SEVERITIES)],
        )
        for index in range(LIST_LIMIT + 55)
    ]
    for index in range(0, len(filed), 7):
        store.set_status(filed[index], "resolved", actor="B")

    walked, cursor = [], None
    while True:
        page = store.list_reports(cursor=cursor)
        walked.extend(row["id"] for row in page["reports"])
        if not page["has_more"]:
            break
        cursor = page["next_cursor"]

    assert sorted(walked) == sorted(filed)
    assert len(walked) == len(set(walked))


def test_the_total_describes_the_filter_rather_than_the_page(tmp_path):
    """It is counted without the cursor, so it does not shrink as pages are walked."""

    store = BugReportStore(tmp_path / "auxillary.db")
    for index in range(LIST_LIMIT + 8):
        store.submit(title=f"Report {index}", description="Detail.")

    first = store.list_reports()
    second = store.list_reports(cursor=first["next_cursor"])
    assert first["total"] == second["total"] == LIST_LIMIT + 8


def test_a_cursor_that_this_page_did_not_issue_is_refused(tmp_path):
    store = BugReportStore(tmp_path / "auxillary.db")
    store.submit(title="Charts blank", description="Detail.")
    for bad in ("not-base64!", "bm90LWEtY3Vyc29y", ""):
        if bad == "":
            continue  # empty means "first page", which is not an error
        with pytest.raises(BugReportValidationError):
            store.list_reports(cursor=bad)


def test_a_database_written_before_revisions_existed_is_migrated(tmp_path):
    """Reports filed by an earlier build must survive the upgrade, not be rebuilt."""

    path = tmp_path / "auxillary.db"
    store = BugReportStore(path)
    store.submit(title="Filed earlier", description="Detail.")

    # Stand in for the older schema by dropping the column back off.
    with sqlite3.connect(path) as conn:
        conn.execute("ALTER TABLE bug_reports DROP COLUMN revision")
    reopened = BugReportStore(path)
    stored = _reports(reopened)[0]

    assert stored["title"] == "Filed earlier"
    assert stored["revision"] == 1
    # And the guard works from there on.
    assert reopened.set_status(stored["id"], "resolved", actor="root",
                               expected_revision=1)["revision"] == 2



# ---------------------------------------------------------------------------
# The status check: what is on the share, without putting anything there
# ---------------------------------------------------------------------------


def test_describe_reports_a_missing_database_without_creating_it(tmp_path):
    """The whole point of the check: looking must not be what makes the file.

    Every other method creates on the way past, which is right when the caller
    came to file a report. Here the caller came to ask whether the share has one
    yet, and a check that answers "yes" because it just made it is no answer.
    """

    path = tmp_path / "GREMLIN Global DB" / "auxillary.db"
    store = BugReportStore(path)

    info = store.describe()

    assert info["exists"] is False
    assert info["parent_exists"] is False
    assert info["schema_ready"] is False
    assert info["summary"] is None
    assert sorted(info["missing_tables"]) == sorted(SCHEMA_TABLES)
    # The tables are reported missing whole; their columns are not listed
    # underneath, which would bury the one fact that matters.
    assert info["missing_columns"] == []
    assert info["altered_columns"] == []
    assert str(path) == info["path"]
    assert not path.exists()
    assert not path.parent.exists()


def test_describe_reports_a_ready_database_and_its_counts(tmp_path):
    path = tmp_path / "auxillary.db"
    store = BugReportStore(path)
    store.submit(title="Charts blank", description="No bars.", severity="blocking")
    resolved = store.submit(title="Typo", description="Says 'teh'.")
    store.set_status(resolved, "resolved", actor="root")

    info = store.describe()

    assert info["exists"] is True
    assert info["parent_exists"] is True
    assert info["schema_ready"] is True
    assert info["missing_tables"] == []
    assert info["size_bytes"] > 0
    assert info["summary"]["total"] == 2
    assert info["summary"]["open"] == 1
    assert info["summary"]["open_blocking"] == 1
    assert info["summary"]["resolved"] == 1


def test_describe_hides_sqlite_own_tables(tmp_path):
    """AUTOINCREMENT creates sqlite_sequence, which is not GREMLIN's to report."""

    store = BugReportStore(tmp_path / "auxillary.db")
    store.submit(title="Broken", description="It broke.")

    assert sorted(store.describe()["tables"]) == sorted(SCHEMA_TABLES)


def test_describe_names_the_table_an_incomplete_database_is_missing(tmp_path):
    """A file that opens and then fails on use is the case worth naming.

    A copy taken before the limiter's table existed looks installed from the
    outside. Saying which table is missing is what turns "the bug page is
    broken" into something an administrator can act on -- and the counts are
    still reported, because the reports themselves are readable.
    """

    path = tmp_path / "auxillary.db"
    store = BugReportStore(path)
    store.submit(title="Filed earlier", description="Detail.")
    with sqlite3.connect(path) as conn:
        conn.execute("DROP TABLE submission_attempts")

    info = BugReportStore(path).describe()

    assert info["exists"] is True
    assert info["schema_ready"] is False
    assert info["missing_tables"] == ["submission_attempts"]
    assert info["summary"]["total"] == 1


def test_describe_names_a_migrated_column_an_older_database_is_missing(tmp_path):
    """Both tables present is not the same as complete.

    A database from before revisions existed opens, lists both tables and reads
    its reports back. What it cannot do is the concurrency guard, and a check
    that stopped at the table names would have called it ready.
    """

    path = tmp_path / "auxillary.db"
    BugReportStore(path).submit(title="Filed earlier", description="Detail.")
    with sqlite3.connect(path) as conn:
        conn.execute("ALTER TABLE bug_reports DROP COLUMN revision")

    info = BugReportStore(path).describe()

    assert info["missing_tables"] == []
    assert info["missing_columns"] == ["bug_reports.revision"]
    assert info["schema_ready"] is False
    assert info["summary"]["total"] == 1


def test_describe_names_a_column_taken_off_a_table(tmp_path):
    """Present tables are not a complete schema, and this is the costly gap.

    A table that has lost a column opens, lists, and counts exactly like a
    healthy one, then refuses the first report filed against it -- reported as
    the drive being unreachable, because that is what a refused write looks like
    from the outside. Nobody chasing a mapped drive finds a dropped column.
    """

    path = tmp_path / "auxillary.db"
    BugReportStore(path).ensure_schema()
    with sqlite3.connect(path) as conn:
        conn.execute("ALTER TABLE bug_reports DROP COLUMN title")

    info = BugReportStore(path).describe()

    assert info["missing_tables"] == []
    assert info["missing_columns"] == ["bug_reports.title"]
    assert info["schema_ready"] is False


def test_describe_still_counts_what_a_damaged_database_holds(tmp_path):
    """The reports are readable even when something else is not, so report them."""

    path = tmp_path / "auxillary.db"
    BugReportStore(path).submit(title="Filed earlier", description="Detail.")
    with sqlite3.connect(path) as conn:
        conn.execute("ALTER TABLE bug_reports DROP COLUMN resolution_note")

    info = BugReportStore(path).describe()

    assert info["missing_columns"] == ["bug_reports.resolution_note"]
    assert info["summary"]["total"] == 1


def test_describe_names_the_missing_column_rather_than_failing_to_count(tmp_path):
    """When the count itself cannot run, the reason must survive it.

    Losing a column _summarise reads would make the count raise, and the whole
    check would come back as "could not be opened" -- the message for an
    unreachable share, on a database sitting right there. The useful answer is
    which column has gone.
    """

    path = tmp_path / "auxillary.db"
    BugReportStore(path).ensure_schema()
    with sqlite3.connect(path) as conn:
        conn.execute("ALTER TABLE bug_reports DROP COLUMN severity")

    info = BugReportStore(path).describe()

    assert info["missing_columns"] == ["bug_reports.severity"]
    assert info["schema_ready"] is False
    assert info["summary"] is None


def test_the_expected_columns_come_from_the_statements_that_create_them(tmp_path):
    """A hand-kept list would drift; a drifted list condemns a healthy database.

    The check is only safe to fail an install on because what it expects is read
    back off the schema itself. So: a database the schema just made must have
    nothing missing, whatever the schema happens to say today.
    """

    path = tmp_path / "auxillary.db"
    store = BugReportStore(path)
    store.ensure_schema()

    info = store.describe()

    assert info["missing_tables"] == []
    assert info["missing_columns"] == []
    assert info["schema_ready"] is True


def test_describe_catches_a_column_that_kept_its_name_and_lost_its_shape(tmp_path):
    """Comparing names alone lets through the damage that costs reports.

    A table rebuilt by hand can keep every column name and lose
    ``id INTEGER PRIMARY KEY``. Reports then file, return an id, and store NULL
    in the id column -- so nothing can find them again to resolve or delete, and
    a check that counted names would have called that database complete.
    """

    path = tmp_path / "auxillary.db"
    BugReportStore(path).ensure_schema()
    with sqlite3.connect(path) as conn:
        columns = [(r[1], r[2]) for r in conn.execute("PRAGMA table_info(bug_reports)")]
        conn.execute("DROP TABLE bug_reports")
        body = ", ".join(f"{name} {kind}" for name, kind in columns)
        conn.execute(f"CREATE TABLE bug_reports ({body})")

    info = BugReportStore(path).describe()

    assert info["missing_columns"] == []  # every name is still there
    assert info["schema_ready"] is False
    assert "bug_reports.id (expected INTEGER PRIMARY KEY, found INTEGER)" in (
        info["altered_columns"]
    )


def test_a_database_the_schema_migrated_is_not_called_altered(tmp_path):
    """The shape check must not condemn the databases it exists to serve.

    A column added by ALTER has to come out identical to the same column created
    by the CREATE TABLE, or every database that has been through a migration
    would be reported damaged -- with nothing able to mend what was never wrong.
    """

    path = tmp_path / "auxillary.db"
    BugReportStore(path).ensure_schema()
    with sqlite3.connect(path) as conn:
        conn.execute("ALTER TABLE bug_reports DROP COLUMN revision")
    BugReportStore(path).ensure_schema()  # puts it back the way an upgrade does

    info = BugReportStore(path).describe()

    assert info["altered_columns"] == []
    assert info["missing_columns"] == []
    assert info["schema_ready"] is True


def test_describe_repairs_nothing_by_itself(tmp_path):
    """It reports; ensure_schema fixes. A check with a side effect is not a check."""

    path = tmp_path / "auxillary.db"
    BugReportStore(path).submit(title="Filed earlier", description="Detail.")
    with sqlite3.connect(path) as conn:
        conn.execute("DROP TABLE submission_attempts")

    store = BugReportStore(path)
    store.describe()
    assert store.describe()["missing_tables"] == ["submission_attempts"]

    store.ensure_schema()
    assert store.describe()["missing_tables"] == []


def test_describe_reports_a_file_that_is_not_a_database_as_a_store_error(tmp_path):
    """Present but unreadable is the case an administrator has to act on."""

    path = tmp_path / "auxillary.db"
    path.write_bytes(b"this is not a database")
    store = BugReportStore(path)

    with pytest.raises(BugReportStoreError) as caught:
        store.describe()
    assert str(path) in str(caught.value)
    assert not isinstance(caught.value, sqlite3.Error)


def test_the_status_check_reads_through_a_connection_that_cannot_write(tmp_path):
    """Asserted on the connection itself, because permissions cannot assert it.

    The check opens the file read-only so that it works from a machine with
    read access to the share and no write access -- which is the machine whose
    administrator most wants an answer. A test that made the file unwritable
    would prove nothing when the suite runs as a user who may write it anyway,
    so the guarantee is asserted where it is made.
    """

    path = tmp_path / "auxillary.db"
    store = BugReportStore(path)
    store.submit(title="Charts blank", description="No bars.")

    conn = store._connect_readonly()
    try:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("DELETE FROM bug_reports")
    finally:
        conn.close()

    # And the reports are still all there to be counted.
    assert store.describe()["summary"]["total"] == 1


def test_a_unc_path_becomes_a_uri_sqlite_will_accept():
    """The share reached by name rather than by a mapped drive letter.

    pathlib puts the host in the URI's authority; sqlite refuses any authority
    that is not empty or localhost. Left alone, that turns a perfectly readable
    database on \\\\server\\share into "could not be opened".

    Asserted on the URI rather than by opening one, because a POSIX test runner
    has no UNC path to resolve -- but it has the same sqlite, and sqlite checks
    the authority before it ever looks for the file.
    """

    raw = PureWindowsPath(r"\\server\share\GREMLIN Global DB\auxillary.db").as_uri()
    assert raw.startswith("file://server/")  # what pathlib hands over

    fixed = _sqlite_file_uri(raw)
    assert fixed == "file:////server/share/GREMLIN%20Global%20DB/auxillary.db"

    with pytest.raises(sqlite3.OperationalError) as caught:
        sqlite3.connect(f"{fixed}?mode=ro", uri=True)
    # Past the authority check, and failing only because no such share is
    # mounted here -- which is the whole of what this fix has to prove.
    assert "authority" not in str(caught.value)


@pytest.mark.parametrize(
    "windows_path",
    [r"Z:\FACIL\GREMLIN Global DB\auxillary.db", r"C:\GREMLIN\auxillary.db"],
)
def test_a_mapped_drive_letter_is_left_exactly_as_pathlib_wrote_it(windows_path):
    """The UNC repair must not touch the ordinary case it does not apply to."""

    raw = PureWindowsPath(windows_path).as_uri()
    assert _sqlite_file_uri(raw) == raw


def test_a_posix_path_is_left_exactly_as_pathlib_wrote_it(tmp_path):
    raw = (tmp_path / "auxillary.db").as_uri()
    assert _sqlite_file_uri(raw) == raw
