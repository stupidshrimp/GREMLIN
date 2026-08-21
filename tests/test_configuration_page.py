"""The Configuration page: its name, its old address, and its collapsed panels."""

import importlib
import re

import pytest


def _app(monkeypatch, tmp_path):
    monkeypatch.setenv("GREMLIN_ACCESS_DB_PATH", str(tmp_path / "accesscontrol.db"))
    monkeypatch.setenv("GREMLIN_DB_PATH", str(tmp_path / "gremlin.db"))
    import app
    return importlib.reload(app)


def _editor_client(module):
    module.access_control.save_user(None, "editor", "2468", "editor")
    client = module.app.test_client()
    assert client.post("/auth/login", json={"username": "editor", "pin": "2468"}).status_code == 200
    return client


# --- the rename -------------------------------------------------------------

def test_the_page_is_called_configuration_everywhere_it_is_named(monkeypatch, tmp_path):
    module = _app(monkeypatch, tmp_path)
    body = module.app.test_client().get("/configuration").get_data(as_text=True)
    assert "<title>Configuration</title>" in body
    # The sidebar entry comes from PAGES, which is also what the nav is built
    # from, so this covers the tooltip the collapsed rail shows as well.
    assert '<span class="nav-label">Configuration</span>' in body
    assert {link["url"] for link in module.NAV_LINKS} >= {"/configuration"}
    assert "/settings" not in {link["url"] for link in module.NAV_LINKS}


def test_the_old_settings_address_still_lands_on_the_page(monkeypatch, tmp_path):
    """Bookmarks outlive a rename, so /settings redirects rather than 404ing."""
    client = _app(monkeypatch, tmp_path).app.test_client()
    response = client.get("/settings")
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/configuration")
    assert client.get("/settings", follow_redirects=True).status_code == 200


def test_searching_the_old_name_still_finds_the_page(monkeypatch, tmp_path):
    """"settings" is what people will keep typing, so it stays a keyword."""
    module = _app(monkeypatch, tmp_path)
    index = module._search_index(is_admin=False, can_edit=False, has_account=False)
    assert module._search_matches("settings", index)[0]["label"] == "Configuration"


# --- the panels -------------------------------------------------------------

PANELS = ["config-availability", "config-linked", "config-cmms"]


def test_every_reliability_panel_ships_collapsed(monkeypatch, tmp_path):
    """The whole point of the restructure: the page opens as a menu, not a wall.

    A `<details>` is open exactly when the attribute is present, so this is the
    one thing that would silently undo the change -- the page would still look
    modular in the markup and still render everything at once in the browser.
    """
    body = _editor_client(_app(monkeypatch, tmp_path)).get("/configuration").get_data(as_text=True)
    for panel in PANELS:
        match = re.search(r"<details\b[^>]*\bid=\"%s\"[^>]*>" % panel, body)
        assert match, panel
        assert " open" not in match.group(0), panel


def test_search_fragments_name_panels_the_page_actually_has(monkeypatch, tmp_path):
    """The catalog links into these panels by id; a rename here would go quiet.

    Nothing raises when a fragment names an element that is not there -- the
    browser just lands at the top of the page with the panel still shut.
    """
    module = _app(monkeypatch, tmp_path)
    body = _editor_client(module).get("/configuration").get_data(as_text=True)
    fragments = [
        entry["url"].split("#", 1)[1]
        for entry in module.SEARCH_ENTRIES
        if entry["url"].startswith("/configuration#")
    ]
    assert sorted(fragments) == sorted(PANELS)
    for fragment in fragments:
        assert f'id="{fragment}"' in body, fragment


@pytest.mark.parametrize("panel", PANELS)
def test_a_reader_who_may_not_edit_gets_no_panel_at_all(monkeypatch, tmp_path, panel):
    body = _app(monkeypatch, tmp_path).app.test_client().get("/configuration").get_data(as_text=True)
    assert f'id="{panel}"' not in body
    # But the section still names itself, so the page does not read as though
    # GREMLIN has no such configuration.
    assert "Reliability configuration" in body
