"""The topbar's tool rail: quick links, the theme toggle, and notifications.

base.html draws the topbar on every page, so these are mostly "is it still on all
of them" tests -- the kind of breakage that shows up as a missing button on one
route rather than as an exception anywhere.
"""

import importlib
import pathlib
import re

import pytest
from markupsafe import escape


def _app(monkeypatch, tmp_path):
    monkeypatch.setenv("GREMLIN_ACCESS_DB_PATH", str(tmp_path / "accesscontrol.db"))
    monkeypatch.setenv("GREMLIN_DB_PATH", str(tmp_path / "gremlin.db"))
    import app
    return importlib.reload(app)


# One page of each kind that base.html serves: the home hero, a form page, a
# footer page, and the links page.
PAGES = ["/", "/settings", "/about", "/reliability-links"]


@pytest.mark.parametrize("page", PAGES)
def test_every_page_carries_the_tool_rail(monkeypatch, tmp_path, page):
    body = _app(monkeypatch, tmp_path).app.test_client().get(page).get_data(as_text=True)
    assert 'class="topbar-tools"' in body
    for control in ["quickLinksButton", "themeToggle", "notificationsButton"]:
        assert f'id="{control}"' in body, control


@pytest.mark.parametrize("page", PAGES)
def test_every_page_carries_the_quick_links(monkeypatch, tmp_path, page):
    """The destinations come from a context processor rather than each view's
    arguments; a missing variable renders as an empty loop instead of raising,
    so this is what would catch that wiring being dropped."""
    module = _app(monkeypatch, tmp_path)
    body = module.app.test_client().get(page).get_data(as_text=True)
    for link in module.QUICK_LINKS:
        assert f'href="{link["url"]}"' in body, link["url"]
        # Escaped, because "S&C SourceOne" reaches the page as "S&amp;C
        # SourceOne" -- and a label that arrived unescaped would mean the
        # template had stopped escaping these, which is worth failing over.
        assert str(escape(link["label"])) in body, link["label"]


def test_quick_links_open_in_a_new_tab_without_handing_over_the_opener(monkeypatch, tmp_path):
    """`target="_blank"` without `rel="noopener"` gives the page that opens a
    handle on this one through window.opener. Every one of these leaves the app,
    so every one of them needs the guard."""
    body = _app(monkeypatch, tmp_path).app.test_client().get("/").get_data(as_text=True)
    menu = re.search(r'id="quickLinksMenu".*?</div>', body, re.S)
    assert menu, "the quick links menu was not rendered"

    anchors = re.findall(r"<a\b[^>]*>", menu.group(0))
    assert anchors, "the quick links menu rendered no links"
    for anchor in anchors:
        assert 'target="_blank"' in anchor, anchor
        assert "noopener" in anchor, anchor


def test_quick_links_are_all_https(monkeypatch, tmp_path):
    for link in _app(monkeypatch, tmp_path).QUICK_LINKS:
        assert link["url"].startswith("https://"), link["url"]


def test_quick_links_stay_out_of_the_sidebar_and_the_search_catalog(monkeypatch, tmp_path):
    """Both of those are catalogs of pages this app serves. An entry that
    silently takes you off-site does not belong in either -- see QUICK_LINKS."""
    module = _app(monkeypatch, tmp_path)
    quick_urls = {link["url"] for link in module.QUICK_LINKS}

    assert quick_urls.isdisjoint({link["url"] for link in module.NAV_LINKS})
    assert quick_urls.isdisjoint({link["url"] for link in module.FOOTER_LINKS})
    catalog = module._search_index(is_admin=True, can_edit=True, has_account=True)
    assert quick_urls.isdisjoint({entry["url"] for entry in catalog})


def test_the_theme_is_resolved_before_the_page_is_painted(monkeypatch, tmp_path):
    """The bootstrap has to run in <head>. Deferring it to topbar_tools.js at the
    end of <body> means the page renders light and then repaints, which is a
    white flash on every navigation for anyone using the dark theme."""
    body = _app(monkeypatch, tmp_path).app.test_client().get("/").get_data(as_text=True)
    head = body.index("</head>")
    bootstrap = body.index("gremlin.theme")
    assert bootstrap < head


def test_the_notifications_button_does_not_claim_a_panel_it_has_not_got(monkeypatch, tmp_path):
    """It is deliberately unwired for now. Until there is something behind it,
    it must not advertise itself as opening one: `aria-expanded` and
    `aria-controls` are promises to a screen reader that nothing would keep."""
    body = _app(monkeypatch, tmp_path).app.test_client().get("/").get_data(as_text=True)
    button = re.search(r"<button\b[^>]*id=\"notificationsButton\"[^>]*>", body, re.S)
    assert button, "the notifications button was not rendered"
    assert "aria-expanded" not in button.group(0)
    assert "aria-controls" not in button.group(0)


# --- the palette behind the toggle ------------------------------------------
# These read theme.css as text rather than rendering anything. A token defined
# in one theme and forgotten in the other is the classic dark-mode bug: nothing
# raises, the page just keeps the light value for that one colour and something
# comes out unreadable on a page nobody happened to open.

THEME_CSS = pathlib.Path(__file__).resolve().parent.parent / "static" / "css" / "theme.css"


def _declarations(selector):
    """The custom properties one selector's block declares."""
    css = THEME_CSS.read_text()
    start = css.index(selector + " {")
    return set(re.findall(r"(--[a-z0-9-]+)\s*:", css[start:css.index("}", start)]))


def test_both_themes_define_the_same_tokens():
    light = _declarations(":root")
    dark = _declarations(':root[data-theme="dark"]')
    assert light, "the light palette was not found in theme.css"
    assert light - dark == set(), f"missing from the dark theme: {sorted(light - dark)}"
    assert dark - light == set(), f"only in the dark theme: {sorted(dark - light)}"


def test_every_token_the_stylesheets_use_is_defined():
    """A `var(--typo)` with no fallback renders as nothing at all -- a missing
    background, or text the colour of whatever it is sitting on."""
    defined = _declarations(":root")
    used = {}
    for sheet in sorted(THEME_CSS.parent.glob("*.css")):
        for token in set(re.findall(r"var\(\s*(--[a-z0-9-]+)\s*\)", sheet.read_text())):
            if token not in defined:
                used.setdefault(sheet.name, []).append(token)
    assert used == {}, f"undefined tokens used without a fallback: {used}"
