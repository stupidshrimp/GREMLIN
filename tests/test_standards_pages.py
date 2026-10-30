"""Standards and Documentation: the landing page, its two pages, and what the
Documentation page promises about itself.

Most of the Documentation page is prose, equations and figures, which a test has
little to say about. What it can hold on to is the wiring a reader relies on
without seeing it (every tab and card that a link, an old bookmark or the search
box names really exists; every analysis type and every Metrics card has a card)
and the one claim the page makes outright: its worked examples are what GREMLIN's
own calculators produce. Those are recomputed here from the services, so a change
to a calculation that leaves its documentation behind fails here instead of
quietly making the page wrong.
"""

import importlib
import importlib.util
import json
import math
import pathlib
import re
import shutil
import subprocess
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
def documentation(module):
    return module.app.test_client().get(DOCUMENTATION).get_data(as_text=True)


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
        ("std-card-documentation", DOCUMENTATION),
        ("std-card-standards", STANDARDS),
    ]
    headings = re.findall(r"<h2>([^<]+)</h2>", grid.group(1))
    assert headings == ["Documentation", "Standards"]


def test_standards_says_it_is_coming_soon_on_its_card_and_its_page(module):
    """There are no standards yet, so neither the card nor the page may look
    like a library with something in it."""
    client = module.app.test_client()
    landing = _main(client.get(LANDING).get_data(as_text=True))
    card = re.search(r'id="std-card-standards".*?</a>', landing, re.S).group(0)
    assert module.COMING_SOON_LABEL in card
    assert "placeholder-badge" in card
    # Documentation is finished, so its card carries no such mark.
    documentation_card = re.search(r'id="std-card-documentation".*?</a>', landing, re.S).group(0)
    assert "placeholder-badge" not in documentation_card

    page = _main(client.get(STANDARDS).get_data(as_text=True))
    assert module.COMING_SOON_LABEL in page
    assert "No standards have been published yet" in page
    assert f'href="{DOCUMENTATION}"' in page


def test_links_into_the_old_standards_page_are_forwarded_to_documentation(module):
    """The Documentation page's tabs and cards used to live at the Standards
    address, and bookmarks and copied links name them by fragment there. The
    Standards page forwards a fragment to the same id on Documentation."""
    page = module.app.test_client().get(STANDARDS).get_data(as_text=True)
    assert f'window.location.replace("{DOCUMENTATION}#" + fragment)' in page


@pytest.mark.parametrize("path", [STANDARDS, DOCUMENTATION])
def test_both_pages_lead_back_to_the_landing_page(module, path):
    body = _main(module.app.test_client().get(path).get_data(as_text=True))
    crumbs = re.search(r'<nav class="std-breadcrumb"[^>]*>(.*?)</nav>', body, re.S)
    assert crumbs, path
    assert f'href="{LANDING}"' in crumbs.group(1)
    assert 'aria-current="page"' in crumbs.group(1)


def test_old_tab_links_are_forwarded_to_where_their_content_went(module, documentation):
    """The landing page used to be four tabs, and the search box, bookmarks and
    copied links named them by fragment. Each old name is forwarded, and to an
    element the Documentation page really has."""
    landing = module.app.test_client().get(LANDING).get_data(as_text=True)
    assert f'window.location.replace("{DOCUMENTATION}#" + target)' in landing
    moved = dict(re.findall(r'"([a-z-]+-panel)": "([a-z-]+)"', landing))
    assert set(moved) == {"metrics-panel", "weibull-panel", "trends-panel", "data-panel"}
    for old, new in moved.items():
        assert f'id="{new}"' in documentation, (old, new)


# --- the Documentation page: tabs and cards -----------------------------------

def test_the_documentation_page_has_its_three_tabs(documentation):
    tabs = re.findall(r'<button type="button" id="([^"]+)"[^>]*role="tab"[^>]*aria-controls="([^"]+)"[^>]*>([^<]+)</button>', documentation)
    assert [(tab_id, controls, label.strip()) for tab_id, controls, label in tabs] == [
        ("metrics-tab", "metrics", "Metrics"),
        ("analysis-tab", "analysis", "Analysis"),
        ("how-gremlin-works-tab", "how-gremlin-works", "How GREMLIN Works"),
    ]
    # The first tab is open on arrival and the other two wait.
    assert re.search(r'<article class="tab-panel" id="metrics" [^>]*aria-labelledby="metrics-tab">', documentation)
    for hidden in ["analysis", "how-gremlin-works"]:
        assert re.search(rf'<article class="tab-panel" id="{hidden}" [^>]*hidden>', documentation), hidden


def test_every_metrics_card_has_a_card_and_only_availability_is_finished(documentation):
    """One expandable card per card on the Metrics page. The two the Metrics page
    marks as under construction are marked the same way here."""
    metrics_page = (TEMPLATES / "metrics.html").read_text()
    on_metrics_page = re.findall(r'<article class="glass-card metrics-card" id="card-([a-z]+)"', metrics_page)
    assert on_metrics_page == ["kpis", "alerts", "availability"]

    cards = _cards(_panel(documentation, "metrics"))
    assert set(cards) == {"metrics-kpis", "metrics-risk", "metrics-availability"}
    for card_id in ["metrics-kpis", "metrics-risk"]:
        summary = re.search(r"<summary.*?</summary>", cards[card_id], re.S).group(0)
        assert "construction-badge" in summary, card_id
    availability = re.search(r"<summary.*?</summary>", cards["metrics-availability"], re.S).group(0)
    assert "construction-badge" not in availability


def test_every_analysis_type_on_offer_has_its_own_card(documentation):
    """Read off the Analysis Type dropdown itself, so a type added there without
    a write-up here fails rather than going undocumented."""
    picker = (TEMPLATES / "perform_analysis.html").read_text()
    offered = re.findall(r'<option value="([^"]+)"', picker.split('id="lda-analysis-type"', 1)[1].split("</select>", 1)[0])
    assert offered == [
        "Weibull Analysis",
        "Triage Analysis",
        "Downtime Driver Analysis",
        "PM Effectiveness Analysis",
        "Repeat Fix Rate Analysis",
    ]
    panel = _panel(documentation, "analysis")
    for analysis_type in offered:
        assert f'<span class="eyebrow">{analysis_type}</span>' in panel, analysis_type


def test_ids_are_unique_and_every_link_on_the_page_lands(documentation):
    ids = re.findall(r'\sid="([^"]+)"', documentation)
    duplicates = sorted({i for i in ids if ids.count(i) > 1})
    assert duplicates == []
    for target in set(re.findall(r'href="#([^"]+)"', documentation)):
        assert target in ids, target


def test_every_search_entry_into_the_page_names_a_tab_or_something_in_one(module, documentation):
    """documentation.js reveals whatever a fragment names by finding the tab panel
    it sits in, so a catalog entry is only useful if its fragment is inside one."""
    entries = [
        entry for entry in module.SEARCH_ENTRIES
        if entry["url"].startswith(DOCUMENTATION + "#")
    ]
    assert entries, "the search catalog no longer links into the Documentation page"
    panels = {
        panel_id: _panel(documentation, panel_id)
        for panel_id in ["metrics", "analysis", "how-gremlin-works"]
    }
    for entry in entries:
        fragment = entry["url"].split("#", 1)[1]
        assert fragment in panels or any(
            f'id="{fragment}"' in markup for markup in panels.values()
        ), entry["label"]


def test_the_disposition_helper_is_explained_where_the_disposition_page_points(documentation):
    """The helper's card, on the How GREMLIN Works tab, says how it fills a work
    order in and how it checks new codes; the Disposition page's helper bar
    links straight to it."""
    cards = _cards(_panel(documentation, "how-gremlin-works"))
    helper = cards["site-helper"]
    for heading in ("Filling in a disposition", "What the percentage means", "Checking new codes for duplicates"):
        assert heading in helper, heading
    assert "#site-helper" in (TEMPLATES / "disposition.html").read_text()


def test_every_figure_is_named_for_assistive_tech(documentation):
    """An inline SVG is a picture to a screen reader unless it says what it shows."""
    documentation = _main(documentation)
    svgs = re.findall(r'<svg[^>]*role="img"[^>]*aria-labelledby="([^"]+)"', documentation)
    figures = re.findall(r'<figure class="std-figure"', documentation)
    assert len(svgs) == len(figures) > 0
    for labelled_by in svgs:
        title_id, desc_id = labelled_by.split()
        assert f'<title id="{title_id}">' in documentation, title_id
        assert f'<desc id="{desc_id}">' in documentation, desc_id


def test_equation_and_figure_numbers_are_each_used_once(documentation):
    """The prose refers to "equation A4" and "figure W1" by number."""
    equations = re.findall(r'aria-label="Equation ([A-Z]+[0-9]+):', documentation)
    figures = re.findall(r'<span class="std-figure-label">Figure ([A-Z]+[0-9]+)\.</span>', documentation)
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

def test_the_availability_examples_are_what_the_calculator_returns(documentation):
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
        assert shown in documentation, shown

    # The note counts only the asset's own orders, so a parent whose month holds
    # nothing but linked downtime is below 100% and still says nobody logged anything.
    linked_only = {
        row.asset_number: row
        for row in compute_rows([group], [january], [o for o in orders if o.asset_number != "P"], linked_rules=rules)
    }["P"]
    assert linked_only.note == "No WO entries this month"
    assert linked_only.availability < 1
    assert percent(linked_only.availability) in documentation
    average = build_series(list(rows.values()), [group], [january])[0].average[0]
    assert percent(average) in documentation

    with_overtime = compute_rows(
        [AssetGroup("Example", ("A",), 24, 1, 2, 3)], [january], orders, manual_ot={("A", january): 8.0}
    )[0]
    assert with_overtime.adjusted_scheduled_hours == 404
    assert percent(with_overtime.availability) in documentation

    # 100 x (P - group average), in points.
    delta = 100 * (rows["P"].availability - average)
    assert f"−{abs(delta):.2f} points" in documentation

    assert resolve_window(date(2026, 10, 2), months=5)[0] == date(2026, 5, 1)
    assert resolve_window(date(2026, 10, 2), months=5, data_earliest=date(2026, 7, 1))[0] == date(2026, 7, 1)
    assert "May to September 2026" in documentation
    assert "July to September 2026" in documentation


def test_the_weibull_example_is_the_fit_gremlin_would_make(documentation):
    from services.life_data_service import MIN_WEIBULL_FAILURE_LIVES, LifeDataService

    service = LifeDataService.__new__(LifeDataService)
    lives = [(520.0, 1), (450.0, 0), (310.0, 1), (980.0, 1), (760.0, 1), (640.0, 1), (1050.0, 0)]
    # The example is one GREMLIN would fit: it has the minimum number of failure lives.
    assert sum(failed for _, failed in lives) >= MIN_WEIBULL_FAILURE_LIVES
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
        assert shown in documentation, shown

    survival = [round(point["survival_estimate"], 3) for point in service._kaplan_meier_points(lives)]
    assert survival == [0.857, 0.686, 0.514, 0.343, 0.171]
    for shown in ("0.857", "0.686", "0.514", "0.343", "0.171"):
        assert shown in documentation, shown

    # The probability plot's R² of those five points (equation W15), where the page states it.
    r_squared = LifeDataService._probability_plot_r_squared(service._kaplan_meier_points(lives))
    for shown in (
        f'R² of the five points, equation W15</span><span class="std-result">{r_squared:.3f}</span>',
        f"their R², equation W15, is {r_squared:.3f}",
        f"(R² = {r_squared:.3f}, above the",
    ):
        assert shown in documentation, shown

    # Reliability at the 400-hour age the example evaluates, and MTTF in calendar
    # weeks on the 20-hour schedule (100 life hours a week).
    reliability = math.exp(-((400 / eta) ** beta))
    assert f'<span class="std-result">{reliability:.3f}</span>, so F(400) = {1 - reliability:.3f}' in documentation
    weeks = LifeDataService.calendar_weeks_for_life_hours(mttf, {"hours_per_day": 20.0, "exclude_weekends": True})
    assert f"about {weeks:.1f} calendar weeks on the 20-hour schedule" in documentation

    # The reading the page gives: wear-out by beta, an interval clear of 1 but wider
    # than 70% of beta (treat with caution), and an eta interval wider than 40% of eta.
    assert beta > 1.1 and 1 < beta_lo and beta_hi - beta_lo > 0.7 * beta
    assert f"It is still {round(100 * (beta_hi - beta_lo) / beta)}% of β, though, far wider than 70%" in documentation
    assert (eta_hi - eta_lo) / eta > 0.4
    assert f"{round(100 * (eta_hi - eta_lo) / eta)}% of η" in documentation
    # And its R² against the review threshold for five failure lives.
    assert f"above the {LifeDataService.r_squared_review_threshold(5):.3f} review threshold for five failures" in documentation

    # And what the page says of the example cut off after its third complete life:
    # under the minimum, with a beta interval that crossed 1.
    shorter = [(520.0, 1), (450.0, 0), (310.0, 1), (980.0, 1), (1050.0, 0)]
    assert sum(failed for _, failed in shorter) < MIN_WEIBULL_FAILURE_LIVES
    short_beta, short_eta, _ = service._fit_weibull_2p(shorter)
    short_lo, short_hi, _, _ = service._weibull_confidence_intervals(shorter, short_beta, short_eta)
    assert short_lo < 1 < short_hi
    assert f"{short_lo:.2f} to {short_hi:.2f}" in documentation


def test_the_r_squared_thresholds_shown_are_gremlins(documentation):
    """The review-threshold table on the Weibull card is the one GREMLIN flags fits by."""
    from services.life_data_service import LifeDataService

    row = re.search(r"Review below R² of</th>(.*?)</tr>", documentation, re.S).group(1)
    shown = [float(value) for value in re.findall(r">([0-9.]+)</td>", row)]
    assert shown == [round(LifeDataService.r_squared_review_threshold(n), 3) for n in (5, 10, 15, 20, 30, 50, 100, 200)]


def test_the_life_hours_figure_is_what_gremlin_counts(documentation):
    """Figure W2: Monday 08:00 to the next Monday 08:00, plant time, on the 20-hour schedule."""
    from datetime import timezone
    from zoneinfo import ZoneInfo

    from services.life_data_service import LifeDataService

    plant = ZoneInfo("America/Chicago")
    # A week with no clock change in it: the week the clocks spring forward has a
    # 47-hour weekend, which GREMLIN counts as such.
    start = datetime(2026, 4, 6, 8, 0, tzinfo=plant).astimezone(timezone.utc)
    end = datetime(2026, 4, 13, 8, 0, tzinfo=plant).astimezone(timezone.utc)
    life, weekend, non_run = LifeDataService._scheduled_life_hours(start, end, 20.0, tz=plant)
    assert round(life, 6) == 100 and round(weekend, 6) == 48 and round(non_run, 6) == 20
    assert "giving 100 life hours for an asset on the 20-hour schedule" in documentation
    assert "100 life hours on the 20-hour schedule" in documentation


def test_the_pm_and_downtime_examples_match_the_service(documentation, tmp_path):
    from services.life_data_service import LifeDataService
    from services.population_analysis_service import PopulationAnalysisService
    from services.standard_disposition_service import StandardDispositionService

    life = LifeDataService(tmp_path / "gremlin.db", refresh_on_startup=False)
    with life.write_connection() as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS import_batch (import_batch_id INTEGER PRIMARY KEY, status TEXT)")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS raw_cmms_record ("
            "raw_record_id INTEGER PRIMARY KEY, import_batch_id INTEGER NOT NULL DEFAULT 1, raw_json TEXT NOT NULL)"
        )
        conn.execute("INSERT INTO import_batch (import_batch_id, status) VALUES (1, 'COMPLETED')")
        # The example: four PMs and the two failures after them, on one asset.
        work = [(1, "2026-01-05", "PM"), (2, "2026-01-19", "PM"), (3, "2026-03-02", "PM"), (4, "2026-07-01", "PM"),
                (5, "2026-02-18", "CORRECTIVE_WO"), (6, "2026-06-10", "CORRECTIVE_WO")]
        for record_id, day, record_class in work:
            conn.execute("INSERT INTO raw_cmms_record (raw_record_id, raw_json) VALUES (?, '{}')", (record_id,))
            conn.execute(
                """
                INSERT INTO mapped_cmms_record (mapped_record_id, raw_record_id, import_batch_id, asset_number, task_id,
                    task_name, created_date_final, completed_date_final, record_class_auto, mapping_version)
                VALUES (?, ?, 1, 'A-1', ?, 'Work', ?, ?, ?, 'v4')
                """,
                (record_id, record_id, f"T{record_id}", f"{day}T12:00:00+00:00", f"{day}T12:00:00+00:00", record_class),
            )
    records = StandardDispositionService(life)
    coded = {"component_code": "Pump", "failure_mechanism_code": "WEAR", "action_code": "REPLACE", "evidence": "EXPLICIT"}
    records.save_dispositions(
        [{"mapped_record_id": record_id, "disposition_category": "NON_FAILURE", **coded, "failure_mode_code": "NFF"}
         for record_id in (1, 2, 3, 4)]
        + [{"mapped_record_id": record_id, "disposition_category": "FAILURE", **coded, "failure_mode_code": "UST"}
           for record_id in (5, 6)]
    )
    pm = PopulationAnalysisService(life, records).pm_effectiveness({"assets": ["A-1"]})
    days = sorted(round(row["days_to_failure"]) for row in pm["rows"])
    assert days == [30, 44, 100]
    assert f"{pm['average_days_to_failure']:.1f} days" in documentation
    assert pm["effectiveness"] == "Fair"
    assert pm["failures_after_pm"] == 2

    downtimes = [0.5, 2, 2, 6, 30]
    assert LifeDataService._median(downtimes) == 2
    assert f"{sum(downtimes):.1f} h" in documentation
    assert f"{sum(downtimes) / len(downtimes):.1f} h" in documentation


# --- the Weibull card's working figures ------------------------------------------

SCRIPT = TEMPLATES.parent / "static" / "js" / "weibull_tutorial.js"
DRAWINGS = TEMPLATES / "documentation_weibull_drawings.html"
WORKED_LIVES = [(520.0, 1), (450.0, 0), (310.0, 1), (980.0, 1), (760.0, 1), (640.0, 1), (1050.0, 0)]


def _prerender_tool():
    spec = importlib.util.spec_from_file_location(
        "prerender_weibull_figures", TEMPLATES.parent / "tools" / "prerender_weibull_figures.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _figure_data(documentation: str) -> dict:
    block = re.search(r'<script type="application/json" id="weibull-figure-data">(.*?)</script>', documentation, re.S)
    assert block, "the Weibull card has no figure data"
    return json.loads(block.group(1))


def _preset_lives(preset: dict) -> list[tuple[float, int]]:
    """A preset's lives as the service takes them: the current life last, censored."""
    lives = [(float(t), int(failed)) for t, failed in preset["lives"]]
    if preset.get("current"):
        lives.append((float(preset["current"]), 0))
    return lives


def _fit(lives):
    from services.life_data_service import LifeDataService

    service = LifeDataService.__new__(LifeDataService)
    beta, eta, log_likelihood = service._fit_weibull_2p(lives)
    intervals = service._weibull_confidence_intervals(lives, beta, eta)
    km = LifeDataService._kaplan_meier_points(lives)
    failures = sum(failed for _, failed in lives)
    return {
        "beta": beta,
        "eta": eta,
        "ll": log_likelihood,
        "ci": intervals,
        "km": km,
        "r2": LifeDataService._probability_plot_r_squared(km),
        "threshold": LifeDataService.r_squared_review_threshold(failures),
        "failures": failures,
    }


def _node():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is not installed, so the figure script cannot be run here")
    return node


def test_the_censoring_comparison_is_what_gremlin_fits(documentation):
    """The table under Why censored lives have to count: the worked example fitted
    with its two censored lives counted as GREMLIN counts them, left out, and
    counted as failures."""
    table = re.search(r'id="weibull-censoring-table">(.*?)</table>', documentation, re.S).group(1)
    rows = re.findall(r"<tr><td>[^<]+</td>((?:<td class=\"is-numeric\">[^<]+</td>){3})</tr>", table)
    shown = [re.findall(r">([^<]+)<", row) for row in rows]
    ways = [
        WORKED_LIVES,
        [(t, 1) for t, failed in WORKED_LIVES if failed],
        [(t, 1) for t, _ in WORKED_LIVES],
    ]
    expected = []
    for lives in ways:
        fit = _fit(lives)
        mttf = fit["eta"] * math.gamma(1 + 1 / fit["beta"])
        expected.append([f"{fit['beta']:.3f}", f"{fit['eta']:.1f} h", f"{mttf:.1f} h"])
    assert shown == expected

    # "about 15% short" when left out, "about 10% short" when counted as failures.
    kept, left_out, as_failures = (_fit(lives) for lives in ways)
    assert round(100 * (1 - left_out["eta"] / kept["eta"])) == 15
    assert round(100 * (1 - as_failures["eta"] / kept["eta"]), -1) == 10


def test_the_figure_presets_are_what_the_card_says_they_are(documentation):
    """Every number the Weibull card quotes from the figures' presets, recomputed by
    the service from the lives the figures draw."""
    data = _figure_data(documentation)
    presets = data["presets"]
    # The prose wraps where the template's lines do.
    text = re.sub(r"\s+", " ", documentation)
    assert set(data["presetOrder"]) == set(presets)
    assert set(data["patterns"]) <= set(presets)

    # The worked example is the one in the text, and Too few failures the text's
    # example cut off after its third complete life.
    assert _preset_lives(presets["worked"]) == WORKED_LIVES
    assert _preset_lives(presets["few"]) == [(520.0, 1), (450.0, 0), (310.0, 1), (980.0, 1), (1050.0, 0)]
    assert sorted(_preset_lives(presets["duplicate"])) == sorted(WORKED_LIVES + [(0.5, 1)])

    # The presets table: failures and PM resets in each.
    counts = {key: (sum(f for _, f in p["lives"]), sum(1 - f for _, f in p["lives"])) for key, p in presets.items()}
    assert counts["wearout"] == (20, 2)
    assert counts["random"] == (12, 1)
    assert counts["early"] == (15, 1)
    assert counts["mixed"] == (12, 0)
    assert counts["failure_free"] == (12, 0)
    from services.life_data_service import MIN_WEIBULL_FAILURE_LIVES
    assert counts["few"][0] < MIN_WEIBULL_FAILURE_LIVES

    worked = _fit(_preset_lives(presets["worked"]))

    # A duplicate work order: beta from 2.74 to 0.75, R² under its threshold.
    duplicate = _fit(_preset_lives(presets["duplicate"]))
    assert duplicate["beta"] < 0.9 < 1.1 < worked["beta"]
    assert f"β falls from {worked['beta']:.2f} to {duplicate['beta']:.2f}" in text
    assert duplicate["r2"] < duplicate["threshold"]

    # Two mechanisms pooled: beta near 1, R² under its threshold.
    mixed = _fit(_preset_lives(presets["mixed"]))
    assert f"β comes out at {mixed['beta']:.2f}" in text
    assert mixed["r2"] < mixed["threshold"]

    # A failure-free period: beta overstated, and R² still passes.
    free = _fit(_preset_lives(presets["failure_free"]))
    assert f"overstates β at {free['beta']:.2f}" in text
    assert free["r2"] >= free["threshold"]

    # One population: R² clear of its threshold.
    wearout = _fit(_preset_lives(presets["wearout"]))
    assert wearout["r2"] >= wearout["threshold"]

    # The intervals: 154% of beta for the worked example, 65% for twenty
    # failures, where both readings turn stable.
    def beta_width(fit):
        low, high, _, _ = fit["ci"]
        return (high - low) / fit["beta"]

    def eta_width(fit):
        _, _, low, high = fit["ci"]
        return (high - low) / fit["eta"]

    assert f"leave β's interval {round(100 * beta_width(worked))}% as wide as β itself" in text
    assert f"brings that down to {round(100 * beta_width(wearout))}%, inside the 70%" in text
    assert beta_width(worked) > 0.7 and eta_width(worked) > 0.4
    assert beta_width(wearout) <= 0.7 and eta_width(wearout) <= 0.4
    assert wearout["ci"][0] > 1


def test_the_lab_reads_a_fit_in_the_interpretation_tables_words(documentation):
    """The lab's readings are the Reading and Suggested direction columns of the
    table under Reading the result, word for word."""
    script = SCRIPT.read_text(encoding="utf-8")
    readings = re.findall(r'\{ label: "([^"]+)", direction: "([^"]+)" \}', script)
    assert len(readings) == 10
    for label, direction in readings:
        assert f"<td>{label}</td><td>{direction}</td>" in documentation, label


def test_every_drawing_has_its_place_in_a_figure(documentation):
    """documentation_weibull_drawings.html holds one macro per piece of a working
    figure; each is placed once, and none is left over."""
    tool = _prerender_tool()
    drawn = tool.read_drawings(DRAWINGS.read_text(encoding="utf-8"))
    template = (TEMPLATES / "documentation_analysis.html").read_text(encoding="utf-8")
    placed = re.findall(r"\{\{ drawn\.([a-z_]+)\(\) \}\}", template)
    assert sorted(placed) == sorted(drawn)
    assert len(placed) == len(set(placed))
    # Every working figure has a drawing for the script to replace.
    kinds = re.findall(r'data-weibull-figure="([a-z]+)"', documentation)
    assert sorted(kinds) == ["charts", "explorer", "lab", "likelihood", "patterns", "straighten"]
    for kind in kinds:
        assert f"{kind}_plot" in drawn, kind


def test_the_drawn_figures_are_what_the_script_draws():
    """The copy of each working figure in the template is what the script draws at
    its opening state, so the page without a script shows the same thing. If this
    fails, run tools/prerender_weibull_figures.py."""
    _node()
    tool = _prerender_tool()
    data = tool.figure_data((TEMPLATES / "documentation_analysis.html").read_text(encoding="utf-8"))
    assert tool.render(data) == tool.read_drawings(DRAWINGS.read_text(encoding="utf-8"))


# The interpretation summary's recommendation for each reading, by how it opens:
# the categories the script's readings name.
_SERVICE_READINGS = [
    ("This pattern does not support", "early"),
    ("This result is more consistent with a random", "random"),
    ("This result supports wear-out", "wearOut"),
    ("The interval crossing 1.0", "betaUncertain"),
    ("The interval is no wider than 70%", "betaStable"),
    ("The interval is wider than 70%", "betaCaution"),
    ("The interval is reasonably tight", "etaStable"),
    ("The interval is wide", "etaDirectional"),
    ("Below ", "rSquaredLow"),
    ("At or above ", "rSquaredOk"),
]


def test_the_figure_script_does_gremlins_arithmetic(documentation):
    """weibull_tutorial.js fits, bounds, plots and reads lives as LifeDataService
    does, for every set of lives the figures offer and a few that stop the fit."""
    node = _node()
    from services.life_data_service import (
        BETA_INTERVAL_STABLE_FRACTION,
        ETA_INTERVAL_STABLE_FRACTION,
        MIN_WEIBULL_FAILURE_LIVES,
        R_SQUARED_REVIEW_THRESHOLDS,
        LifeDataService,
        WeibullFitError,
    )

    presets = _figure_data(documentation)["presets"]
    sets = {key: {"lives": p["lives"], "current": p.get("current")} for key, p in presets.items()}
    sets["identical"] = {"lives": [[500, 1]] * 6, "current": None}
    sets["no current life"] = {"lives": [[t, f] for t, f in WORKED_LIVES[:-1]], "current": None}
    program = (
        "const api = require(process.argv[1]); let input = '';"
        "process.stdin.on('data', (c) => { input += c; });"
        "process.stdin.on('end', () => {"
        "  const m = api.method; const sets = JSON.parse(input); const out = {};"
        "  for (const [key, set] of Object.entries(sets)) {"
        "    const r = m.analyse(set);"
        "    out[key] = { problem: r.problem, fit: r.fit, ci: r.ci, km: r.km.map((p) => p.survival),"
        "      r2: r.rSquared, threshold: r.threshold, mttf: r.mttf || null,"
        "      readings: (r.readings || []).map((row) => row.key || null) };"
        "  }"
        "  out.constants = { min: m.MIN_FAILURE_LIVES, beta: m.BETA_INTERVAL_STABLE_FRACTION,"
        "    eta: m.ETA_INTERVAL_STABLE_FRACTION, table: m.R_SQUARED_REVIEW_THRESHOLDS };"
        "  process.stdout.write(JSON.stringify(out));"
        "});"
    )
    completed = subprocess.run(
        [node, "-e", program, str(SCRIPT)],
        input=json.dumps(sets),
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    script = json.loads(completed.stdout)

    constants = script.pop("constants")
    assert constants["min"] == MIN_WEIBULL_FAILURE_LIVES
    assert constants["beta"] == BETA_INTERVAL_STABLE_FRACTION
    assert constants["eta"] == ETA_INTERVAL_STABLE_FRACTION
    assert [tuple(row) for row in constants["table"]] == list(R_SQUARED_REVIEW_THRESHOLDS)

    service = LifeDataService.__new__(LifeDataService)
    close = lambda a, b: a == pytest.approx(b, rel=1e-9, abs=1e-12)  # noqa: E731
    for key, set_ in sets.items():
        lives = [(float(t), int(f)) for t, f in set_["lives"]]
        if set_["current"]:
            lives.append((float(set_["current"]), 0))
        shown = script[key]
        km = LifeDataService._kaplan_meier_points(lives)
        assert all(close(a, b["survival_estimate"]) for a, b in zip(shown["km"], km, strict=True)), key
        r2 = LifeDataService._probability_plot_r_squared(km)
        assert (shown["r2"] is None) == (r2 is None) and (r2 is None or close(shown["r2"], r2)), key
        failures = sum(f for _, f in lives)
        assert close(shown["threshold"], LifeDataService.r_squared_review_threshold(failures)), key

        if failures < MIN_WEIBULL_FAILURE_LIVES:
            assert shown["problem"] == "few", key
            continue
        try:
            beta, eta, log_likelihood = service._fit_weibull_2p(lives)
        except WeibullFitError:
            assert shown["problem"] == "no-root", key
            continue
        assert shown["problem"] is None, key
        assert close(shown["fit"]["beta"], beta) and close(shown["fit"]["eta"], eta), key
        assert close(shown["fit"]["logLikelihood"], log_likelihood), key
        assert close(shown["mttf"], eta * math.gamma(1 + 1 / beta)), key
        intervals = service._weibull_confidence_intervals(lives, beta, eta)
        if intervals[0] is None:
            assert shown["ci"] is None, key
        else:
            ci = shown["ci"]
            assert [ci["betaLo"], ci["betaHi"], ci["etaLo"], ci["etaHi"]] == pytest.approx(list(intervals), rel=1e-6), key

        summary = service._weibull_interpretation_summary(
            beta, eta, eta * math.gamma(1 + 1 / beta), *intervals, r_squared=r2, failure_count=failures
        )
        expected = []
        for row in summary:
            if row["metric"] == "Eta":
                continue
            if row["metric"] == "MTTF":
                continue
            if row["value"] == "Not available":
                expected.append(None)
                continue
            expected.append(next(name for opening, name in _SERVICE_READINGS if row["recommendation"].startswith(opening)))
        if intervals[0] is None:
            # The script gives the two missing intervals one row between them.
            expected = [expected[0], None, expected[-1]]
        assert shown["readings"] == expected, key
    # The sets above include one that stops for want of failures and one with no root.
    assert script["few"]["problem"] == "few"
    assert script["identical"]["problem"] == "no-root"


def test_an_age_off_a_charts_scale_is_named_at_its_edge(documentation):
    """The lab and figure W6 read all four charts at an age. One past a chart's
    scale is named at the edge it lies beyond, on the probability plot's log
    scale and the other three's alike, and no scale is stretched to take it in:
    stretched to a million hours, the fitted curves would vanish between the
    points they are drawn through."""
    node = _node()
    worked = _figure_data(documentation)["presets"]["worked"]
    program = (
        "const api = require(process.argv[1]); let input = '';"
        "process.stdin.on('data', (c) => { input += c; });"
        "process.stdin.on('end', () => {"
        "  const set = JSON.parse(input); const result = api.method.analyse(set);"
        "  const out = {};"
        "  for (const age of [100, 400, 5000, 1000000]) out[age] = api.draw.charts(result, { id: 't', age });"
        "  out.none = api.draw.charts(result, { id: 't', age: null });"
        "  process.stdout.write(JSON.stringify(out));"
        "});"
    )
    completed = subprocess.run(
        [node, "-e", program, str(SCRIPT)],
        input=json.dumps(worked),
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    drawn = json.loads(completed.stdout)
    # 100 h is on the linear scales and off the log one; 5,000 h off all four.
    assert drawn["100"].count("off this scale") == 1
    assert "\u25c0 100 h, off this scale" in drawn["100"]
    assert drawn["5000"].count("5,000 h, off this scale \u25b6") == 4
    assert drawn["1000000"].count("1,000,000 h, off this scale \u25b6") == 4
    assert "off this scale" not in drawn["400"]
    # However far off the age, the fitted curves are drawn on their own scales,
    # exactly as with no age at all.
    curves = lambda markup: re.findall(r'<path class="fig-accent-line" d="([^"]+)"', markup)  # noqa: E731
    assert curves(drawn["1000000"]) == curves(drawn["5000"]) == curves(drawn["none"])


def test_each_lifes_score_in_figure_w5_adds_up_to_the_total(documentation):
    """Figure W5's table gives each life's ln score and calls the total their sum.
    At a guess the sliders allow that makes the lives all but impossible (β 6 and
    η 100 h), the scores themselves round to zero, so the logs are worked out
    directly and every row still has one."""
    node = _node()
    worked = _figure_data(documentation)["presets"]["worked"]
    program = (
        "const api = require(process.argv[1]); let input = '';"
        "process.stdin.on('data', (c) => { input += c; });"
        "process.stdin.on('end', () => {"
        "  const m = api.method; const result = m.analyse(JSON.parse(input));"
        "  const model = { lives: result.lives, current: result.current, best: result.fit };"
        "  const out = {};"
        "  for (const [beta, eta] of [[1, 1200], [2.5, 800], [6, 100]]) {"
        "    out[beta + '/' + eta] = {"
        "      scores: result.lives.map(([t, f]) => m.lifeLogScore(t, f, beta, eta)),"
        "      total: m.logLikelihood(result.lives, beta, eta),"
        "      readout: api.draw.likelihoodReadout({ beta, eta }, model),"
        "    };"
        "  }"
        "  process.stdout.write(JSON.stringify(out));"
        "});"
    )
    completed = subprocess.run(
        [node, "-e", program, str(SCRIPT)],
        input=json.dumps(worked),
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    for guess, shown in json.loads(completed.stdout).items():
        assert sum(shown["scores"]) == pytest.approx(shown["total"], rel=1e-12), guess
        assert "n/a" not in shown["readout"], guess
    impossible = json.loads(completed.stdout)["6/100"]
    assert "under 0.00001" in impossible["readout"]
    assert "over a billion times less likely" in impossible["readout"]
