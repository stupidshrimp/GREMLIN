"""The About page: the animation it shares with Home, and the links it offers.

The page is mostly copy, which tests have nothing useful to say about. What they
can hold on to is the wiring underneath it -- the three-part pairing that makes
the S&C mark stop instead of looping, and the promise that every link the page
offers lands on a page rather than on a 404.
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


def test_about_points_at_pages_that_exist(monkeypatch, tmp_path):
    """About is a footer page, so a visitor reads it; every link on it has to land.

    An editor opens all of them. A visitor opens the ones GREMLIN keeps open and
    is told to log in for the rest -- which is a page saying so, not a dead end,
    and is the only other answer allowed here. A 404 or a 500 is a broken link
    either way.
    """
    module = _app(monkeypatch, tmp_path)
    module.access_control.save_user(None, "editor", "2468", "editor")
    visitor = module.app.test_client()
    editor = module.app.test_client()
    assert editor.post("/auth/login", json={"username": "editor", "pin": "2468"}).status_code == 200

    content = _main_content(visitor.get("/about").get_data(as_text=True))
    routes = re.findall(r'href="(/[^"#]*)"', content)
    assert routes, "the About page offered no links at all"
    for route in routes:
        assert editor.get(route).status_code == 200, route
        refused = visitor.get(route)
        assert refused.status_code in (200, 403), route
        if refused.status_code == 403:
            assert b"needs an account" in refused.data, route


def test_about_says_who_built_it(monkeypatch, tmp_path):
    """The page exists partly to carry the credit, so it is worth failing over."""
    body = _app(monkeypatch, tmp_path).app.test_client().get("/about").get_data(as_text=True)
    for phrase in ["GREMLINS", "Process Reliability", "704"]:
        assert phrase in body, phrase
