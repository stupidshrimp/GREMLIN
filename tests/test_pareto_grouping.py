"""The Pareto on Perform an Analysis, grouped by failure mode or by failure mechanism.

The summary sends one Pareto row per failure mode and mechanism pair, and the page
draws them as they come when grouped by Failure mechanism. Grouped by Failure
mode, it adds up each mode's pairs into one bar with no mechanism, which is the
mode-level "all mechanisms" selection the analysis pickers offer, so clicking it
runs the analysis for the whole mode. PM effectiveness is per mechanism, so there
a mode bar opens into its mechanisms instead.

These run the page's own functions under node against the rows the summary
endpoint sends, the way the disposition and tour tests run theirs.
"""

import importlib
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
ANALYSIS_JS = ROOT / "static" / "js" / "life_data_analysis.js"
SCRIPT = ANALYSIS_JS.read_text(encoding="utf-8")
TEMPLATE = (ROOT / "templates" / "perform_analysis.html").read_text(encoding="utf-8")

ASSET = "A-1"
# failure mode -> mechanism -> downtime hours of each of its included failures.
FAILURES = {
    "Bearing": {"Wear": [4.0, 2.0, 1.0], "Fatigue": [8.0, 3.0]},
    "Seal": {"Leak": [6.0], "Hardening": [0.5, 0.5, 0.5, 0.5]},
    "Electrical": {"Short": [20.0]},
}


def _seed(service) -> dict:
    ids: dict = {"modes": {}, "mechanisms": {}}
    with service.write_connection() as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS import_batch (import_batch_id INTEGER PRIMARY KEY, status TEXT)")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS raw_cmms_record ("
            "raw_record_id INTEGER PRIMARY KEY, import_batch_id INTEGER NOT NULL DEFAULT 1, raw_json TEXT NOT NULL)"
        )
        conn.execute("INSERT INTO import_batch (import_batch_id, status) VALUES (1, 'COMPLETED')")
        raw_id = 0
        for mode, mechanisms in FAILURES.items():
            mode_id = conn.execute("INSERT INTO failure_mode (failure_mode_name) VALUES (?)", (mode,)).lastrowid
            ids["modes"][mode] = int(mode_id)
            for mechanism, downtimes in mechanisms.items():
                mechanism_id = conn.execute(
                    "INSERT INTO failure_mechanism (failure_mechanism_name, failure_mode_id) VALUES (?, ?)",
                    (mechanism, mode_id),
                ).lastrowid
                ids["mechanisms"][mechanism] = int(mechanism_id)
                for downtime in downtimes:
                    raw_id += 1
                    conn.execute(
                        "INSERT INTO raw_cmms_record (raw_record_id, import_batch_id, raw_json) VALUES (?, 1, '{}')",
                        (raw_id,),
                    )
                    mapped_id = conn.execute(
                        """
                        INSERT INTO mapped_cmms_record (
                            raw_record_id, import_batch_id, asset_number, task_id, task_name,
                            completed_date_final, downtime_hours, record_class_auto, is_corrective_wo_candidate
                        ) VALUES (?, 1, ?, ?, ?, ?, ?, 'CORRECTIVE_WO', 1)
                        """,
                        (raw_id, ASSET, str(raw_id), f"Task {raw_id}", f"2024-0{1 + raw_id % 9}-15", downtime),
                    ).lastrowid
                    conn.execute(
                        """
                        INSERT INTO event_disposition (
                            mapped_record_id, record_class_final, disposition_category, include_in_event_processing,
                            include_in_weibull_candidate, failure_mode_id, failure_mechanism_id
                        ) VALUES (?, 'CORRECTIVE_WO', 'INCLUDED_FAILURE', 1, 1, ?, ?)
                        """,
                        (mapped_id, mode_id, mechanism_id),
                    )
    return ids


@pytest.fixture
def client_and_ids(monkeypatch, tmp_path):
    monkeypatch.setenv("GREMLIN_DB_PATH", str(tmp_path / "gremlin.db"))
    monkeypatch.setenv("GREMLIN_ACCESS_DB_PATH", str(tmp_path / "accesscontrol.db"))
    import app

    module = importlib.reload(app)
    ids = _seed(module.get_life_data_service())
    return module.app.test_client(), ids


def _pareto(client) -> list:
    return client.get(f"/life-data-analysis/api/summary?asset={ASSET}").get_json()["pareto"]


# ---- the page's functions, under node ----------------------------------------

# The Pareto's grouping, ranking and click routing, lifted out of the page script
# and run against stand-ins for what they call. Each case names the state to set
# and what to run, and the harness prints what came back.
_HARNESS = r"""
const fs = require("fs");
const src = fs.readFileSync(process.argv[2], "utf8");
const grab = (name) => {
  const start = src.indexOf("function " + name + "(");
  if (start < 0) throw new Error("missing " + name);
  let depth = 0;
  for (let i = src.indexOf("{", start); i < src.length; i++) {
    if (src[i] === "{") depth++;
    else if (src[i] === "}" && --depth === 0) return src.slice(start, i + 1);
  }
  throw new Error("unbalanced " + name);
};

const ANALYSIS_TYPES = {
  WEIBULL: "Weibull Analysis",
  TREND: "Failure Mode Trend Analysis",
  DOWNTIME: "Downtime Driver Analysis",
  PM: "PM Effectiveness Analysis",
  REPEAT: "Repeat Fix Rate Analysis",
};
const calls = [];
const state = { paretoRows: [], paretoMetric: "downtime_hours", paretoGroupBy: "mechanism", paretoModeFocus: null };
const drawPareto = () => calls.push(["drawPareto", state.paretoGroupBy, state.paretoModeFocus]);
const setActiveMechanism = () => {};
const showBanner = (message) => calls.push(["banner", message]);
const weibullDateWindow = () => ({ start: "", cutoff: "" });
const runAnalysisForGroup = (group) => calls.push(["weibull", group]);
const selectPmMechanism = (row) => calls.push(["pm", row.failure_mechanism_id]);
const selectTrendMechanism = (row) => calls.push(["trend", row.failure_mode_id, row.failure_mechanism_id]);
const selectDowntimeMechanism = (row) => calls.push(["downtime", row.failure_mode_id, row.failure_mechanism_id]);
const selectRepeatMechanism = (row) => calls.push(["repeat", row.failure_mode_id, row.failure_mechanism_id]);
eval(["paretoGroupedRows", "paretoDisplayRows", "openParetoMode", "onParetoBarSelected",
      "runParetoMechanism", "repeatFilterKeeps"].map(grab).join(";"));

const cases = JSON.parse(fs.readFileSync(process.argv[3], "utf8"));
const out = cases.map((c) => {
  calls.length = 0;
  Object.assign(state, c.state || {});
  state.analysisType = c.analysisType || ANALYSIS_TYPES.WEIBULL;
  let value = null;
  if (c.run === "display") value = paretoDisplayRows(c.groupBy);
  if (c.run === "click") onParetoBarSelected(c.row);
  if (c.run === "keeps") value = c.rows.map((row) => repeatFilterKeeps(c.filter, row));
  return { value, calls: calls.slice(), state: { groupBy: state.paretoGroupBy, focus: state.paretoModeFocus } };
});
console.log(JSON.stringify(out));
"""


def _run(tmp_path, cases):
    runner = tmp_path / "pareto.js"
    runner.write_text(_HARNESS)
    data = tmp_path / "cases.json"
    data.write_text(json.dumps(cases))
    result = subprocess.run(
        ["node", str(runner), str(ANALYSIS_JS), str(data)],
        capture_output=True, text=True, check=True, timeout=30,
    )
    return json.loads(result.stdout)


needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="needs node to run the Pareto's grouping")


@needs_node
def test_grouped_by_mode_each_bar_adds_up_its_mechanisms(client_and_ids, tmp_path):
    client, ids = client_and_ids
    rows = _pareto(client)
    (by_mode,) = (case["value"] for case in _run(tmp_path, [
        {"run": "display", "groupBy": "mode", "state": {"paretoRows": rows}},
    ]))
    bars = {bar["failure_mode_name"]: bar for bar in by_mode}
    assert set(bars) == set(FAILURES)
    for mode, mechanisms in FAILURES.items():
        bar = bars[mode]
        assert bar["failure_mode_id"] == ids["modes"][mode]
        # A mode bar is the mode-level selection: no mechanism.
        assert bar["failure_mechanism_id"] is None
        assert bar["failure_count"] == sum(len(d) for d in mechanisms.values())
        assert bar["downtime_hours"] == pytest.approx(sum(sum(d) for d in mechanisms.values()))
        assert bar["mechanism_count"] == len(mechanisms)
    # Largest first by downtime: Electrical 20, Bearing 18, Seal 8.
    assert [bar["failure_mode_name"] for bar in by_mode] == ["Electrical", "Bearing", "Seal"]
    assert by_mode[-1]["_cumulative_percent"] == pytest.approx(100.0)
    assert by_mode[0]["_cumulative_percent"] == pytest.approx(20 / 46 * 100)


@needs_node
def test_both_groupings_count_the_same_failures_and_rank_by_the_measure(client_and_ids, tmp_path):
    client, _ids = client_and_ids
    rows = _pareto(client)
    by_mechanism, by_mode, by_mode_count = (case["value"] for case in _run(tmp_path, [
        {"run": "display", "groupBy": "mechanism", "state": {"paretoRows": rows}},
        {"run": "display", "groupBy": "mode", "state": {"paretoRows": rows}},
        {"run": "display", "groupBy": "mode", "state": {"paretoRows": rows, "paretoMetric": "failure_count"}},
    ]))
    assert len(by_mechanism) == 5
    for key in ("failure_count", "downtime_hours"):
        assert sum(bar[key] for bar in by_mode) == pytest.approx(sum(bar[key] for bar in by_mechanism))
    # By count, Electrical's one failure drops from first to last. Bearing and Seal
    # tie on five, and keep the downtime order between them.
    assert [bar["failure_mode_name"] for bar in by_mode_count] == ["Bearing", "Seal", "Electrical"]
    assert [bar["failure_count"] for bar in by_mode_count] == [5, 5, 1]


@needs_node
def test_an_opened_mode_shows_only_its_mechanisms(client_and_ids, tmp_path):
    client, ids = client_and_ids
    rows = _pareto(client)
    focus = {"failure_mode_id": ids["modes"]["Bearing"], "failure_mode_name": "Bearing"}
    (opened,) = (case["value"] for case in _run(tmp_path, [
        {"run": "display", "groupBy": "mechanism", "state": {"paretoRows": rows, "paretoModeFocus": focus}},
    ]))
    assert [bar["failure_mechanism_name"] for bar in opened] == ["Fatigue", "Wear"]
    # The line is the running share within the mode.
    assert opened[-1]["_cumulative_percent"] == pytest.approx(100.0)


@needs_node
def test_a_mode_bar_runs_the_mode_level_analysis(tmp_path):
    mode_bar = {"failure_mode_id": 7, "failure_mechanism_id": None, "failure_mode_name": "Bearing"}
    weibull, trend, downtime, repeat = _run(tmp_path, [
        {"run": "click", "row": mode_bar, "analysisType": "Weibull Analysis"},
        {"run": "click", "row": mode_bar, "analysisType": "Failure Mode Trend Analysis"},
        {"run": "click", "row": mode_bar, "analysisType": "Downtime Driver Analysis"},
        {"run": "click", "row": mode_bar, "analysisType": "Repeat Fix Rate Analysis"},
    ])
    assert weibull["calls"] == [
        ["weibull", {"grouping_level": "FAILURE_MODE", "failure_mode_id": 7, "failure_mechanism_id": None}]
    ]
    assert trend["calls"] == [["trend", 7, None]]
    assert downtime["calls"] == [["downtime", 7, None]]
    assert repeat["calls"] == [["repeat", 7, None]]


@needs_node
def test_a_mechanism_bar_still_runs_the_mechanism(tmp_path):
    mechanism_bar = {"failure_mode_id": 7, "failure_mechanism_id": 3}
    weibull, pm = _run(tmp_path, [
        {"run": "click", "row": mechanism_bar, "analysisType": "Weibull Analysis"},
        {"run": "click", "row": mechanism_bar, "analysisType": "PM Effectiveness Analysis"},
    ])
    assert weibull["calls"] == [
        ["weibull", {"grouping_level": "FAILURE_MECHANISM", "failure_mode_id": 7, "failure_mechanism_id": 3}]
    ]
    assert pm["calls"] == [["pm", 3]]


@needs_node
def test_under_pm_a_mode_bar_opens_into_its_mechanisms(tmp_path):
    """PM effectiveness needs a mechanism, so the click offers the mode's instead of an error."""

    mode_bar = {"failure_mode_id": 7, "failure_mechanism_id": None, "failure_mode_name": "Bearing"}
    (pm,) = _run(tmp_path, [
        {"run": "click", "row": mode_bar, "analysisType": "PM Effectiveness Analysis",
         "state": {"paretoGroupBy": "mode"}},
    ])
    focus = {"failure_mode_id": 7, "failure_mode_name": "Bearing"}
    assert pm["state"] == {"groupBy": "mechanism", "focus": focus}
    assert pm["calls"] == [["drawPareto", "mechanism", focus]]


@needs_node
def test_a_mode_filter_keeps_every_repeat_under_the_mode(tmp_path):
    rows = [
        {"failure_mode_id": 7, "failure_mechanism_id": 3},
        {"failure_mode_id": 7, "failure_mechanism_id": 4},
        {"failure_mode_id": 8, "failure_mechanism_id": 5},
    ]
    by_mode, by_mechanism = (case["value"] for case in _run(tmp_path, [
        {"run": "keeps", "rows": rows, "filter": {"failure_mode_id": 7, "failure_mechanism_id": None}},
        {"run": "keeps", "rows": rows, "filter": {"failure_mode_id": 7, "failure_mechanism_id": 4}},
    ]))
    assert by_mode == [True, True, False]
    assert by_mechanism == [False, True, False]


# ---- a mode bar and the analysis it opens --------------------------------------


def test_a_mode_bar_matches_the_mode_level_downtime_analysis(client_and_ids):
    """The click sends the mode alone, which counts the same work orders as the bar."""

    client, ids = client_and_ids
    rows = _pareto(client)
    bearing = [row for row in rows if row["failure_mode_id"] == ids["modes"]["Bearing"]]
    response = client.get(
        f"/life-data-analysis/api/downtime-drivers?asset={ASSET}&failure_mode_id={ids['modes']['Bearing']}"
    )
    summary = response.get_json()["downtime_drivers"]["summary"]
    assert summary["work_order_count"] == sum(row["failure_count"] for row in bearing)
    assert summary["total_downtime_hours"] == pytest.approx(sum(row["downtime_hours"] for row in bearing))


# ---- the page ------------------------------------------------------------------


def test_the_pareto_has_group_by_and_rank_by_switches():
    for group in ("mode", "mechanism"):
        assert f'data-group="{group}"' in TEMPLATE
    for metric in ("downtime_hours", "failure_count"):
        assert f'data-metric="{metric}"' in TEMPLATE
    # Pressed buttons in a labelled group, not the old checkbox.
    assert 'role="group" aria-labelledby="lda-pareto-group-label"' in TEMPLATE
    assert 'role="group" aria-labelledby="lda-pareto-metric-label"' in TEMPLATE
    assert TEMPLATE.count('aria-pressed="true"') >= 2
    assert "lda-pareto-toggle" not in TEMPLATE
    # It opens grouped by mechanism, ranked by downtime, as it always has.
    assert 'data-group="mechanism" aria-pressed="true"' in TEMPLATE
    assert 'data-metric="downtime_hours" aria-pressed="true"' in TEMPLATE
    assert 'paretoGroupBy: "mechanism"' in SCRIPT


def test_the_script_wires_the_switches_and_the_way_out_of_an_opened_mode():
    for element_id in ("lda-pareto-group", "lda-pareto-metric", "lda-pareto-focus-clear"):
        assert f'id="{element_id}"' in TEMPLATE, element_id
        assert f'$("{element_id}").addEventListener' in SCRIPT, element_id
    controls = SCRIPT[SCRIPT.index("function renderParetoControls"):SCRIPT.index("function setParetoGroupBy")]
    assert 'button.setAttribute("aria-pressed"' in controls
    # A new asset may not have the mode a bar was opened into.
    select = SCRIPT[SCRIPT.index("if (previous !== state.selectedAsset)"):SCRIPT.index("function clearWorkspace")]
    assert "state.paretoModeFocus = null;" in select


def test_a_redraw_drops_the_old_bars_hover_too():
    draw = SCRIPT[SCRIPT.index("function drawPareto"):]
    draw = draw[: draw.index("\n  }\n")]
    for line in ("canvas.onmousemove = null;", "canvas.onmouseleave = null;", "hidePointTooltip();"):
        assert draw.index(line) < draw.index("if (!rows.length) return;"), line
