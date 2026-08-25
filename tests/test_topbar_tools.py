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
    """Pressing it raises a toast, not a panel. `aria-expanded` and
    `aria-controls` are promises to a screen reader that something opens and
    stays open, and neither would be kept -- so they stay off until there is a
    real feed behind the button to open."""
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
# Switching themes grows a circle of the new theme out of the toggle, drawn onto
# ::view-transition-new(root) -- a snapshot the browser owns rather than any part
# of this page. A pixel in there is not obliged to be a pixel out here, and on a
# display running at 250% it is not: a shape given in this page's pixels comes out
# scaled towards the top left corner. That put the circle in the top middle
# instead of the top right and left the radius about 39% of what it needed, so the
# wipe stalled partway and the rest of the screen arrived at once when the clip
# reverted. Percentages resolve against the snapshot's own box, which is the one
# thing both spaces agree on.

TOPBAR_JS = STATIC_JS / "topbar_tools.js"


def _reveal():
    """The body of reveal(), which is the only place the circle is measured."""
    source = TOPBAR_JS.read_text()
    return source[source.index("function reveal("):source.index("function switchTo(")]


def test_the_circle_is_given_in_the_snapshot_s_own_units():
    """A length in this page's pixels means nothing to the snapshot it is handed
    to. This is the whole bug, and `px at` is exactly what it looked like."""
    reveal = _reveal()
    assert "% at " in reveal, "the circle has to be placed in percentages"
    assert "px at " not in reveal, "a page pixel handed to the snapshot is the bug"


def test_the_radius_is_converted_against_the_diagonal_and_not_a_side():
    """CSS resolves a percentage radius against `hypot(w, h) / sqrt(2)` -- not
    against the width, and not against the diagonal on its own.

    Converting against either of those instead still looks plausible and still
    animates; it just quietly stops short of the corner on most window shapes,
    which is the same failure this whole section exists to prevent.
    """
    reveal = _reveal()
    assert "Math.SQRT2" in reveal
    assert "Math.hypot(width, height)" in reveal


def test_the_reach_is_measured_to_the_furthest_corner():
    """The button sits in a corner, so the circle has to cross the whole diagonal
    to clear the window -- the nearer corner is not what it has to reach."""
    reveal = _reveal()
    assert "Math.max(x, width - x)" in reveal
    assert "Math.max(y, height - y)" in reveal


def test_the_window_is_measured_once():
    """`innerWidth` is read for the reach and again for the conversion. Reading it
    fresh each time is how the two halves end up describing different windows if
    anything moves in between -- which, on a laptop being docked and undocked, is
    a thing that happens."""
    reveal = _reveal()
    assert reveal.count("window.innerWidth") == 1
    assert reveal.count("window.innerHeight") == 1


# --- the notifications button -----------------------------------------------
# There is no feed behind the bell, so a press is answered with a toast saying
# the feature is not built yet. None of that can be reached by rendering a page
# -- it is a click handler -- so these read the sources, the way the section
# above does. What they guard is that the wiring is still there at all, and that
# the toast does not come out dressed as an error.

SIDEBAR_CSS = THEME_CSS.parent / "sidebar.css"
LAYOUT_JS = STATIC_JS / "layout.js"


def _notifications():
    """The code of the block in topbar_tools.js that wires up the bell.

    Comments stripped, the way _theme_bootstrap above strips them: this section
    explains itself at some length, and a test that a guard is still *there*
    passes just as happily on the sentence describing the guard.
    """
    source = TOPBAR_JS.read_text()
    block = source[source.index('getElementById("notificationsButton")'):]
    return "\n".join(re.sub(r"//.*", "", line) for line in block.splitlines())


def test_pressing_the_bell_says_the_feature_is_not_built_yet():
    """A button that swallows every press cannot be told apart from a broken
    one, and this is a button people press. Until there is a feed behind it, the
    press has to produce something that says so."""
    handler = _notifications()
    assert "addEventListener" in handler, "the button is wired to nothing"
    assert "window.gremlinToast(" in handler, "the press raises no toast"
    assert re.search(r'"[^"]*under development[^"]*"', handler), \
        "the toast does not say the feature is under development"


def test_the_bells_toast_is_not_dressed_as_an_error():
    """`.gremlin-toast` on its own is the red one, because every caller of the
    helper that predates this button is reporting a write the server refused. A
    note about a feature that does not exist yet is not a failure and must not
    arrive looking like one -- which takes both halves: a kind passed here, and
    a rule behind it in the stylesheet. Miss the second and it renders red."""
    assert 'window.gremlinToast(MESSAGE, "info")' in _notifications()
    assert ".gremlin-toast.is-info" in SIDEBAR_CSS.read_text(), \
        "the info variant has no rule behind it, so the toast comes out red"


def test_the_toast_helper_still_defaults_to_the_alarmed_look():
    """The kind is optional and every caller predating it leaves it off -- see
    metrics.js, life_data_analysis.js, availability_config.js, home.js and
    configuration.html, all of them reporting a refused write. Making the quiet
    look the default would quietly soften every one of those."""
    assert 'toast.className = "gremlin-toast" + (kind ? " is-" + kind : "");' in LAYOUT_JS.read_text()


def test_the_bell_does_not_stack_the_same_sentence_up_the_screen():
    """The host keeps every toast it is given on screen at once, so a second
    press while the first is still up leaves two identical notes, and a fifth
    leaves five."""
    assert "isConnected" in _notifications(), "nothing stops a press from stacking another toast"


def test_the_bell_keeps_no_copy_of_how_long_a_toast_lasts():
    """That timing belongs to gremlinToast. A copy of it here would go stale the
    first time it changed there, and the symptom would be a button that ignores
    a press for a while -- the very thing this section exists to prevent."""
    assert "5000" not in _notifications()
