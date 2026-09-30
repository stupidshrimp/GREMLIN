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


def _disposition_page_targets():
    return _targets(ANALYSIS_JS, "const DISPOSITION_SETUP_TOUR_STEPS") + _targets(
        ANALYSIS_JS, "const DISPOSITION_TOUR_END_STEPS"
    )


def _disposition_editor_targets():
    return _targets(ANALYSIS_JS, "const DISPOSITION_EDITOR_TOUR_STEPS")


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


def test_no_analysis_step_describes_a_mechanism_before_the_tour_shows_one():
    """A step about "the selected mechanism" needs one on screen to point at.

    Started with none picked, the tour only shows one at the first step carrying
    the mechanism action, so a step before it would light empty cards while
    describing their numbers.
    """
    steps = _analysis_steps("const ANALYSIS_RESULTS_TOUR_STEPS")

    def type_of(step):
        match = re.search(r"when: forType\(ANALYSIS_TYPES\.(\w+)\)", step)
        return match.group(1) if match else None

    for analysis_type in ("WEIBULL", "TREND", "PM", "DOWNTIME"):
        own = [step for step in steps if type_of(step) in (analysis_type, None)]
        shows = [i for i, step in enumerate(own) if "action: TOUR_MECHANISM_ACTION" in step]
        describes = [i for i, step in enumerate(own) if "selected mechanism" in step]
        if describes:
            assert shows and shows[0] <= describes[0], analysis_type


def test_the_analysis_tour_opens_saved_fits_rather_than_running_them():
    """An editor's Pareto click runs and stores a fit; the tour's must not."""
    source = ANALYSIS_JS.read_text(encoding="utf-8")
    action = re.search(r"const TOUR_MECHANISM_ACTION = \{(.*?)\n  \};", source, re.S)
    assert action, "TOUR_MECHANISM_ACTION was not found"
    assert "runParetoMechanism(row, { savedOnly: true })" in action.group(1)


def test_the_disposition_tour_has_steps():
    assert len(_targets(ANALYSIS_JS, "const DISPOSITION_SETUP_TOUR_STEPS")) >= 3
    assert len(_disposition_editor_targets()) >= 5


@pytest.mark.parametrize("selector", _disposition_page_targets())
def test_every_disposition_step_points_at_something_on_the_page(disposition_page, selector):
    assert _on_page(disposition_page, selector)


def _function_body(name):
    source = ANALYSIS_JS.read_text(encoding="utf-8")
    body = re.search(rf"\n  function {name}\([^)]*\) \{{\n(.*?)\n  \}}\n", source, re.S)
    assert body, f"{name} was not found in life_data_analysis.js"
    return body.group(1)


def _disposition_editor_source():
    return _function_body("renderDispositionEditor")


@pytest.mark.parametrize("selector", _disposition_editor_targets())
def test_every_disposition_editor_step_points_at_something_the_editor_draws(selector):
    """The editor is built by the script once an asset is picked, so its targets
    aren't in the served page: they are the ids renderDispositionEditor gives."""
    match = re.fullmatch(r"#([\w-]+)", selector)
    assert match, f"{selector!r} is not a single id; this test can't check it"
    assert f'id: "{match.group(1)}"' in _disposition_editor_source()


def test_the_disposition_editor_offers_its_tour_once_drawn():
    assert "offerDispositionTour();" in _disposition_editor_source()


def test_the_disposition_tour_picks_an_example_for_the_rows_showing():
    """With no asset picked the editor steps have nothing to point at, so the
    Asset Number step picks one -- whose table has rows for the Record Type, Rows
    and search the page is set to, or the tour would light an empty one."""
    [asset_step] = [
        step for step in _analysis_steps("const DISPOSITION_SETUP_TOUR_STEPS") if '"#lda-asset-field"' in step
    ]
    assert 'label: "Pick an example ►"' in asset_step
    # A search still in its debounce is applied first, or the example would be
    # picked for the last search and its table reloaded for the new one.
    assert asset_step.index("await flushDispositionSearch();") < asset_step.index(
        "return pickTourExampleAsset(dispositionTourExampleUrl());"
    )
    assert "flushDispositionSearch = () => {" in _function_body("initDispositionPage")
    url = _function_body("dispositionTourExampleUrl")
    assert "/disposition-tour-example?" in url
    for control in ("state.dispositionKind", "state.dispositionScope", "state.dispositionSearch"):
        assert control in url, control


def test_the_disposition_editor_leaves_the_scrolling_to_an_open_tour():
    """The tour draws the editor for its example, then shows the Asset Number
    step again; the editor's own glide into view would carry that step away."""
    source = _disposition_editor_source()
    guard = source.index("if (!(window.gremlinTour && window.gremlinTour.isOpen())) {")
    assert guard < source.index("card.scrollIntoView(")


@pytest.mark.parametrize(
    "function",
    [
        "endLoading",  # the loading veil
        "openModal",  # the unsaved-changes question
        "closeAssetDropdown",  # the Asset Number list
        "buildTaxonomyCombobox",  # a failure mode / mechanism list in the table
        "closeColumnMenu",  # a column's menu
        "wireExcelHelpDialog",  # the Excel explainer
        "initDispositionPage",  # the search box
        "wireDispositionTour",  # a pointer held down
        "startDispositionTour",  # another tour
    ],
)
def test_an_owed_disposition_tour_is_tried_again_when_its_way_clears(function):
    """A tour that offers itself while something is in its way is owed, not
    dropped, and only starts if what cleared the way asks for it again."""
    assert "startOwedDispositionTour" in _function_body(function)


@pytest.mark.parametrize(
    "page_name, button",
    [
        ("home_page", "home-tour-btn"),
        ("analysis_page", "analysis-tour-btn"),
        ("disposition_page", "disposition-tour-btn"),
    ],
)
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


@pytest.mark.parametrize("page_name", ["home_page", "analysis_page", "disposition_page"])
def test_the_overlay_has_everything_the_engine_looks_up(request, page_name):
    page = request.getfixturevalue(page_name)
    ids = set(re.findall(r'\$\("(page-tour[\w-]*)"\)', PAGE_TOUR_JS.read_text(encoding="utf-8")))
    assert ids, "page_tour.js no longer looks anything up by id; update this test"
    for name in sorted(ids):
        assert f'id="{name}"' in page, f"page_tour.js looks up #{name}, which isn't on the page"
