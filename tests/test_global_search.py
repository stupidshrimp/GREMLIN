"""The topbar's global search: its catalog, its role filtering, and its ranking.

The catalog is hand-written configuration pointing at routes and fragments
defined elsewhere, so most of what can go wrong is drift -- a route renamed, a
heading's id changed, an entry added without a role gate. These are the checks
that catch that at the point it happens rather than on somebody's 404.
"""
import importlib
import json
import re

import pytest
from werkzeug.exceptions import HTTPException


def _app(monkeypatch, tmp_path):
    monkeypatch.setenv("GREMLIN_ACCESS_DB_PATH", str(tmp_path / "accesscontrol.db"))
    monkeypatch.setenv("GREMLIN_DB_PATH", str(tmp_path / "gremlin.db"))
    monkeypatch.setenv("GREMLIN_ADMIN_USERNAME", "root")
    monkeypatch.setenv("GREMLIN_ADMIN_PIN", "secret")
    import app
    return importlib.reload(app)


def _embedded_index(client, path="/"):
    """The catalog as the topbar actually embedded it, parsed back out of the page."""
    body = client.get(path).get_data(as_text=True)
    match = re.search(r'id="globalSearchIndex">(.*?)</script>', body, re.S)
    assert match, "topbar.html did not embed a search catalog"
    return json.loads(match.group(1))


def _labels(index):
    return {entry["label"] for entry in index}


# --- the box itself ---------------------------------------------------------

@pytest.mark.parametrize("page", ["/", "/settings", "/metrics", "/reliability-links", "/about", "/search"])
def test_every_page_carries_the_search_box(monkeypatch, tmp_path, page):
    """base.html draws the topbar everywhere, so no page may be missing the search.

    The catalog arrives through a context processor rather than each view's
    arguments; a missing variable would render as an empty index instead of
    raising, which is what this would catch.
    """
    client = _app(monkeypatch, tmp_path).app.test_client()
    body = client.get(page).get_data(as_text=True)
    assert 'id="globalSearchInput"' in body, page
    assert _embedded_index(client, page), page


def test_search_is_usable_without_logging_in(monkeypatch, tmp_path):
    client = _app(monkeypatch, tmp_path).app.test_client()
    assert client.get("/search?q=metrics").status_code == 200
    assert client.get("/search").status_code == 200


# --- role filtering ---------------------------------------------------------

def test_visitors_are_not_offered_doors_that_would_refuse_them(monkeypatch, tmp_path):
    """A signed-out visitor's catalog holds nothing gated behind a role.

    The developer area is deliberately unlinked from every page -- the account
    dialog is its only entrance, and only for an administrator. Search must not
    become the second entrance, and it must not put the list of developer routes
    in the page source of an anonymous request either.
    """
    module = _app(monkeypatch, tmp_path)
    index = _embedded_index(module.app.test_client())

    assert not [entry for entry in index if entry["context"] == "Developer"]
    assert "Disposition" not in _labels(index)
    for entry in index:
        assert "/developer" not in entry["url"], entry
    assert "Log in" in _labels(index)

    # Settings draws its three editing sections for an editor and leaves them
    # out of the page for everybody else. Search has to agree, or it sends a
    # viewer to a fragment their copy of the page does not contain.
    assert _labels(index).isdisjoint(
        {"Asset group schedules", "Linked downtime rules", "Refresh CMMS mapping"}
    )


def test_a_viewer_is_no_better_off_than_a_visitor(monkeypatch, tmp_path):
    """A signed-in viewer may write no more than a signed-out one, search included."""
    module = _app(monkeypatch, tmp_path)
    module.access_control.save_user(None, "viewer", "1357", "viewer")
    client = module.app.test_client()
    assert client.post("/auth/login", json={"username": "viewer", "pin": "1357"}).status_code == 200

    index = _embedded_index(client)
    assert _labels(index).isdisjoint({
        "Disposition",
        "Disposition Work Orders",
        "Disposition PM Resets",
        "Asset group schedules",
        "Linked downtime rules",
        "Refresh CMMS mapping",
    })
    assert not [entry for entry in index if entry["context"] == "Developer"]


def test_an_editor_gets_disposition_and_still_no_developer_tools(monkeypatch, tmp_path):
    module = _app(monkeypatch, tmp_path)
    module.access_control.save_user(None, "editor", "2468", "editor")
    client = module.app.test_client()
    client.post("/auth/login", json={"username": "editor", "pin": "2468"})

    index = _embedded_index(client)
    assert {"Disposition", "Disposition Work Orders", "Disposition PM Resets"} <= _labels(index)
    assert not [entry for entry in index if entry["context"] == "Developer"]
    assert "Log out" in _labels(index)


def test_an_administrator_gets_the_whole_catalog(monkeypatch, tmp_path):
    module = _app(monkeypatch, tmp_path)
    client = module.app.test_client()
    client.post("/auth/login", json={"username": "root", "pin": "secret"})

    index = _embedded_index(client)
    assert {"Developer", "Database inspection", "Limble sync", "Access control", "User activity"} <= _labels(index)
    assert "Disposition" in _labels(index)


# --- the catalog points at real things --------------------------------------

def test_every_entry_points_at_a_route_that_exists(monkeypatch, tmp_path):
    """The catalog is hand-written, so a renamed route would break it silently.

    Matching against the URL map rather than requesting each page keeps this
    about the wiring: a page that needs a database GREMLIN does not have here
    would answer 503, which says nothing about whether the link is right.
    """
    module = _app(monkeypatch, tmp_path)
    adapter = module.app.url_map.bind("localhost")

    for entry in module.SEARCH_ENTRIES:
        path = entry["url"].split("?")[0].split("#")[0]
        try:
            adapter.match(path)
        except HTTPException as exc:  # pragma: no cover - only on a broken catalog
            pytest.fail(f"{entry['label']} points at {path!r}, which no route serves ({exc})")


def test_gated_entries_name_a_role_the_app_knows(monkeypatch, tmp_path):
    module = _app(monkeypatch, tmp_path)
    for entry in module.SEARCH_ENTRIES:
        role = entry.get("role")
        assert role is None or role in module.ROLE_LEVEL, entry["label"]


def test_entries_are_shaped_alike_and_named_once(monkeypatch, tmp_path):
    module = _app(monkeypatch, tmp_path)
    seen = set()
    for entry in module.SEARCH_ENTRIES:
        assert entry.keys() >= {"label", "url", "kind", "context", "keywords"}, entry
        assert entry["kind"] in {"page", "function"}, entry
        assert isinstance(entry["keywords"], list), entry
        # A label and context together are what the dropdown shows; two rows
        # reading identically would be indistinguishable once clicked.
        key = (entry["label"], entry["context"])
        assert key not in seen, f"duplicate entry {key}"
        seen.add(key)


def test_every_sidebar_page_can_be_found_by_searching_for_it(monkeypatch, tmp_path):
    """Whatever the sidebar links to, the search box has to reach as well."""
    module = _app(monkeypatch, tmp_path)
    catalog = {entry["url"] for entry in module.SEARCH_ENTRIES}
    for link in module.NAV_LINKS:
        assert link["url"] in catalog, link["label"]


def test_footer_pages_can_be_found_by_searching_for_them(monkeypatch, tmp_path):
    module = _app(monkeypatch, tmp_path)
    catalog = {entry["url"] for entry in module.SEARCH_ENTRIES}
    for link in module.FOOTER_LINKS:
        assert link["url"] in catalog, link["label"]


# --- ranking ----------------------------------------------------------------

def _index(module):
    return module._search_index(is_admin=True, can_edit=True, has_account=True)


def test_a_page_named_by_the_query_comes_first(monkeypatch, tmp_path):
    module = _app(monkeypatch, tmp_path)
    index = _index(module)
    assert module._search_matches("metrics", index)[0]["label"] == "Metrics"
    assert module._search_matches("settings", index)[0]["label"] == "Settings"


def test_a_word_nobody_can_see_still_finds_its_entry(monkeypatch, tmp_path):
    """Keywords exist so the word somebody has in mind reaches the right entry."""
    module = _app(monkeypatch, tmp_path)
    index = _index(module)
    for query, expected in [
        ("changelog", "Patch Notes"),
        ("uptime", "Asset availability by month"),
        ("sql", "Database inspection"),
        ("sharepoint", "Reliability Links"),
    ]:
        assert module._search_matches(query, index)[0]["label"] == expected, query


def test_a_second_word_narrows_rather_than_widens(monkeypatch, tmp_path):
    module = _app(monkeypatch, tmp_path)
    index = _index(module)
    one = module._search_matches("analysis", index)
    two = module._search_matches("downtime analysis", index)
    assert len(two) < len(one)
    assert two[0]["label"] == "Downtime Driver Analysis"
    for entry in two:
        assert "downtime" in (entry["label"] + entry["keywords"] + entry["context"]).lower()


def test_a_query_matching_nothing_returns_nothing(monkeypatch, tmp_path):
    module = _app(monkeypatch, tmp_path)
    index = _index(module)
    assert module._search_matches("zzzz", index) == []
    assert module._search_matches("   ", index) == []


def test_ranking_ignores_case(monkeypatch, tmp_path):
    module = _app(monkeypatch, tmp_path)
    index = _index(module)
    lower = [entry["label"] for entry in module._search_matches("weibull", index)]
    upper = [entry["label"] for entry in module._search_matches("WEIBULL", index)]
    assert lower == upper and lower


# --- the no-script page -----------------------------------------------------

def test_the_results_page_lists_what_the_dropdown_would_have(monkeypatch, tmp_path):
    module = _app(monkeypatch, tmp_path)
    client = module.app.test_client()
    body = client.get("/search?q=weibull").get_data(as_text=True)
    assert "Weibull Analysis" in body
    assert 'href="/life-data-analysis/perform-analysis?analysis=Weibull+Analysis"' in body


def test_the_results_page_says_so_when_nothing_matches(monkeypatch, tmp_path):
    module = _app(monkeypatch, tmp_path)
    body = module.app.test_client().get("/search?q=zzzz").get_data(as_text=True)
    assert "0 results" in body
    assert "Nothing in GREMLIN matches that" in body


def test_the_results_page_leaves_out_entries_it_cannot_link_to(monkeypatch, tmp_path):
    """"Log in" opens a dialog the dropdown owns; without a script it goes nowhere."""
    module = _app(monkeypatch, tmp_path)
    body = module.app.test_client().get("/search?q=log+in").get_data(as_text=True)
    assert 'href="#account"' not in body


def test_the_results_page_obeys_the_same_role_gate(monkeypatch, tmp_path):
    module = _app(monkeypatch, tmp_path)
    body = module.app.test_client().get("/search?q=database").get_data(as_text=True)
    assert "/developer/database" not in body

    client = module.app.test_client()
    client.post("/auth/login", json={"username": "root", "pin": "secret"})
    assert "/developer/database" in client.get("/search?q=database").get_data(as_text=True)


def test_a_query_is_escaped_rather_than_rendered(monkeypatch, tmp_path):
    """The query is echoed back on the page, so it goes through Jinja's escaping."""
    module = _app(monkeypatch, tmp_path)
    body = module.app.test_client().get("/search?q=%3Cimg+src%3Dx%3E").get_data(as_text=True)
    assert "<img src=x>" not in body
    assert "&lt;img src=x&gt;" in body
