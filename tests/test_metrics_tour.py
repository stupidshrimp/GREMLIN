"""The metrics page's first-visit tour: what it points at has to be on the page.

The tour finds each step's element by selector when the step is shown, and a
selector that matches nothing is not an error -- the step just falls back to a
centred card with nothing lit up. So renaming an id in metrics.html would
quietly turn a step into a caption about something the reader can't find. These
tests are what notice.
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


def _tour_targets():
    source = METRICS_JS.read_text(encoding="utf-8")
    steps = re.search(r"const TOUR_STEPS = \[(.*?)\n  \];", source, re.S)
    assert steps, "TOUR_STEPS was not found in metrics.js"
    return re.findall(r'target: "([^"]+)"', steps.group(1))


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
    match = re.fullmatch(r"([#.])([\w-]+)", selector)
    assert match, f"{selector!r} is not a single id or class; this test can't check it"
    kind, name = match.groups()
    if kind == "#":
        assert f'id="{name}"' in metrics_page
    else:
        assert re.search(rf'class="[^"]*(?<![\w-]){re.escape(name)}(?![\w-])', metrics_page)


def test_the_tour_can_be_reopened(metrics_page):
    """It opens by itself only once, so the button is the only way back in."""
    assert 'id="metrics-tour-btn"' in metrics_page
    assert 'id="metrics-tour"' in metrics_page
