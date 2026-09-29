"""The metrics page's tours: what they point at has to be on the page.

A tour finds each step's element by selector when the step is shown, and a
selector that matches nothing is not an error -- the page tour's step just
falls back to a centred card with nothing lit up, and the Availability tour
leaves the step out altogether. So renaming an id in metrics.html would quietly
turn a step into a caption about something the reader can't find, or drop it.
These tests are what notice.
"""

import importlib
import re
from pathlib import Path

import pytest

METRICS_JS = Path(__file__).resolve().parent.parent / "static" / "js" / "metrics.js"


def _app(monkeypatch, tmp_path):
    monkeypatch.setenv("GREMLIN_ACCESS_DB_PATH", str(tmp_path / "accesscontrol.db"))
    monkeypatch.setenv("GREMLIN_DB_PATH", str(tmp_path / "gremlin.db"))
    monkeypatch.setenv("GREMLIN_ADMIN_USERNAME", "root")
    monkeypatch.setenv("GREMLIN_ADMIN_PIN", "secret")
    import app

    return importlib.reload(app)


def _tour_targets(name="TOUR_STEPS"):
    source = METRICS_JS.read_text(encoding="utf-8")
    steps = re.search(rf"const {name} = \[(.*?)\n  \];", source, re.S)
    assert steps, f"{name} was not found in metrics.js"
    return re.findall(r'target: "([^"]+)"', steps.group(1))


def _split(selector):
    match = re.fullmatch(r"([#.])([\w-]+)", selector)
    assert match, f"{selector!r} is not a single id or class; this test can't check it"
    return match.groups()


def _first_use(page, kind, name):
    """Where document.querySelector would find it: the first match in the page."""
    if kind == "#":
        return page.find(f'id="{name}"')
    match = re.search(rf'class="[^"]*(?<![\w-]){re.escape(name)}(?![\w-])', page)
    return match.start() if match else -1


def _availability_card(page):
    start = page.find('id="card-availability"')
    assert start != -1
    return start, page.find("</article>", start)


@pytest.fixture
def metrics_page(monkeypatch, tmp_path):
    client = _app(monkeypatch, tmp_path).app.test_client()
    # The page is for signed-in users; which role doesn't change the markup.
    assert client.post("/auth/login", json={"username": "root", "pin": "secret"}).status_code == 200
    response = client.get("/metrics")
    assert response.status_code == 200
    return response.get_data(as_text=True)


def test_the_tour_has_steps():
    assert len(_tour_targets()) >= 3


@pytest.mark.parametrize("selector", _tour_targets())
def test_every_step_points_at_something_on_the_page(metrics_page, selector):
    kind, name = _split(selector)
    assert _first_use(metrics_page, kind, name) != -1


def test_the_tour_can_be_reopened(metrics_page):
    """It opens by itself only once, so the button is the only way back in."""
    assert 'id="metrics-tour-btn"' in metrics_page
    assert 'id="metrics-tour"' in metrics_page


def test_the_availability_tour_has_steps():
    assert len(_tour_targets("AVAILABILITY_TOUR_STEPS")) >= 5


@pytest.mark.parametrize("selector", _tour_targets("AVAILABILITY_TOUR_STEPS"))
def test_every_availability_step_points_inside_the_card(metrics_page, selector):
    """Each step lands on the Availability card, not on something that shares its name.

    Some targets are in the template; the charts, tables and group buttons are
    drawn into the card by metrics.js. querySelector takes the first match, so
    a template target has to be the first of its name on the page, and a drawn
    one must not also be used in the template ahead of it -- otherwise the step
    would light up (or, hidden, skip) something in another card.
    """
    kind, name = _split(selector)
    at = _first_use(metrics_page, kind, name)
    if at != -1:
        start, end = _availability_card(metrics_page)
        assert start <= at < end, f"{selector} is first used outside the Availability card"
        return
    assert kind == ".", f"{selector} is not on the page"
    source = METRICS_JS.read_text(encoding="utf-8")
    assert re.search(rf'class: "[^"]*(?<![\w-]){re.escape(name)}(?![\w-])', source), (
        f"{selector} is neither on the page nor put on anything by metrics.js"
    )


def test_the_availability_tour_can_be_started_from_the_card(metrics_page):
    start, end = _availability_card(metrics_page)
    assert start <= metrics_page.find('id="availability-tour-btn"') < end
