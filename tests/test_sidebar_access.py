"""The sidebar's two access rules: logging in, and which department you are in.

Both are decided in one place -- PAGES, and the helpers beside it in app.py --
and land in three: the markup the sidebar draws, the route that refuses a typed
URL, and the search catalog. These are the checks that the three keep agreeing.
"""

import importlib
import re

import pytest


def _app(monkeypatch, tmp_path):
    monkeypatch.setenv("GREMLIN_ACCESS_DB_PATH", str(tmp_path / "accesscontrol.db"))
    monkeypatch.setenv("GREMLIN_DB_PATH", str(tmp_path / "gremlin.db"))
    import app
    return importlib.reload(app)


def _client(module, department="all", staff_level="all", role="viewer"):
    """A signed-in browser for an account recorded in `department`."""
    module.access_control.save_user(None, "somebody", "2468", role, department, staff_level)
    client = module.app.test_client()
    assert client.post(
        "/auth/login", json={"username": "somebody", "pin": "2468"}
    ).status_code == 200
    return client


def _sidebar(client, path="/"):
    body = client.get(path).get_data(as_text=True)
    nav = re.search(r'<nav class="sidebar-nav">(.*?)</nav>', body, re.S)
    assert nav, "the page rendered no sidebar navigation"
    return nav.group(1)


def _entries(client, path="/"):
    """Every sidebar entry as (label, locked)."""
    found = []
    for item in re.findall(r"<li>(.*?)</li>", _sidebar(client, path), re.S):
        label = re.search(r'<span class="nav-label">(.*?)</span>', item, re.S)
        assert label, item
        found.append((label.group(1).strip(), "nav-locked" in item))
    return found


def _labels(client, path="/"):
    return [label for label, _ in _entries(client, path)]


# The three GREMLIN shows everybody, signed in or not, in whatever department.
OPEN = ["Home", "Reliability Links", "Configuration"]
# The three that belong to Operations & Maintenance.
DEPARTMENT_PAGES = {
    "Safety Report": "/safety-report",
    "PM Task Tracker": "/pm-task-tracker",
    "Overdue WO Tracker": "/overdue-wo-tracker",
}


# --- signed out: everything else is struck through --------------------------

def test_a_visitor_gets_the_three_open_pages_as_links(monkeypatch, tmp_path):
    entries = dict(_entries(_app(monkeypatch, tmp_path).app.test_client()))
    for label in OPEN:
        assert label in entries, label
        assert entries[label] is False, f"{label} was locked for a visitor"


def test_a_visitor_gets_every_other_entry_struck_through(monkeypatch, tmp_path):
    """The whole point of the signed-out sidebar: still the full list, all of it
    crossed out except the three that need no account."""
    entries = _entries(_app(monkeypatch, tmp_path).app.test_client())
    locked = [label for label, is_locked in entries if is_locked]
    assert locked, "nothing at all was locked for a signed-out visitor"
    assert set(locked) == {label for label, _ in entries} - set(OPEN)


def test_a_locked_entry_advertises_no_address(monkeypatch, tmp_path):
    """It is a button, not a dimmed link: there is no URL on it to middle-click."""
    sidebar = _sidebar(_app(monkeypatch, tmp_path).app.test_client())
    for item in re.findall(r"<li>(.*?)</li>", sidebar, re.S):
        if "nav-locked" in item:
            assert "href" not in item, item


def test_a_locked_entry_carries_the_toast_and_the_hover(monkeypatch, tmp_path):
    module = _app(monkeypatch, tmp_path)
    sidebar = _sidebar(module.app.test_client())
    assert f'data-locked-message="{module.LOCKED_NAV_MESSAGE}"' in sidebar
    assert module.LOCKED_NAV_HINT in sidebar
    # The wording is what somebody actually reads, so it is worth pinning.
    assert module.LOCKED_NAV_MESSAGE == "To see this page, please log in."
    assert module.LOCKED_NAV_HINT == "Log in first to see this page!"


def test_signing_in_unlocks_the_sidebar(monkeypatch, tmp_path):
    module = _app(monkeypatch, tmp_path)
    assert any(locked for _, locked in _entries(module.app.test_client()))
    assert not any(locked for _, locked in _entries(_client(module)))


def test_the_three_open_pages_are_served_to_a_visitor(monkeypatch, tmp_path):
    client = _app(monkeypatch, tmp_path).app.test_client()
    for route in ["/", "/reliability-links", "/configuration"]:
        assert client.get(route).status_code == 200, route


def test_a_locked_page_is_refused_rather_than_only_struck_through(monkeypatch, tmp_path):
    """Striking the entry out is presentation; this is what makes it true."""
    client = _app(monkeypatch, tmp_path).app.test_client()
    for route in ["/metrics", "/life-data-analysis/perform-analysis"]:
        response = client.get(route)
        assert response.status_code == 403, route
        assert b"needs an account" in response.data, route


# --- signed in: the department decides --------------------------------------

@pytest.mark.parametrize(
    "department", ["operations", "maintenance", "operations_maintenance", "all"]
)
def test_operations_and_maintenance_are_offered_the_department_pages(
    monkeypatch, tmp_path, department
):
    """A combined value names both its parts, and "all" names every part."""
    client = _client(_app(monkeypatch, tmp_path), department=department)
    labels = _labels(client)
    for label, route in DEPARTMENT_PAGES.items():
        assert label in labels, f"{label} was missing for {department}"
        assert client.get(route).status_code == 200, route


def test_another_department_is_neither_shown_nor_served_them(monkeypatch, tmp_path):
    client = _client(_app(monkeypatch, tmp_path), department="facilities")
    labels = _labels(client)
    for label, route in DEPARTMENT_PAGES.items():
        assert label not in labels, f"{label} was offered to Facilities"
        response = client.get(route)
        assert response.status_code == 403, route
        assert b"is for another department" in response.data, route


def test_the_refusal_names_both_sides_of_the_mismatch(monkeypatch, tmp_path):
    """So that somebody reading it can tell whether their account is wrong."""
    client = _client(_app(monkeypatch, tmp_path), department="facilities")
    body = client.get("/safety-report").get_data(as_text=True)
    assert "Operations &amp; Maintenance" in body
    assert "Facilities" in body


@pytest.mark.parametrize("staff_level", ["engineer", "leadership", "associate", "all"])
def test_every_level_is_offered_a_page_marked_for_all_levels(
    monkeypatch, tmp_path, staff_level
):
    client = _client(
        _app(monkeypatch, tmp_path),
        department="operations",
        staff_level=staff_level,
    )
    assert set(DEPARTMENT_PAGES) <= set(_labels(client)), staff_level


@pytest.mark.parametrize(
    "department", ["facilities", "operations", "maintenance", "operations_maintenance", "all"]
)
def test_no_department_ends_up_below_the_floor(monkeypatch, tmp_path, department):
    """Whatever a department narrows away, these three survive it."""
    client = _client(_app(monkeypatch, tmp_path), department=department)
    assert set(OPEN) <= set(_labels(client)), department


def test_every_department_page_declares_both_of_its_keys(monkeypatch, tmp_path):
    """The rule reads two keys; a page that sets one is narrowed by half."""
    module = _app(monkeypatch, tmp_path)
    for route in DEPARTMENT_PAGES.values():
        page = module.PAGES_BY_ROUTE[route]
        assert page["department"] == "operations_maintenance", route
        assert page["staff_level"] == "all", route


# --- the placeholder pages themselves ----------------------------------------

def test_each_new_page_renders_and_says_it_is_not_built_yet(monkeypatch, tmp_path):
    client = _client(_app(monkeypatch, tmp_path), department="operations_maintenance")
    for label, route in DEPARTMENT_PAGES.items():
        body = client.get(route).get_data(as_text=True)
        assert f"<title>{label}</title>" in body, route
        assert "not built yet" in body, route


# --- the search catalog agrees with the sidebar ------------------------------

def test_search_hides_a_page_the_account_will_never_be_given(monkeypatch, tmp_path):
    module = _app(monkeypatch, tmp_path)
    facilities = _client(module, department="facilities")
    assert "/safety-report" not in facilities.get("/search?q=safety").get_data(as_text=True)


def test_search_offers_it_to_the_department_it_belongs_to(monkeypatch, tmp_path):
    module = _app(monkeypatch, tmp_path)
    operations = _client(module, department="operations")
    assert "/safety-report" in operations.get("/search?q=safety").get_data(as_text=True)


def test_search_still_offers_a_visitor_what_logging_in_would_open(monkeypatch, tmp_path):
    """Department narrows; being signed out only locks. The page asks them in."""
    module = _app(monkeypatch, tmp_path)
    body = module.app.test_client().get("/search?q=safety").get_data(as_text=True)
    assert "/safety-report" in body


# --- the pages that explain the rules ----------------------------------------
#
# Three places tell somebody what they may open: the login dialog on every page,
# the refusal page, and the developer page where an administrator sets the two
# fields. All three said "everything else is readable without an account" until
# the rules above made that false, and nothing failed when it did -- which is
# what these are for. They check the claim, not the sentence, so the wording can
# still be rewritten.

def _says_everything_is_readable(body: str) -> bool:
    """Whether a page still makes the promise the login gate broke."""
    lowered = body.lower()
    return any(
        phrase in lowered
        for phrase in (
            "everything else in gremlin stays readable",
            "remains available read-only without an account",
            "nothing is filtered by them yet",
        )
    )


@pytest.mark.parametrize("page", ["/", "/configuration", "/reliability-links"])
def test_the_login_dialog_names_what_an_account_is_for(monkeypatch, tmp_path, page):
    """It is drawn on every page, and it is where somebody decides to bother."""
    body = _app(monkeypatch, tmp_path).app.test_client().get(page).get_data(as_text=True)
    assert not _says_everything_is_readable(body), page
    for label in OPEN:
        assert label in body, label
    assert "department" in body.lower(), page


def test_the_refusal_page_does_not_promise_what_it_just_refused(monkeypatch, tmp_path):
    """The editor-role refusal is rendered for signed-out visitors too."""
    body = (
        _app(monkeypatch, tmp_path)
        .app.test_client()
        .get("/life-data-analysis/disposition")
        .get_data(as_text=True)
    )
    assert not _says_everything_is_readable(body)


def test_the_developer_page_explains_the_rule_it_edits(monkeypatch, tmp_path):
    """Whoever sets a department here is the one person who must not be misled."""
    module = _app(monkeypatch, tmp_path)
    client = _client(module, role="admin")
    body = client.get("/developer/access").get_data(as_text=True)
    assert not _says_everything_is_readable(body)
    # The three facts an administrator needs before changing anybody's row.
    assert "overlap" in body.lower()
    for label in OPEN:
        assert label in body, label
