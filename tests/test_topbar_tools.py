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
PAGES = ["/", "/configuration", "/about", "/reliability-links"]


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


def _theme_bootstrap(body):
    """The code of the inline <head> script that stamps data-theme before paint.

    Comments are stripped: what the script says about prefers-color-scheme and
    what it does with it are different things, and only the second one is what
    these tests are about.
    """
    head = body[: body.index("</head>")]
    start = head.rindex("<script>", 0, head.index("gremlin.theme"))
    script = head[start : head.index("</script>", start)]
    return "\n".join(re.sub(r"//.*", "", line) for line in script.splitlines())


def test_the_theme_is_resolved_before_the_page_is_painted(monkeypatch, tmp_path):
    """The bootstrap has to run in <head>. Deferring it to topbar_tools.js at the
    end of <body> means the page renders light and then repaints, which is a
    white flash on every navigation for anyone using the dark theme."""
    body = _app(monkeypatch, tmp_path).app.test_client().get("/").get_data(as_text=True)
    assert body.index("gremlin.theme") < body.index("</head>")


def test_the_theme_is_light_unless_dark_was_chosen(monkeypatch, tmp_path):
    """Light is the default and the machine's own setting does not override it:
    dark is somewhere you go, not somewhere an OS preference puts you. The only
    thing that selects it is the saved value from a press of the toggle."""
    body = _app(monkeypatch, tmp_path).app.test_client().get("/").get_data(as_text=True)
    bootstrap = _theme_bootstrap(body)

    assert "prefers-color-scheme" not in bootstrap
    # The whole rule, as one expression: dark only on a stored "dark".
    assert '"data-theme",' in bootstrap
    assert 'stored === "dark" ? "dark" : "light"' in bootstrap


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


# --- the charts -------------------------------------------------------------
# A canvas is the one thing on the page the cascade cannot reach: it is a bitmap,
# so it inherits nothing and keeps whatever colour it was painted in. Two rules
# hold it together, and neither shows up as an exception when broken -- the only
# symptom is an unreadable chart in one theme.

STATIC_JS = pathlib.Path(__file__).resolve().parent.parent / "static" / "js"
CHART_SCRIPTS = ["metrics.js", "life_data_analysis.js"]


@pytest.mark.parametrize("script", CHART_SCRIPTS)
def test_the_chart_scripts_hold_no_colours_of_their_own(script):
    """Every chart colour belongs in theme.css, read back out by chart_theme.js.

    One left behind in the JS is a colour the theme cannot reach: it will keep
    its light-theme value on a dark card, and nothing will say so.
    """
    source = (STATIC_JS / script).read_text()
    literals = sorted(set(re.findall(r'"#[0-9a-fA-F]{3,8}"', source)))
    assert literals == [], f"{script} hard-codes {literals}; add tokens to theme.css instead"


TEMPLATES = pathlib.Path(__file__).resolve().parent.parent / "templates"


@pytest.mark.parametrize(
    "template,script",
    [
        ("metrics.html", "metrics.js"),
        ("perform_analysis.html", "life_data_analysis.js"),
        ("disposition.html", "life_data_analysis.js"),
    ],
)
def test_chart_theme_is_ordered_before_the_script_that_uses_it(template, script):
    """Order matters here, not just presence: the chart scripts resolve the
    palette at load time, so chart_theme.js arriving second would throw on a
    missing global and take the whole page's script down with it.

    Read from the template rather than from a response because disposition.html
    is editor-only, and an anonymous request renders the 403 page instead.
    """
    source = (TEMPLATES / template).read_text()
    assert "chart_theme.js" in source, template
    assert source.index("chart_theme.js") < source.index(script), template


@pytest.mark.parametrize(
    "page,script",
    [
        ("/metrics", "metrics.js"),
        ("/life-data-analysis/perform-analysis", "life_data_analysis.js"),
    ],
)
def test_chart_pages_serve_the_palette_reader(monkeypatch, tmp_path, page, script):
    """And it survives rendering, for the pages a visitor can actually reach."""
    body = _app(monkeypatch, tmp_path).app.test_client().get(page).get_data(as_text=True)
    assert body.index("chart_theme.js") < body.index(script), page


# --- the theme wipe ---------------------------------------------------------
# Switching to dark grows a circle of the new theme out of the toggle, drawn over
# a pair of photographs the browser takes of the window either side of the swap.
# All three of those -- the circle, and both photographs -- are sized in the CSS
# pixels of one particular window, so any of them being measured for a *different*
# window is a visibly broken animation rather than an error: the wipe comes out of
# the wrong place, or stops short and leaves a band of the old theme around the
# edge until it snaps. Dragging the browser from one display to another is how
# that happens in practice -- a laptop panel and a desk monitor rarely agree on
# either the CSS size of a window or its device pixel ratio.

TOPBAR_JS = STATIC_JS / "topbar_tools.js"


def test_the_wipe_is_measured_against_the_window_that_was_photographed():
    """The reading has to happen before the browser is asked for the transition.

    `transition.ready` resolves a couple of frames after the request, and a
    display change landing in between means the two photographs are of two
    differently sized windows. Reading the window only inside `ready` cannot tell
    that apart from a window that never moved.
    """
    source = TOPBAR_JS.read_text()
    assert source.index("captured = viewport()") < source.index("startViewTransition")
    assert "viewport() !== captured" in source
    assert "skipTransition" in source


def test_the_wipe_notices_a_display_that_differs_only_in_scale():
    """A Retina laptop panel and a 1x desk monitor can report the same CSS size.

    Nothing about `innerWidth` and `innerHeight` moves in that case, so a check
    written on those alone would call the window unchanged while the browser
    re-rasterises both photographs at a different scale. `devicePixelRatio` is
    what separates the two, and `resolution` is the media feature that reports it
    changing -- a resize event is not guaranteed for it.
    """
    source = TOPBAR_JS.read_text()
    assert "devicePixelRatio" in source
    assert "dppx" in source and "resolution: " in source


def test_a_display_change_mid_wipe_ends_the_wipe():
    """The keyframes are fixed pixel lengths, and cannot be renegotiated.

    Once the window is a different size they describe a circle centred off the
    button and short of the corners, which would band the old theme around the
    edge for the rest of the run. Jumping to the end is the one outcome that
    cannot look wrong.
    """
    source = TOPBAR_JS.read_text()
    reveal = source[source.index("function reveal("):source.index("function switchTo(")]
    assert "onViewportChange" in reveal
    assert "animation.finish()" in reveal
    # Both signals, and both taken off again -- a listener left behind would
    # outlive its animation and finish an object nothing is drawing with.
    assert 'addEventListener("resize"' in source
    assert 'removeEventListener("resize"' in source
    assert "animation.finished" in reveal
