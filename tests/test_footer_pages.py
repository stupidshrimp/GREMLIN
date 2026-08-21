import importlib

import pytest


def _app(monkeypatch, tmp_path):
    monkeypatch.setenv("GREMLIN_ACCESS_DB_PATH", str(tmp_path / "accesscontrol.db"))
    monkeypatch.setenv("GREMLIN_DB_PATH", str(tmp_path / "gremlin.db"))
    import app
    return importlib.reload(app)


FOOTER_ROUTES = ["/about", "/patch-notes", "/report-a-bug"]


def test_footer_pages_are_readable_without_logging_in(monkeypatch, tmp_path):
    """The footer is drawn for every visitor, so nothing it links to may need an account."""
    client = _app(monkeypatch, tmp_path).app.test_client()
    for route in FOOTER_ROUTES:
        assert client.get(route).status_code == 200, route


@pytest.mark.parametrize("page", ["/", "/configuration", "/reliability-links", "/about"])
def test_every_page_carries_the_footer(monkeypatch, tmp_path, page):
    """base.html includes the footer, so a page that extends it cannot be missing one.

    The links come from a context processor rather than each view's arguments; this is
    what would catch that wiring being dropped, since a missing variable renders as an
    empty loop instead of raising.
    """
    body = _app(monkeypatch, tmp_path).app.test_client().get(page).get_data(as_text=True)
    assert 'role="contentinfo"' in body
    for route in FOOTER_ROUTES:
        assert f'href="{route}"' in body, route


def test_footer_pages_stay_out_of_the_sidebar(monkeypatch, tmp_path):
    """They are reached from the footer and nowhere else -- see FOOTER_LINKS."""
    module = _app(monkeypatch, tmp_path)
    nav_urls = {link["url"] for link in module.NAV_LINKS}
    assert nav_urls.isdisjoint(FOOTER_ROUTES)
