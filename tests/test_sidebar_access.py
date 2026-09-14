"""The sidebar's access rules: logging in, and which department you are in.

All of them are decided in one place -- PAGES, and the helpers beside it in
app.py -- and land in three: the markup the sidebar draws, the route that
refuses a typed URL, and the search catalog. These are the checks that the three
keep agreeing.

The department rule has two halves that pull in opposite directions and are
easy to mistake for each other. "department" says who a page is *for*, and is an
overlap test: Safety Report is marked for Operations & Maintenance and Facilities
does not get it. "withheld_from_department" says who a page is kept *out* of, and
is a containment test: Metrics is kept from Operations & Maintenance, so an
Operations account loses it and an "all departments" account -- which covers
Facilities too -- keeps it.
"""

import importlib
import re

import pytest


def _app(monkeypatch, tmp_path):
    monkeypatch.setenv("GREMLIN_ACCESS_DB_PATH", str(tmp_path / "accesscontrol.db"))
    monkeypatch.setenv("GREMLIN_DB_PATH", str(tmp_path / "gremlin.db"))
    import app
    return importlib.reload(app)


def _client(module, department="all", staff_level="all", role="viewer", username="somebody"):
    """A signed-in browser for an account recorded in `department`.

    `username` is a parameter only so that one test can sign two accounts in
    against the same module and compare what each is served.
    """
    module.access_control.save_user(None, username, "2468", role, department, staff_level)
    client = module.app.test_client()
    assert client.post(
        "/auth/login", json={"username": username, "pin": "2468"}
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


# The three GREMLIN shows a visitor with no account at all.
OPEN = ["Home", "Reliability Links", "Configuration"]
# The two of those three that no department can narrow away either. Configuration
# is not among them: it is open to anybody who has not signed in, and kept from a
# signed-in Operations & Maintenance account, which is the one page where those
# two rules disagree.
FLOOR = ["Home", "Reliability Links"]
# The three that belong to Operations & Maintenance.
DEPARTMENT_PAGES = {
    "Safety Report": "/safety-report",
    "PM Task Tracker": "/pm-task-tracker",
    "Overdue WO Tracker": "/overdue-wo-tracker",
}
# The three kept out of Operations & Maintenance, and the address space each one
# owns -- the page itself, and what would otherwise still answer underneath it.
WITHHELD_PAGES = {
    "Life Data Analysis": [
        "/life-data-analysis/perform-analysis",
        "/life-data-analysis/disposition",
        "/life-data-analysis/failure-classification",
    ],
    "Metrics": ["/metrics", "/metrics/api/reliability"],
    "Configuration": ["/configuration", "/settings"],
}
# Every department that loses those three, and every one that keeps them.
WITHHELD_FROM = ["operations", "maintenance", "operations_maintenance"]
KEEPS_THEM = ["facilities", "all"]


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
    """Whatever a department narrows or withholds, these two survive it.

    The point is that nobody can be left staring at an empty sidebar, which is
    why the floor is checked for every department rather than for the ones the
    rules happen to touch today.
    """
    client = _client(_app(monkeypatch, tmp_path), department=department)
    labels = _labels(client)
    assert set(FLOOR) <= set(labels), department
    for route in ["/", "/reliability-links"]:
        assert client.get(route).status_code == 200, (department, route)


# --- signed in: the three pages a department is kept out of -------------------

@pytest.mark.parametrize("department", WITHHELD_FROM)
def test_operations_and_maintenance_lose_the_withheld_pages(
    monkeypatch, tmp_path, department
):
    """Gone from the sidebar, not struck through: nothing here is about logging
    in, so there is nothing to invite the account to do about it."""
    client = _client(_app(monkeypatch, tmp_path), department=department)
    labels = _labels(client)
    for label in WITHHELD_PAGES:
        assert label not in labels, f"{label} was offered to {department}"


@pytest.mark.parametrize("department", WITHHELD_FROM)
def test_the_whole_section_is_refused_not_only_its_front_door(
    monkeypatch, tmp_path, department
):
    """Hiding the entry and leaving /metrics/api/... answering would make the
    page invisible rather than closed."""
    client = _client(_app(monkeypatch, tmp_path), department=department)
    for label, routes in WITHHELD_PAGES.items():
        for route in routes:
            assert client.get(route).status_code == 403, f"{route} ({department})"


@pytest.mark.parametrize("department", KEEPS_THEM)
def test_every_other_department_keeps_them(monkeypatch, tmp_path, department):
    """"All departments" covers Facilities as well as the other two, so it is
    not contained by the withheld pair and keeps all three pages."""
    client = _client(_app(monkeypatch, tmp_path), department=department)
    labels = _labels(client)
    for label, routes in WITHHELD_PAGES.items():
        assert label in labels, f"{label} was taken from {department}"
        assert client.get(routes[0]).status_code == 200, (department, routes[0])


def test_configuration_is_open_to_a_visitor_and_closed_to_operations(
    monkeypatch, tmp_path
):
    """The one page where "open to everybody" and "kept from a department" meet.
    Being on the open floor says no account is needed, never that no rule
    applies."""
    module = _app(monkeypatch, tmp_path)
    assert module.app.test_client().get("/configuration").status_code == 200
    operations = _client(module, department="operations")
    assert operations.get("/configuration").status_code == 403
    assert "Configuration" not in _labels(operations)


def test_a_withheld_api_answers_in_json_rather_than_a_page(monkeypatch, tmp_path):
    """Most of a withheld section is endpoints, and a page of HTML handed to a
    fetch() is a parse error rather than an answer."""
    client = _client(_app(monkeypatch, tmp_path), department="operations")
    response = client.get("/metrics/api/reliability")
    assert response.status_code == 403
    assert response.is_json
    assert "Operations & Maintenance" in response.get_json()["error"]


def test_the_withheld_refusal_names_the_account_and_what_it_lost(
    monkeypatch, tmp_path
):
    """Somebody reading it should be able to tell whether their account is wrong
    rather than whether GREMLIN is."""
    client = _client(_app(monkeypatch, tmp_path), department="maintenance")
    body = client.get("/metrics").get_data(as_text=True)
    assert "Operations &amp; Maintenance" in body
    assert "Maintenance" in body


def test_the_visitor_sidebar_is_unchanged_by_the_withholding(monkeypatch, tmp_path):
    """Nobody has said which department a signed-out visitor is in, so nothing is
    withheld from them; the three entries are locked, exactly as before."""
    entries = dict(_entries(_app(monkeypatch, tmp_path).app.test_client()))
    assert entries["Life Data Analysis"] is True
    assert entries["Metrics"] is True
    assert entries["Configuration"] is False


def test_every_withheld_page_declares_the_section_it_stands_for(monkeypatch, tmp_path):
    """A withheld page that named only its own route would leave the rest of its
    section answering, which is the failure this key exists to prevent."""
    module = _app(monkeypatch, tmp_path)
    withheld = {page["title"]: page for _route, page in module.PAGES_BY_ROUTE.items()
                if page.get("withheld_from_department")}
    assert set(withheld) == set(WITHHELD_PAGES)
    for title, page in withheld.items():
        assert page["withheld_from_department"] == "operations_maintenance", title
        assert page["section"], title
        for route in WITHHELD_PAGES[title]:
            assert any(
                route == prefix or route.startswith(prefix + "/")
                for prefix in page["section"]
            ), f"{route} is outside {title}'s declared section"


def test_home_stops_advertising_a_section_it_just_closed(monkeypatch, tmp_path):
    """Home stays open to everybody, and its cards open Life Data Analysis. A
    card that only answered 403 would be the advertisement the sidebar just
    stopped making."""
    module = _app(monkeypatch, tmp_path)
    operations = _client(module, department="operations", username="ops")
    assert "/life-data-analysis" not in operations.get("/").get_data(as_text=True)
    facilities = _client(module, department="facilities", username="fac")
    assert "/life-data-analysis/perform-analysis" in facilities.get("/").get_data(as_text=True)


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


def test_each_new_page_carries_the_coming_soon_mark(monkeypatch, tmp_path):
    """The same mark its sidebar entry carries, on the page it leads to."""
    module = _app(monkeypatch, tmp_path)
    client = _client(module, department="operations_maintenance")
    for route in DEPARTMENT_PAGES.values():
        body = client.get(route).get_data(as_text=True)
        assert module.COMING_SOON_LABEL in body, route
        assert "placeholder-badge" in body, route
        assert module.COMING_SOON_ICON in body, route


def test_the_sidebar_marks_the_pages_that_are_not_built_yet(monkeypatch, tmp_path):
    """And only those: a badge on a page that works would be a lie about it."""
    module = _app(monkeypatch, tmp_path)
    client = _client(module, department="operations_maintenance")
    marked = set()
    for item in re.findall(r"<li>(.*?)</li>", _sidebar(client), re.S):
        label = re.search(r'<span class="nav-label">(.*?)</span>', item, re.S).group(1)
        if "nav-coming-soon" in item:
            marked.add(label.strip())
            assert module.COMING_SOON_LABEL in item, label
    assert marked == set(DEPARTMENT_PAGES)


def test_the_mark_says_coming_soon_in_words_as_well_as_in_a_shape(
    monkeypatch, tmp_path
):
    """An hourglass reads as nothing at all to a screen reader, so the badge
    carries the word too, and the collapsed rail has it in the tooltip."""
    module = _app(monkeypatch, tmp_path)
    sidebar = _sidebar(_client(module, department="operations"))
    assert f'<span class="nav-coming-soon-text">{module.COMING_SOON_LABEL}</span>' in sidebar
    assert f'title="Safety Report — {module.COMING_SOON_LABEL}"' in sidebar


def test_a_visitor_sees_the_mark_on_the_locked_entries_too(monkeypatch, tmp_path):
    """Signed out the three are struck through rather than hidden, and being
    locked does not make them any more built than they were."""
    module = _app(monkeypatch, tmp_path)
    sidebar = _sidebar(module.app.test_client())
    for label in DEPARTMENT_PAGES:
        item = next(
            entry for entry in re.findall(r"<li>(.*?)</li>", sidebar, re.S) if label in entry
        )
        assert "nav-locked" in item, label
        assert "nav-coming-soon" in item, label


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


def test_search_is_not_the_way_around_a_withheld_page(monkeypatch, tmp_path):
    module = _app(monkeypatch, tmp_path)
    operations = _client(module, department="operations")
    for query in ["metrics", "configuration", "weibull"]:
        body = operations.get(f"/search?q={query}").get_data(as_text=True)
        for path in ["/metrics", "/configuration", "/life-data-analysis"]:
            assert f'href="{path}' not in body, (query, path)


def test_search_drops_the_deep_links_into_a_withheld_section_too(monkeypatch, tmp_path):
    """The catalog links to panels and presets, not only to pages. A preset that
    survived would be a door into a section that is supposed to be shut."""
    module = _app(monkeypatch, tmp_path)
    operations = _client(module, department="maintenance")
    body = operations.get("/search?q=downtime").get_data(as_text=True)
    assert "/life-data-analysis/perform-analysis?analysis=" not in body
    assert "/metrics#" not in body


def test_search_keeps_them_for_a_department_that_is_not_withheld(monkeypatch, tmp_path):
    module = _app(monkeypatch, tmp_path)
    facilities = _client(module, department="facilities")
    assert "/metrics" in facilities.get("/search?q=metrics").get_data(as_text=True)
    assert "/configuration" in facilities.get("/search?q=configuration").get_data(as_text=True)


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
