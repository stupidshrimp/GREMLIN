"""The About page: the animation it shares with Home, and the links it offers.

The page is mostly copy, which tests have nothing useful to say about. What they
can hold on to is the wiring underneath it -- the three-part pairing that makes
the S&C mark stop instead of looping, and the promise that a visitor who is not
signed in can open everything the page points at.
"""

import importlib
import pathlib
import re

import pytest


def _app(monkeypatch, tmp_path):
    monkeypatch.setenv("GREMLIN_ACCESS_DB_PATH", str(tmp_path / "accesscontrol.db"))
    monkeypatch.setenv("GREMLIN_DB_PATH", str(tmp_path / "gremlin.db"))
    import app
    return importlib.reload(app)


REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]

# Both pages that open on the mark. They are tested together on purpose: the
# hold time and the still frame are shared, so the two either agree or the swap
# is visibly wrong on one of them.
ANIMATED_PAGES = ["/", "/about"]

GIF = "/static/img/sc_loading_animation.gif"
STILL = "/static/img/sc_loading_animation_still.png"


def _main_content(body: str) -> str:
    """The page's own markup, without the topbar, sidebar and footer around it."""
    main = re.search(r'<main class="content" role="main">(.*?)</main>', body, re.S)
    assert main, "the page rendered no main landmark"
    return main.group(1)


@pytest.mark.parametrize("page", ANIMATED_PAGES)
def test_the_mark_ships_with_everything_needed_to_stop_it(monkeypatch, tmp_path, page):
    """A GIF cannot be paused, so the hold takes all three of these.

    Drop the still and the animation loops forever; drop the script and the same.
    Neither failure raises anything -- the page renders and just keeps moving --
    which is why it is asserted here rather than left to be noticed.
    """
    body = _app(monkeypatch, tmp_path).app.test_client().get(page).get_data(as_text=True)
    assert GIF in body
    assert f'data-still-src="{STILL}"' in body
    assert "js/held_animation.js" in body


def test_the_held_frame_is_actually_on_disk():
    """`url_for` builds a path whether or not the file behind it exists, and a
    still that 404s at the cutoff blanks the artwork rather than settling it."""
    assert (REPO_ROOT / "static" / "img" / "sc_loading_animation_still.png").is_file()


def test_the_hold_lands_before_the_animation_fades_out(monkeypatch, tmp_path):
    """The GIF fades to an empty frame from 2560 ms so that it can loop.

    Hold after that and the page settles on nothing at all. The comment in
    held_animation.js explains the window; this is what fails if somebody moves
    the number past the end of it without re-cutting the still.
    """
    script = (REPO_ROOT / "static" / "js" / "held_animation.js").read_text()
    hold_ms = re.search(r"var HOLD_MS = (\d+);", script)
    assert hold_ms, "held_animation.js no longer declares a hold time"
    assert 1560 <= int(hold_ms.group(1)) <= 2560


def test_about_only_points_at_pages_a_visitor_can_open(monkeypatch, tmp_path):
    """Nobody is signed in on the About page, so nothing it offers may need to be.

    Disposition is the one workflow that does -- it takes the editor role -- and
    the page says so in a sentence instead of linking to a 403.
    """
    client = _app(monkeypatch, tmp_path).app.test_client()
    content = _main_content(client.get("/about").get_data(as_text=True))

    routes = re.findall(r'href="(/[^"#]*)"', content)
    assert routes, "the About page offered no links at all"
    for route in routes:
        assert client.get(route).status_code == 200, route


def test_about_says_who_built_it(monkeypatch, tmp_path):
    """The page exists partly to carry the credit, so it is worth failing over."""
    body = _app(monkeypatch, tmp_path).app.test_client().get("/about").get_data(as_text=True)
    for phrase in ["GREMLINS", "Process Reliability", "704"]:
        assert phrase in body, phrase
