"""The From and To dates over the failure mechanism Pareto, and the analyses that take them.

The Pareto on Perform an Analysis is shown for every analysis type, and its From
and To dates narrow it -- and the trend, downtime driver, PM effectiveness and
repeat fix rate analyses with it -- to the records whose work order date falls in
the range, so a bar and the analysis it opens count the same work orders. A
Weibull fit takes them as its start and cutoff dates instead. These pin what that
date is (completed, else start, else created, read in UTC like the rest of the
page), that a record with no usable date drops out only while a bound is set,
that every request carrying an analysis sends the dates, and how a Weibull run
picks them up.
"""

import importlib
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from services.life_data_service import LifeDataService

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = (ROOT / "static" / "js" / "life_data_analysis.js").read_text(encoding="utf-8")
TEMPLATE = (ROOT / "templates" / "perform_analysis.html").read_text(encoding="utf-8")

ASSET = "A-1"
# (task id, mechanism, completed, start, created, downtime hours). Wear has three
# failures a month or more apart; Fatigue one dated only by its start date, one
# only by its created date, and one with no date at all.
FAILURES = [
    ("101", "Wear", "2024-01-15", None, None, 4.0),
    ("102", "Wear", "2024-02-15", None, None, 2.0),
    # A UTC timestamp late on the last day of March stays in March.
    ("103", "Wear", "2024-03-31T23:30:00+00:00", None, None, 1.0),
    ("201", "Fatigue", "  ", "2024-02-20", None, 8.0),
    ("202", "Fatigue", None, None, "2024-03-05", 3.0),
    ("203", "Fatigue", None, None, None, 5.0),
]
# (task id, completed) for the completed PMs on the asset.
PMS = [("901", "2024-01-10"), ("902", "2024-02-10"), ("903", "2024-03-20")]


def _seed(service: LifeDataService, failures=FAILURES) -> dict:
    with service.write_connection() as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS import_batch (import_batch_id INTEGER PRIMARY KEY, status TEXT)")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS raw_cmms_record ("
            "raw_record_id INTEGER PRIMARY KEY, import_batch_id INTEGER NOT NULL DEFAULT 1, raw_json TEXT NOT NULL)"
        )
        conn.execute("INSERT INTO import_batch (import_batch_id, status) VALUES (1, 'COMPLETED')")
        mode_id = conn.execute("INSERT INTO failure_mode (failure_mode_name) VALUES ('Bearing')").lastrowid
        ids = {
            name: conn.execute(
                "INSERT INTO failure_mechanism (failure_mechanism_name, failure_mode_id) VALUES (?, ?)",
                (name, mode_id),
            ).lastrowid
            for name in ("Wear", "Fatigue")
        }
        raw_id = 0
        for task_id, mechanism, completed, started, created, downtime in failures:
            raw_id += 1
            conn.execute(
                "INSERT INTO raw_cmms_record (raw_record_id, import_batch_id, raw_json) VALUES (?, 1, '{}')",
                (raw_id,),
            )
            mapped_id = conn.execute(
                """
                INSERT INTO mapped_cmms_record (
                    raw_record_id, import_batch_id, asset_number, task_id, task_name,
                    completed_date_final, start_date_final, created_date_final, downtime_hours,
                    record_class_auto, is_corrective_wo_candidate
                ) VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?, 'CORRECTIVE_WO', 1)
                """,
                (raw_id, ASSET, task_id, f"Task {task_id}", completed, started, created, downtime),
            ).lastrowid
            conn.execute(
                """
                INSERT INTO event_disposition (
                    mapped_record_id, record_class_final, disposition_category, include_in_event_processing,
                    include_in_weibull_candidate, failure_mode_id, failure_mechanism_id
                ) VALUES (?, 'CORRECTIVE_WO', 'INCLUDED_FAILURE', 1, 1, ?, ?)
                """,
                (mapped_id, mode_id, ids[mechanism]),
            )
        for task_id, completed in PMS:
            raw_id += 1
            conn.execute(
                "INSERT INTO raw_cmms_record (raw_record_id, import_batch_id, raw_json) VALUES (?, 1, '{}')",
                (raw_id,),
            )
            conn.execute(
                """
                INSERT INTO mapped_cmms_record (
                    raw_record_id, import_batch_id, asset_number, task_id, task_name,
                    completed_date_final, downtime_hours, record_class_auto, is_pm_candidate
                ) VALUES (?, 1, ?, ?, ?, ?, 0, 'PM', 1)
                """,
                (raw_id, ASSET, task_id, f"PM {task_id}", completed),
            )
    return {"mode": int(mode_id), **{name: int(value) for name, value in ids.items()}}


@pytest.fixture
def seeded(tmp_path):
    service = LifeDataService(tmp_path / "gremlin.db", refresh_on_startup=False)
    return service, _seed(service)


@pytest.fixture
def service(seeded):
    return seeded[0]


def _by_name(rows):
    return {row["failure_mechanism_name"]: (row["failure_count"], row["downtime_hours"]) for row in rows}


# ---- the Pareto ----------------------------------------------------------------


def test_without_dates_every_included_failure_counts_even_undated(service):
    rows = service.failure_mechanism_pareto(ASSET)
    assert _by_name(rows) == {"Fatigue": (3, 16.0), "Wear": (3, 7.0)}
    assert rows[-1]["cumulative_percent"] == pytest.approx(100.0)


def test_both_bounds_are_inclusive_days(service):
    rows = service.failure_mechanism_pareto(ASSET, start=date(2024, 2, 15), end=date(2024, 3, 5))
    # Wear's February failure is on the From day; Fatigue's start-dated one is inside
    # and its created-dated one is on the To day. The undated one has no place in it.
    assert _by_name(rows) == {"Fatigue": (2, 11.0), "Wear": (1, 2.0)}
    # The running share is over the failures in range, not the whole history.
    assert rows[0]["cumulative_percent"] == pytest.approx(11.0 / 13.0 * 100)
    assert rows[-1]["cumulative_percent"] == pytest.approx(100.0)


def test_either_bound_alone_leaves_the_other_side_open(service):
    assert _by_name(service.failure_mechanism_pareto(ASSET, start=date(2024, 3, 1))) == {
        "Fatigue": (1, 3.0),
        "Wear": (1, 1.0),
    }
    assert _by_name(service.failure_mechanism_pareto(ASSET, end=date(2024, 1, 31))) == {"Wear": (1, 4.0)}


def test_a_utc_timestamp_is_read_as_its_utc_day(service):
    assert _by_name(service.failure_mechanism_pareto(ASSET, start=date(2024, 3, 31), end=date(2024, 3, 31))) == {
        "Wear": (1, 1.0)
    }
    assert service.failure_mechanism_pareto(ASSET, start=date(2024, 4, 1)) == []


def test_an_inverted_range_is_refused_by_every_analysis(seeded):
    service, ids = seeded
    backwards = {"start": date(2024, 3, 1), "end": date(2024, 2, 1)}
    calls = [
        lambda: service.failure_mechanism_pareto(ASSET, **backwards),
        lambda: service.failure_mode_trend(ASSET, **backwards),
        lambda: service.downtime_driver_analysis(ASSET, ids["Wear"], ids["mode"], **backwards),
        lambda: service.pm_effectiveness(ASSET, ids["Wear"], ids["mode"], **backwards),
        lambda: service.repeat_fix_rate(ASSET, **backwards),
    ]
    for call in calls:
        with pytest.raises(ValueError):
            call()


# ---- the analyses take the same dates --------------------------------------------


def test_the_trend_covers_only_the_dates(service):
    trend = service.failure_mode_trend(ASSET, start=date(2024, 2, 1))
    assert trend["months"] == ["2024-02", "2024-03"]
    totals = {m["failure_mechanism_name"]: m["total_count"] for m in trend["mechanisms"]}
    # The same counts as the Pareto over the same dates, undated failure and all.
    assert totals == {"Wear": 2, "Fatigue": 2}
    assert {name: count for name, (count, _) in _by_name(
        service.failure_mechanism_pareto(ASSET, start=date(2024, 2, 1))
    ).items()} == totals


def test_the_downtime_drivers_count_what_the_bar_counts(seeded):
    service, ids = seeded
    everything = service.downtime_driver_analysis(ASSET, ids["Fatigue"], ids["mode"])
    assert everything["summary"]["work_order_count"] == 3
    narrowed = service.downtime_driver_analysis(
        ASSET, ids["Fatigue"], ids["mode"], start=date(2024, 2, 15), end=date(2024, 3, 5)
    )
    assert narrowed["summary"]["work_order_count"] == 2
    assert narrowed["summary"]["total_downtime_hours"] == pytest.approx(11.0)
    assert narrowed["months"] == ["2024-02", "2024-03"]


def test_pm_effectiveness_limits_the_pms_and_the_failures(seeded):
    service, ids = seeded
    everything = service.pm_effectiveness(ASSET, ids["Wear"], ids["mode"])
    assert (everything["pms_performed"], everything["failures_after_pm"]) == (3, 3)

    february = service.pm_effectiveness(ASSET, ids["Wear"], ids["mode"], start=date(2024, 2, 1), end=date(2024, 3, 15))
    assert (february["pms_performed"], february["failures_after_pm"]) == (1, 1)
    assert [row["corrective_wo_number"] for row in february["rows"]] == ["102"]

    # The March 20 PM is in range but the failure after it (March 31) is not, so it
    # counts as a PM that no failure followed.
    to_late_march = service.pm_effectiveness(ASSET, ids["Wear"], ids["mode"], start=date(2024, 2, 1), end=date(2024, 3, 25))
    assert (to_late_march["pms_performed"], to_late_march["failures_after_pm"]) == (2, 1)


def test_repeat_fix_rate_starts_each_chain_inside_the_dates(tmp_path):
    service = LifeDataService(tmp_path / "gremlin.db", refresh_on_startup=False)
    # A Wear failure the day after the first: 20 scheduled hours on the plant default
    # schedule, inside the 24-hour window, so a repeat of it.
    _seed(service, FAILURES + [("104", "Wear", "2024-01-16", None, None, 0.5)])

    everything = service.repeat_fix_rate(ASSET)
    assert everything["repeats"] == 1
    assert [(p["prior_task_id"], p["repeat_task_id"]) for p in everything["pairs"]] == [("101", "104")]

    # From the 16th, the failure the repeat followed is outside the range, so the
    # chain starts at 104 and nothing in it is a repeat.
    narrowed = service.repeat_fix_rate(ASSET, start=date(2024, 1, 16))
    assert narrowed["repeats"] == 0
    wear = next(m for m in narrowed["mechanisms"] if m["failure_mechanism_name"] == "Wear")
    assert wear["failures"] == 3


@pytest.mark.parametrize("zone", ["Pacific/Kiritimati", "Pacific/Pago_Pago"])
def test_today_is_the_plants_day(service, zone):
    """UTC+14 and UTC-11: for most of any day one of them is a calendar day off UTC."""

    with service.write_connection() as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS availability_settings (id INTEGER PRIMARY KEY, timezone TEXT)")
        conn.execute("INSERT OR REPLACE INTO availability_settings (id, timezone) VALUES (1, ?)", (zone,))
    assert service.plant_today() == datetime.now(timezone.utc).astimezone(ZoneInfo(zone)).date().isoformat()


# ---- the endpoints ---------------------------------------------------------------


@pytest.fixture
def client_and_ids(monkeypatch, tmp_path):
    monkeypatch.setenv("GREMLIN_DB_PATH", str(tmp_path / "gremlin.db"))
    monkeypatch.setenv("GREMLIN_ACCESS_DB_PATH", str(tmp_path / "accesscontrol.db"))
    import app

    module = importlib.reload(app)
    ids = _seed(module.get_life_data_service())
    return module.app.test_client(), ids


def test_the_summary_draws_the_pareto_and_trend_for_the_dates(client_and_ids):
    client, _ids = client_and_ids
    everything = client.get(f"/life-data-analysis/api/summary?asset={ASSET}").get_json()
    assert _by_name(everything["pareto"]) == {"Fatigue": (3, 16.0), "Wear": (3, 7.0)}
    assert everything["trend"]["months"][0] == "2024-01"

    payload = client.get(f"/life-data-analysis/api/summary?asset={ASSET}&date_from=2024-03-01").get_json()
    assert _by_name(payload["pareto"]) == {"Fatigue": (1, 3.0), "Wear": (1, 1.0)}
    assert payload["trend"]["months"] == ["2024-03"]
    # What a Weibull run counts as today, for the page to judge the To date by.
    assert payload["plant_today"] == date.fromisoformat(payload["plant_today"]).isoformat()

    # A blank bound is no bound, the same as leaving it off.
    blank = client.get(f"/life-data-analysis/api/summary?asset={ASSET}&date_from=&date_to=2024-01-31").get_json()
    assert _by_name(blank["pareto"]) == {"Wear": (1, 4.0)}


def test_the_analysis_endpoints_take_the_dates(client_and_ids):
    client, ids = client_and_ids
    dates = "date_from=2024-02-15&date_to=2024-03-05"
    downtime = client.get(
        f"/life-data-analysis/api/downtime-drivers?asset={ASSET}&failure_mode_id={ids['mode']}"
        f"&failure_mechanism_id={ids['Fatigue']}&{dates}"
    ).get_json()["downtime_drivers"]
    assert downtime["summary"]["work_order_count"] == 2

    pm = client.get(
        f"/life-data-analysis/api/pm-effectiveness?asset={ASSET}&failure_mode_id={ids['mode']}"
        f"&failure_mechanism_id={ids['Wear']}&date_from=2024-02-01&date_to=2024-03-15"
    ).get_json()["pm_effectiveness"]
    assert (pm["pms_performed"], pm["failures_after_pm"]) == (1, 1)

    repeat = client.get(f"/life-data-analysis/api/repeat-fixes?asset={ASSET}&{dates}").get_json()["repeat_fixes"]
    assert repeat["failures"] == 1  # Wear's February failure; Fatigue's have no completed date


@pytest.mark.parametrize(
    "path",
    [
        "/life-data-analysis/api/summary?asset=A-1",
        "/life-data-analysis/api/repeat-fixes?asset=A-1",
        "/life-data-analysis/api/downtime-drivers?asset=A-1&failure_mode_id=1",
        "/life-data-analysis/api/pm-effectiveness?asset=A-1&failure_mechanism_id=1",
    ],
)
@pytest.mark.parametrize(
    "query, wording",
    [
        ("date_from=15/02/2024", "From date must be a date"),
        ("date_to=not-a-date", "To date must be a date"),
        ("date_from=2024-03-01&date_to=2024-02-01", "From date can't be later than the To date"),
    ],
)
def test_a_bad_date_is_a_400_not_a_server_error(client_and_ids, path, query, wording):
    client, _ids = client_and_ids
    response = client.get(f"{path}&{query}")
    assert response.status_code == 400
    assert wording in response.get_json()["error"]


# ---- the page ----------------------------------------------------------------------


def test_the_pareto_panel_has_the_date_inputs_and_the_script_wires_them():
    for element_id in ("lda-date-from", "lda-date-to", "lda-date-reset"):
        assert f'id="{element_id}"' in TEMPLATE, element_id
        assert f'$("{element_id}").addEventListener' in SCRIPT, element_id
    assert 'type="date" id="lda-date-from"' in TEMPLATE
    assert 'type="date" id="lda-date-to"' in TEMPLATE


def test_an_emptied_pareto_drops_the_old_bars_click_handler():
    """Dates that leave no bars must not leave the last chart's bars clickable."""

    draw = SCRIPT[SCRIPT.index("function drawPareto"):]
    draw = draw[: draw.index("\n  }\n")]
    assert draw.index("canvas.onclick = null;") < draw.index("if (!rows.length) return;")


@pytest.mark.parametrize(
    "request_line",
    [
        # The summary carries the Pareto and the trend, and is what a disposition
        # change re-reads, so the dates showing survive it.
        "withDateRange(new URLSearchParams({ asset, weeks: String(riskWeeks()) }))",
        "withDateRange(new URLSearchParams({ asset, failure_mechanism_id: sel.failure_mechanism_id }))",
        "withDateRange(new URLSearchParams({ asset, window_hours: String(windowHours) }))",
        "withDateRange(new URLSearchParams({ asset, failure_mode_id: sel.failure_mode_id }))",
    ],
)
def test_every_analysis_request_sends_the_dates(request_line):
    assert request_line in SCRIPT


def test_a_date_change_refreshes_every_analysis():
    apply = SCRIPT[SCRIPT.index("function applyDateRange"):SCRIPT.index("function resetDateRange")]
    # The summary re-reads the Pareto and trend, and the downtime, PM and repeat
    # analyses when one is showing.
    assert "refreshSummary();" in apply
    # An editor's fit on screen runs again over the new dates.
    assert "runAnalysisForGroup(state.latestResultGroup" in apply
    assert "CAN_EDIT" in apply
    # The month ranges inside the trend and PM charts open back out.
    assert "state.trendRange = { from: null, to: null };" in apply
    assert "state.pmRange = { from: null, to: null };" in apply
    # Typing a date fires change on each part of it, so it is applied once settled.
    assert "setTimeout(applyDateRange" in SCRIPT


def test_a_weibull_run_takes_the_dates_as_its_window():
    window = SCRIPT[SCRIPT.index("function weibullDateWindow"):SCRIPT.index("let dateRangeDebounce")]
    # A To date of today or later is left off; today is the plant's, from the
    # summary, since the browser's calendar can be a day either side of it.
    assert "to && to < plantToday() ? to : \"\"" in window
    assert "state.plantToday = data.plant_today || null;" in SCRIPT
    bar = SCRIPT[SCRIPT.index("function runParetoMechanism"):SCRIPT.index("// ---- analysis type switching")]
    assert "{ window: weibullDateWindow(), ...options }" in bar
    dialog = SCRIPT[SCRIPT.index("async function performAnalysis"):SCRIPT.index("function sameWeibullGroup")]
    assert "startInput.value = dateWindow.start;" in dialog
    assert "cutoffInput.value = dateWindow.cutoff;" in dialog
