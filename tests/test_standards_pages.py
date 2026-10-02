"""Standards and Documentation: the landing page, its two pages, and what the
Standards page promises about itself.

Most of the Standards page is prose, equations and figures, which a test has
little to say about. What it can hold on to is the wiring a reader relies on
without seeing it (every tab and card that a link, an old bookmark or the search
box names really exists; every analysis type and every Metrics card has a card)
and the one claim the page makes outright: its worked examples are what GREMLIN's
own calculators produce. Those are recomputed here from the services, so a change
to a calculation that leaves its documentation behind fails here instead of
quietly making the page wrong.
"""

import importlib
import math
import pathlib
import re
from datetime import date, datetime

import pytest


def _app(monkeypatch, tmp_path):
    monkeypatch.setenv("GREMLIN_ACCESS_DB_PATH", str(tmp_path / "accesscontrol.db"))
    monkeypatch.setenv("GREMLIN_DB_PATH", str(tmp_path / "gremlin.db"))
    import app
    return importlib.reload(app)


LANDING = "/standards-and-documentation"
STANDARDS = "/standards-and-documentation/standards"
DOCUMENTATION = "/standards-and-documentation/documentation"

TEMPLATES = pathlib.Path(__file__).resolve().parents[1] / "templates"


def _main(body: str) -> str:
    """The page's own markup, without the topbar, sidebar and footer around it."""
    main = re.search(r'<main class="content" role="main">(.*?)</main>', body, re.S)
    assert main, "the page rendered no main landmark"
    return main.group(1)


@pytest.fixture
def module(monkeypatch, tmp_path):
    return _app(monkeypatch, tmp_path)


@pytest.fixture
def standards(module):
    return module.app.test_client().get(STANDARDS).get_data(as_text=True)


def _panel(body: str, panel_id: str) -> str:
    panel = re.search(
        rf'<article class="tab-panel" id="{panel_id}"[^>]*>(.*?)</article>', body, re.S
    )
    assert panel, f"no tab panel {panel_id}"
    return panel.group(1)


def _cards(panel: str) -> dict[str, str]:
    """Every expandable card in a panel, by id."""
    return dict(re.findall(r'<details class="std-card" id="([^"]+)">(.*?)</details>', panel, re.S))


# --- the landing page --------------------------------------------------------

def test_the_landing_page_is_two_cards_one_per_page(module):
    body = _main(module.app.test_client().get(LANDING).get_data(as_text=True))
    grid = re.search(r'<section class="card-grid two-col std-landing-grid"[^>]*>(.*?)</section>', body, re.S)
    assert grid, "the landing page has no card grid"
    cards = re.findall(r'<a class="[^"]*std-landing-card[^"]*" id="([^"]+)" href="([^"]+)"', grid.group(1))
    assert cards == [
        ("std-card-standards", STANDARDS),
        ("std-card-documentation", DOCUMENTATION),
    ]


def test_documentation_says_it_is_coming_soon_on_its_card_and_its_page(module):
    """There are no documents yet, so neither the card nor the page may look
    like a library with something in it."""
    client = module.app.test_client()
    landing = _main(client.get(LANDING).get_data(as_text=True))
    card = re.search(r'id="std-card-documentation".*?</a>', landing, re.S).group(0)
    assert module.COMING_SOON_LABEL in card
    assert "placeholder-badge" in card
    # Standards is finished, so its card carries no such mark.
    standards_card = re.search(r'id="std-card-standards".*?</a>', landing, re.S).group(0)
    assert "placeholder-badge" not in standards_card

    page = _main(client.get(DOCUMENTATION).get_data(as_text=True))
    assert module.COMING_SOON_LABEL in page
    assert "No documents have been published yet" in page
    assert f'href="{STANDARDS}"' in page


@pytest.mark.parametrize("path", [STANDARDS, DOCUMENTATION])
def test_both_pages_lead_back_to_the_landing_page(module, path):
    body = _main(module.app.test_client().get(path).get_data(as_text=True))
    crumbs = re.search(r'<nav class="std-breadcrumb"[^>]*>(.*?)</nav>', body, re.S)
    assert crumbs, path
    assert f'href="{LANDING}"' in crumbs.group(1)
    assert 'aria-current="page"' in crumbs.group(1)


def test_old_tab_links_are_forwarded_to_where_their_content_went(module, standards):
    """The landing page used to be four tabs, and the search box, bookmarks and
    copied links named them by fragment. Each old name is forwarded, and to an
    element the Standards page really has."""
    landing = module.app.test_client().get(LANDING).get_data(as_text=True)
    moved = dict(re.findall(r'"([a-z-]+-panel)": "([a-z-]+)"', landing))
    assert set(moved) == {"metrics-panel", "weibull-panel", "trends-panel", "data-panel"}
    for old, new in moved.items():
        assert f'id="{new}"' in standards, (old, new)


# --- the Standards page: tabs and cards ---------------------------------------

def test_the_standards_page_has_its_three_tabs(standards):
    tabs = re.findall(r'<button type="button" id="([^"]+)"[^>]*role="tab"[^>]*aria-controls="([^"]+)"[^>]*>([^<]+)</button>', standards)
    assert [(tab_id, controls, label.strip()) for tab_id, controls, label in tabs] == [
        ("metrics-tab", "metrics", "Metrics"),
        ("analysis-tab", "analysis", "Analysis"),
        ("how-gremlin-works-tab", "how-gremlin-works", "How GREMLIN Works"),
    ]
    # The first tab is open on arrival and the other two wait.
    assert re.search(r'<article class="tab-panel" id="metrics" [^>]*aria-labelledby="metrics-tab">', standards)
    for hidden in ["analysis", "how-gremlin-works"]:
        assert re.search(rf'<article class="tab-panel" id="{hidden}" [^>]*hidden>', standards), hidden


def test_every_metrics_card_has_a_card_and_only_availability_is_finished(standards):
    """One expandable card per card on the Metrics page. The two the Metrics page
    marks as under construction are marked the same way here."""
    metrics_page = (TEMPLATES / "metrics.html").read_text()
    on_metrics_page = re.findall(r'<article class="glass-card metrics-card" id="card-([a-z]+)"', metrics_page)
    assert on_metrics_page == ["kpis", "alerts", "availability"]

    cards = _cards(_panel(standards, "metrics"))
    assert set(cards) == {"metrics-kpis", "metrics-risk", "metrics-availability"}
    for card_id in ["metrics-kpis", "metrics-risk"]:
        summary = re.search(r"<summary.*?</summary>", cards[card_id], re.S).group(0)
        assert "construction-badge" in summary, card_id
    availability = re.search(r"<summary.*?</summary>", cards["metrics-availability"], re.S).group(0)
    assert "construction-badge" not in availability


def test_every_analysis_type_on_offer_has_its_own_card(standards):
    """Read off the Analysis Type dropdown itself, so a type added there without
    a write-up here fails rather than going undocumented."""
    picker = (TEMPLATES / "perform_analysis.html").read_text()
    offered = re.findall(r'<option value="([^"]+)"', picker.split('id="lda-analysis-type"', 1)[1].split("</select>", 1)[0])
    assert offered == [
        "Weibull Analysis",
        "Failure Mode Trend Analysis",
        "Downtime Driver Analysis",
        "PM Effectiveness Analysis",
    ]
    panel = _panel(standards, "analysis")
    for analysis_type in offered:
        assert f'<span class="eyebrow">{analysis_type}</span>' in panel, analysis_type


def test_ids_are_unique_and_every_link_on_the_page_lands(standards):
    ids = re.findall(r'\sid="([^"]+)"', standards)
    duplicates = sorted({i for i in ids if ids.count(i) > 1})
    assert duplicates == []
    for target in set(re.findall(r'href="#([^"]+)"', standards)):
        assert target in ids, target


def test_every_search_entry_into_the_page_names_a_tab_or_something_in_one(module, standards):
    """standards.js reveals whatever a fragment names by finding the tab panel it
    sits in, so a catalog entry is only useful if its fragment is inside one."""
    entries = [
        entry for entry in module.SEARCH_ENTRIES
        if entry["url"].startswith(STANDARDS + "#")
    ]
    assert entries, "the search catalog no longer links into the Standards page"
    panels = {
        panel_id: _panel(standards, panel_id)
        for panel_id in ["metrics", "analysis", "how-gremlin-works"]
    }
    for entry in entries:
        fragment = entry["url"].split("#", 1)[1]
        assert fragment in panels or any(
            f'id="{fragment}"' in markup for markup in panels.values()
        ), entry["label"]


def test_every_figure_is_named_for_assistive_tech(standards):
    """An inline SVG is a picture to a screen reader unless it says what it shows."""
    standards = _main(standards)
    svgs = re.findall(r'<svg[^>]*role="img"[^>]*aria-labelledby="([^"]+)"', standards)
    figures = re.findall(r'<figure class="std-figure"', standards)
    assert len(svgs) == len(figures) > 0
    for labelled_by in svgs:
        title_id, desc_id = labelled_by.split()
        assert f'<title id="{title_id}">' in standards, title_id
        assert f'<desc id="{desc_id}">' in standards, desc_id


def test_equation_and_figure_numbers_are_each_used_once(standards):
    """The prose refers to "equation A4" and "figure W1" by number."""
    equations = re.findall(r'aria-label="Equation ([A-Z]+[0-9]+):', standards)
    figures = re.findall(r'<span class="std-figure-label">Figure ([A-Z]+[0-9]+)\.</span>', standards)
    assert equations and figures
    assert len(equations) == len(set(equations))
    assert len(figures) == len(set(figures))


@pytest.mark.parametrize("path", [LANDING, STANDARDS, DOCUMENTATION])
def test_the_pages_text_uses_no_em_or_en_dashes(module, path):
    """Asked for when these pages were written: ranges say "to", asides use a
    comma or a colon."""
    body = _main(module.app.test_client().get(path).get_data(as_text=True))
    for dash in ("—", "–"):
        assert dash not in body, (path, dash)


# --- the worked examples are the calculators' own output -----------------------

def test_the_availability_examples_are_what_the_calculator_returns(standards):
    from services.availability_service import (
        AssetGroup,
        LinkedRule,
        WorkOrder,
        build_series,
        compute_rows,
        resolve_window,
        weekday_count,
    )

    january = date(2026, 1, 1)
    group = AssetGroup("Example", ("A", "P", "C"), 24, 1, 2, 3)
    assert group.net_scheduled_hours_per_day == 18
    assert weekday_count(2026, 1) == 22
    orders = [
        WorkOrder("A", datetime(2026, 1, 12, 9), 10.0),
        WorkOrder("P", datetime(2026, 1, 8, 9), 12.0),
        WorkOrder("L1", datetime(2026, 1, 5, 9), 6.0),
        WorkOrder("L2", datetime(2026, 1, 6, 9), 10.0),
        WorkOrder("L3", datetime(2026, 1, 7, 9), 30.0),
        WorkOrder("L4", datetime(2026, 1, 9, 9), 2.0),
    ]
    rules = [LinkedRule("P", linked, 0.5) for linked in ("L1", "L2", "L3", "L4")]
    rows = {row.asset_number: row for row in compute_rows([group], [january], orders, linked_rules=rules)}
    assert rows["A"].scheduled_hours == 396
    assert rows["P"].linked_downtime_hours == 24
    assert rows["P"].adjusted_downtime_hours == 36
    assert rows["C"].note == "No WO entries this month"

    def percent(value):
        return f"{value * 100:.2f}%"

    for shown in (percent(rows["A"].availability), percent(rows["P"].availability), percent(rows["C"].availability)):
        assert shown in standards, shown
    average = build_series(list(rows.values()), [group], [january])[0].average[0]
    assert percent(average) in standards

    with_overtime = compute_rows(
        [AssetGroup("Example", ("A",), 24, 1, 2, 3)], [january], orders, manual_ot={("A", january): 8.0}
    )[0]
    assert with_overtime.adjusted_scheduled_hours == 404
    assert percent(with_overtime.availability) in standards

    # 100 x (P - group average), in points.
    delta = 100 * (rows["P"].availability - average)
    assert f"−{abs(delta):.2f} points" in standards

    assert resolve_window(date(2026, 10, 2), months=5)[0] == date(2026, 5, 1)
    assert resolve_window(date(2026, 10, 2), months=5, data_earliest=date(2026, 7, 1))[0] == date(2026, 7, 1)
    assert "May to September 2026" in standards
    assert "July to September 2026" in standards


def test_the_weibull_example_is_the_fit_gremlin_would_make(standards):
    from services.life_data_service import LifeDataService

    service = LifeDataService.__new__(LifeDataService)
    lives = [(520.0, 1), (450.0, 0), (310.0, 1), (980.0, 1), (1050.0, 0)]
    beta, eta, log_likelihood = service._fit_weibull_2p(lives)
    beta_lo, beta_hi, eta_lo, eta_hi = service._weibull_confidence_intervals(lives, beta, eta)
    mttf = eta * math.gamma(1 + 1 / beta)
    b10 = eta * (-math.log(0.90)) ** (1 / beta)
    b50 = eta * math.log(2) ** (1 / beta)
    aic = 4 - 2 * log_likelihood
    bic = 2 * math.log(len(lives)) - 2 * log_likelihood

    for shown in (
        f"{beta:.3f}",
        f"{eta:.1f} h",
        f"{sum(t ** beta for t, _ in lives):,.0f}",
        f"β {beta_lo:.3f} to {beta_hi:.3f}; η {eta_lo:.1f} to {eta_hi:,.0f} h",
        f"{mttf:.1f} h",
        f"{b10:.1f} h and {b50:.1f} h",
        f"−{abs(log_likelihood):.2f}; {aic:.2f}; {bic:.2f}",
    ):
        assert shown in standards, shown

    survival = [round(point["survival_estimate"], 3) for point in service._kaplan_meier_points(lives)]
    assert survival == [0.8, 0.533, 0.267]
    for shown in ("0.800", "0.533", "0.267"):
        assert shown in standards, shown

    # The reading the page gives: wear-out by beta, but an interval crossing 1.
    assert beta > 1.1 and beta_lo < 1 < beta_hi


def test_the_pm_and_downtime_examples_match_the_service(standards):
    from services.life_data_service import LifeDataService

    pms = [(datetime(2026, month, day), {"task_id": f"PM{month}{day}"}) for month, day in [(1, 5), (1, 19), (3, 2), (7, 1)]]
    failures = [
        (datetime(2026, 2, 18), {"mapped_record_id": 1}),
        (datetime(2026, 6, 10), {"mapped_record_id": 2}),
    ]
    pairs = LifeDataService._pair_pms_to_failures(pms, failures)
    days = [round(pair[4]) for pair in pairs]
    assert days == [44, 30, 100]
    average = sum(pair[4] for pair in pairs) / len(pairs)
    assert f"{average:.1f} days" in standards
    assert LifeDataService._pm_effectiveness_rating(average) == "Fair"
    assert len({pair[3]["mapped_record_id"] for pair in pairs}) == 2

    downtimes = [0.5, 2, 2, 6, 30]
    assert LifeDataService._median(downtimes) == 2
    assert f"{sum(downtimes):.1f} h" in standards
    assert f"{sum(downtimes) / len(downtimes):.1f} h" in standards
