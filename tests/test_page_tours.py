"""The Home and Perform an Analysis tours: what they point at has to be on the page.

Both are driven by page_tour.js, which finds each step's element by selector as
the tour starts. Home's tour leaves out any step whose element isn't drawn, and
the analysis tour, which picks an example asset to draw the rest of the page,
shows such a step as a card pointing at nothing. Either way a selector that
matches nothing is not an error, so renaming an id in a template would quietly
drop or blank a step. These tests are what notice, the same as
test_metrics_tour.py does for Metrics.
"""

import importlib
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PAGE_TOUR_JS = ROOT / "static" / "js" / "page_tour.js"
HOME_JS = ROOT / "static" / "js" / "home.js"
ANALYSIS_JS = ROOT / "static" / "js" / "life_data_analysis.js"


def _app(monkeypatch, tmp_path):
    monkeypatch.setenv("GREMLIN_ACCESS_DB_PATH", str(tmp_path / "accesscontrol.db"))
    monkeypatch.setenv("GREMLIN_DB_PATH", str(tmp_path / "gremlin.db"))
    monkeypatch.setenv("GREMLIN_ADMIN_USERNAME", "root")
    monkeypatch.setenv("GREMLIN_ADMIN_PIN", "secret")
    import app

    return importlib.reload(app)


def _targets(path, declaration):
    source = path.read_text(encoding="utf-8")
    steps = re.search(rf"{re.escape(declaration)} = \[(.*?)\n  \];", source, re.S)
    assert steps, f"{declaration} was not found in {path.name}"
    return re.findall(r'target: "([^"]+)"', steps.group(1))


def _home_targets():
    return _targets(HOME_JS, "var STEPS")


def _analysis_targets():
    return (
        _targets(ANALYSIS_JS, "const ANALYSIS_SETUP_TOUR_STEPS")
        + _targets(ANALYSIS_JS, "const ANALYSIS_RESULTS_TOUR_STEPS")
        + _targets(ANALYSIS_JS, "const ANALYSIS_TOUR_END_STEPS")
    )


def _on_page(page, selector):
    match = re.fullmatch(r"([#.])([\w-]+)", selector)
    assert match, f"{selector!r} is not a single id or class; this test can't check it"
    kind, name = match.groups()
    if kind == "#":
        return f'id="{name}"' in page
    return re.search(rf'class="[^"]*(?<![\w-]){re.escape(name)}(?![\w-])', page) is not None


@pytest.fixture
def client(monkeypatch, tmp_path):
    client = _app(monkeypatch, tmp_path).app.test_client()
    # Signed in as the administrator, so every editor-only target -- the
    # Perform Analysis buttons, Calculate all -- is in the markup to be found.
    assert client.post("/auth/login", json={"username": "root", "pin": "secret"}).status_code == 200
    return client


@pytest.fixture
def home_page(client):
    response = client.get("/")
    assert response.status_code == 200
    return response.get_data(as_text=True)


@pytest.fixture
def analysis_page(client):
    response = client.get("/life-data-analysis/perform-analysis")
    assert response.status_code == 200
    return response.get_data(as_text=True)


def test_the_home_tour_has_steps():
    assert len(_home_targets()) >= 5


@pytest.mark.parametrize("selector", _home_targets())
def test_every_home_step_points_at_something_on_the_page(home_page, selector):
    assert _on_page(home_page, selector)


def test_the_analysis_tour_has_steps():
    assert len(_targets(ANALYSIS_JS, "const ANALYSIS_SETUP_TOUR_STEPS")) >= 2
    assert len(_targets(ANALYSIS_JS, "const ANALYSIS_RESULTS_TOUR_STEPS")) >= 5


@pytest.mark.parametrize("selector", _analysis_targets())
def test_every_analysis_step_points_at_something_on_the_page(analysis_page, selector):
    assert _on_page(analysis_page, selector)


def _analysis_steps(declaration):
    source = ANALYSIS_JS.read_text(encoding="utf-8")
    steps = re.search(rf"{re.escape(declaration)} = \[(.*?)\n  \];", source, re.S)
    assert steps, f"{declaration} was not found in {ANALYSIS_JS.name}"
    return re.split(r"\n    \{\n", steps.group(1))[1:]


def _panels_shown_by_analysis_type():
    """The ids applyAnalysisTypeUI shows for one Analysis Type and hides for the rest."""
    source = ANALYSIS_JS.read_text(encoding="utf-8")
    shown = dict(re.findall(r'setHidden\(\$\("([\w-]+)"\), !is(\w+)\);', source))
    assert shown, "applyAnalysisTypeUI no longer toggles panels by type; update this test"
    return shown


def test_each_analysis_type_panel_step_says_which_type_it_is_for():
    """The tour can't rely on what is drawn to tell it which panels apply.

    With no asset picked none of them is, and the tour keeps its steps anyway so
    the example it picks has something to be shown on. So a step for one Analysis
    Type's panel has to say so, or it would appear, pointing at nothing, in every
    other type's tour.
    """
    types = {"Weibull": "WEIBULL", "Trend": "TREND", "Pm": "PM", "Downtime": "DOWNTIME"}
    shown = _panels_shown_by_analysis_type()
    checked = 0
    for step in _analysis_steps("const ANALYSIS_RESULTS_TOUR_STEPS"):
        target = re.search(r'target: "#([\w-]+)"', step).group(1)
        if target not in shown:
            continue
        checked += 1
        assert f"when: forType(ANALYSIS_TYPES.{types[shown[target]]})" in step, target
    assert checked >= 8


def test_the_analysis_tour_opens_saved_fits_rather_than_running_them():
    """An editor's Pareto click runs and stores a fit; the tour's must not."""
    source = ANALYSIS_JS.read_text(encoding="utf-8")
    action = re.search(r"const TOUR_MECHANISM_ACTION = \{(.*?)\n  \};", source, re.S)
    assert action, "TOUR_MECHANISM_ACTION was not found"
    assert "runParetoMechanism(row, { savedOnly: true })" in action.group(1)


@pytest.mark.parametrize("page_name, button", [("home_page", "home-tour-btn"), ("analysis_page", "analysis-tour-btn")])
def test_each_tour_can_be_reopened_and_has_its_overlay(request, page_name, button):
    """It opens by itself only once, so the button is the only way back in."""
    page = request.getfixturevalue(page_name)
    assert f'id="{button}"' in page
    assert page.count('id="page-tour"') == 1
    assert "js/page_tour.js" in page
    assert "css/page_tour.css" in page
    # The steps are handed over by the page's own script, so the engine has to
    # be loaded ahead of it.
    own_script = "js/home.js" if page_name == "home_page" else "js/life_data_analysis.js"
    assert page.index("js/page_tour.js") < page.index(own_script)


@pytest.mark.parametrize("page_name", ["home_page", "analysis_page"])
def test_the_overlay_has_everything_the_engine_looks_up(request, page_name):
    page = request.getfixturevalue(page_name)
    ids = set(re.findall(r'\$\("(page-tour[\w-]*)"\)', PAGE_TOUR_JS.read_text(encoding="utf-8")))
    assert ids, "page_tour.js no longer looks anything up by id; update this test"
    for name in sorted(ids):
        assert f'id="{name}"' in page, f"page_tour.js looks up #{name}, which isn't on the page"
