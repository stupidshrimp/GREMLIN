"""The Home, Perform an Analysis and Disposition tours: what they point at has to be on the page.

All three are driven by page_tour.js, which finds each step's element by
selector as the tour starts. The Home tour leaves out any step whose element
isn't drawn, and the analysis and Disposition tours, which pick an example asset
to draw the rest of the page, show such a step as a card pointing at nothing.
Either way a selector that matches nothing is not an error, so renaming an id in
a template would quietly drop or blank a step. These tests are what notice, the
same as test_metrics_tour.py does for Metrics.
"""

import importlib
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PAGE_TOUR_JS = ROOT / "static" / "js" / "page_tour.js"
HOME_JS = ROOT / "static" / "js" / "home.js"
ANALYSIS_JS = ROOT / "static" / "js" / "life_data_analysis.js"
DISPOSITION_JS = ROOT / "static" / "js" / "disposition.js"
DISPOSITION_FORM_JS = ROOT / "static" / "js" / "disposition_form.js"


def _app(monkeypatch, tmp_path):
    monkeypatch.setenv("GREMLIN_ACCESS_DB_PATH", str(tmp_path / "accesscontrol.db"))
    monkeypatch.setenv("GREMLIN_DB_PATH", str(tmp_path / "gremlin.db"))
    monkeypatch.setenv("GREMLIN_ADMIN_USERNAME", "root")
    monkeypatch.setenv("GREMLIN_ADMIN_PIN", "secret")
    import app

    return importlib.reload(app)


def _steps(path, declaration):
    source = path.read_text(encoding="utf-8")
    steps = re.search(rf"{re.escape(declaration)} = \[(.*?)\n  \];", source, re.S)
    assert steps, f"{declaration} was not found in {path.name}"
    return re.split(r"\n    \{\n", steps.group(1))[1:]


def _targets(path, declaration):
    source = path.read_text(encoding="utf-8")
    steps = re.search(rf"{re.escape(declaration)} = \[(.*?)\n  \];", source, re.S)
    assert steps, f"{declaration} was not found in {path.name}"
    return re.findall(r'target: "([^"]+)"', steps.group(1))


def _home_targets():
    return _targets(HOME_JS, "var STEPS")


def _analysis_targets():
    return _targets(ANALYSIS_JS, "const ANALYSIS_TOUR_STEPS")


def _disposition_targets():
    return _targets(DISPOSITION_JS, "const DISPOSITION_TOUR_STEPS")


def _on_page(page, selector):
    match = re.fullmatch(r"([#.])([\w-]+)", selector)
    assert match, f"{selector!r} is not a single id or class; this test can't check it"
    kind, name = match.groups()
    if kind == "#":
        return f'id="{name}"' in page
    return re.search(rf'class="[^"]*(?<![\w-]){re.escape(name)}(?![\w-])', page) is not None


def _drawn_by_script(source, selector):
    """Whether a script gives an element this id or class when it draws it."""
    kind, name = re.fullmatch(r"([#.])([\w-]+)", selector).groups()
    if kind == "#":
        return f'id: "{name}"' in source or f'"{name}"),' in source or f'"{name}")' in source
    return re.search(rf'class: "[^"]*(?<![\w-]){re.escape(name)}(?![\w-])', source) is not None


@pytest.fixture
def client(monkeypatch, tmp_path):
    client = _app(monkeypatch, tmp_path).app.test_client()
    # Signed in as the administrator, so every editor-only target -- the
    # Perform Analysis buttons -- is in the markup to be found.
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


@pytest.fixture
def disposition_page(client):
    response = client.get("/life-data-analysis/disposition")
    assert response.status_code == 200
    return response.get_data(as_text=True)


def test_the_home_tour_has_steps():
    assert len(_home_targets()) >= 5


@pytest.mark.parametrize("selector", _home_targets())
def test_every_home_step_points_at_something_on_the_page(home_page, selector):
    assert _on_page(home_page, selector)


def test_the_analysis_tour_has_steps():
    assert len(_analysis_targets()) >= 25


@pytest.mark.parametrize("selector", _analysis_targets())
def test_every_analysis_step_points_at_something_on_the_page_or_the_results(analysis_page, selector):
    """The results card is drawn by the script once a fit is shown, so a step
    about one of its parts points at an id renderAnalysisResult and the builders
    it calls give; every other target is in the served page."""
    source = ANALYSIS_JS.read_text(encoding="utf-8")
    assert _on_page(analysis_page, selector) or _drawn_by_script(source, selector), selector


def _panels_shown_by_analysis_type():
    """The ids applyAnalysisTypeUI shows for one Analysis Type and hides for the rest."""
    source = ANALYSIS_JS.read_text(encoding="utf-8")
    body = re.search(r"\n  function applyAnalysisTypeUI\(\) \{\n(.*?)\n  \}\n", source, re.S).group(1)
    shown = {}
    for ids, kind in re.findall(r'\[([^\]]+)\]\.forEach\(\(id\) => setHidden\(\$\(id\), !is\(ANALYSIS_TYPES\.(\w+)\)\)\)', body):
        for name in re.findall(r'"([\w-]+)"', ids):
            shown[name] = kind
    for name, kind in re.findall(r'setHidden\(\$\("([\w-]+)"\), !is\(ANALYSIS_TYPES\.(\w+)\)\)', body):
        shown[name] = kind
    assert shown, "applyAnalysisTypeUI no longer toggles panels by type; update this test"
    return shown


def test_each_analysis_type_panel_step_says_which_type_it_is_for():
    """With no asset picked none of the panels is drawn, and the tour keeps its
    steps anyway, so a step for one type's panel has to say so or it would
    appear, pointing at nothing, in every other type's tour."""
    shown = _panels_shown_by_analysis_type()
    checked = 0
    for step in _steps(ANALYSIS_JS, "const ANALYSIS_TOUR_STEPS"):
        target = re.search(r'target: "#([\w-]+)"', step)
        if not target or target.group(1) not in shown:
            continue
        checked += 1
        assert f"forType(ANALYSIS_TYPES.{shown[target.group(1)]})" in step, target.group(1)
    assert checked >= 8


def test_the_analysis_tour_opens_saved_fits_rather_than_running_them():
    """Performing an analysis saves a run; showing somebody around must not."""
    source = ANALYSIS_JS.read_text(encoding="utf-8")
    for action in ("TOUR_EXAMPLE_ACTION", "TOUR_FIT_ACTION"):
        body = re.search(rf"const {action} = \{{(.*?)\n  \}};", source, re.S)
        assert body, f"{action} was not found"
        assert "performAnalysis" not in body.group(1)
        assert "weibull/save" not in body.group(1)
        assert "openRankedFit" in body.group(1)


def test_every_documentation_link_in_the_analysis_tour_opens_a_part_of_it(client):
    """A fragment that names nothing opens the Documentation page at its top,
    so a renamed section would quietly send a card's link nowhere in particular."""
    source = ANALYSIS_JS.read_text(encoding="utf-8")
    url = re.search(r'const DOCS_URL = "([^"]+)";', source)
    assert url, "DOCS_URL was not found"
    response = client.get(url.group(1))
    assert response.status_code == 200
    page = response.get_data(as_text=True)
    fragments = sorted(set(re.findall(r'docsLink\("([\w-]+)"', source)))
    assert len(fragments) >= 5
    for fragment in fragments:
        assert f'id="{fragment}"' in page, f"#{fragment} isn't on the Documentation page"


def test_the_disposition_tour_has_steps():
    assert len(_disposition_targets()) >= 8


@pytest.mark.parametrize("selector", _disposition_targets())
def test_every_disposition_step_points_at_something_on_the_page_or_the_form(disposition_page, selector):
    """The disposition window's form is built by disposition_form.js once a work
    order is picked, so a step about one of its parts points at a class the form
    gives; every other target is in the served page."""
    source = DISPOSITION_FORM_JS.read_text(encoding="utf-8")
    assert _on_page(disposition_page, selector) or _drawn_by_script(source, selector), selector


def test_the_disposition_tour_picks_an_example_before_the_windows():
    """With no asset picked the two windows are hidden, so the asset step picks one."""
    [asset_step] = [step for step in _steps(DISPOSITION_JS, "const DISPOSITION_TOUR_STEPS") if '"#lda-asset-field"' in step]
    assert "action: TOUR_PICK_EXAMPLE" in asset_step
    assert "/disposition-tour-example" in DISPOSITION_JS.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "page_name, button, own_script",
    [
        ("home_page", "home-tour-btn", "js/home.js"),
        ("analysis_page", "analysis-tour-btn", "js/life_data_analysis.js"),
        ("disposition_page", "disposition-tour-btn", "js/disposition.js"),
    ],
)
def test_each_tour_can_be_reopened_and_has_its_overlay(request, page_name, button, own_script):
    """It opens by itself only once, so the button is the only way back in."""
    page = request.getfixturevalue(page_name)
    assert f'id="{button}"' in page
    assert page.count('id="page-tour"') == 1
    assert "js/page_tour.js" in page
    assert "css/page_tour.css" in page
    # The steps are handed over by the page's own script, so the engine has to
    # be loaded ahead of it.
    assert page.index("js/page_tour.js") < page.index(own_script)


@pytest.mark.parametrize("page_name", ["home_page", "analysis_page", "disposition_page"])
def test_the_overlay_has_everything_the_engine_looks_up(request, page_name):
    page = request.getfixturevalue(page_name)
    ids = set(re.findall(r'\$\("(page-tour[\w-]*)"\)', PAGE_TOUR_JS.read_text(encoding="utf-8")))
    assert ids, "page_tour.js no longer looks anything up by id; update this test"
    for name in sorted(ids):
        assert f'id="{name}"' in page, f"page_tour.js looks up #{name}, which isn't on the page"
