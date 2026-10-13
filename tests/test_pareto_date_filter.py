"""The failure mechanism Pareto's From and To dates.

The Pareto on Perform an Analysis is shown for every analysis type, and its date
filter narrows it to the included failures whose work order date falls in the
range. These pin what that date is (completed, else start, else created, read in
UTC like the rest of the page), that a failure with no usable date drops out only
while a bound is set, and that both requests that draw the chart -- the summary
and the Pareto's own endpoint -- take the dates.
"""

import importlib
from datetime import date
from pathlib import Path

import pytest

from services.life_data_service import LifeDataService

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = (ROOT / "static" / "js" / "life_data_analysis.js").read_text(encoding="utf-8")
TEMPLATE = (ROOT / "templates" / "perform_analysis.html").read_text(encoding="utf-8")

ASSET = "A-1"
# (task id, mechanism, completed, start, created, downtime hours). Wear has three
# failures a month apart; Fatigue one dated only by its start date, one only by its
# created date, and one with no date at all.
FAILURES = [
    ("101", "Wear", "2024-01-15", None, None, 4.0),
    ("102", "Wear", "2024-02-15", None, None, 2.0),
    # A UTC timestamp late on the last day of March stays in March.
    ("103", "Wear", "2024-03-31T23:30:00+00:00", None, None, 1.0),
    ("201", "Fatigue", "  ", "2024-02-20", None, 8.0),
    ("202", "Fatigue", None, None, "2024-03-05", 3.0),
    ("203", "Fatigue", None, None, None, 5.0),
]


def _seed(service: LifeDataService) -> None:
    with service.write_connection() as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS import_batch (import_batch_id INTEGER PRIMARY KEY, status TEXT)")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS raw_cmms_record ("
            "raw_record_id INTEGER PRIMARY KEY, import_batch_id INTEGER NOT NULL DEFAULT 1, raw_json TEXT NOT NULL)"
        )
        conn.execute("INSERT INTO import_batch (import_batch_id, status) VALUES (1, 'COMPLETED')")
        mode_id = conn.execute("INSERT INTO failure_mode (failure_mode_name) VALUES ('Bearing')").lastrowid
        mechanisms = {
            name: conn.execute(
                "INSERT INTO failure_mechanism (failure_mechanism_name, failure_mode_id) VALUES (?, ?)",
                (name, mode_id),
            ).lastrowid
            for name in ("Wear", "Fatigue")
        }
        for raw_id, (task_id, mechanism, completed, started, created, downtime) in enumerate(FAILURES, start=1):
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
                (mapped_id, mode_id, mechanisms[mechanism]),
            )


@pytest.fixture
def service(tmp_path):
    service = LifeDataService(tmp_path / "gremlin.db", refresh_on_startup=False)
    _seed(service)
    return service


def _by_name(rows):
    return {row["failure_mechanism_name"]: (row["failure_count"], row["downtime_hours"]) for row in rows}


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


def test_an_inverted_range_is_refused(service):
    with pytest.raises(ValueError):
        service.failure_mechanism_pareto(ASSET, start=date(2024, 3, 1), end=date(2024, 2, 1))


# ---- the endpoints -----------------------------------------------------------


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("GREMLIN_DB_PATH", str(tmp_path / "gremlin.db"))
    monkeypatch.setenv("GREMLIN_ACCESS_DB_PATH", str(tmp_path / "accesscontrol.db"))
    import app

    module = importlib.reload(app)
    _seed(module.get_life_data_service())
    return module.app.test_client()


def test_the_pareto_endpoint_takes_the_dates(client):
    everything = client.get(f"/life-data-analysis/api/pareto?asset={ASSET}").get_json()["pareto"]
    assert _by_name(everything) == {"Fatigue": (3, 16.0), "Wear": (3, 7.0)}

    narrowed = client.get(
        f"/life-data-analysis/api/pareto?asset={ASSET}&pareto_from=2024-02-15&pareto_to=2024-03-05"
    ).get_json()["pareto"]
    assert _by_name(narrowed) == {"Fatigue": (2, 11.0), "Wear": (1, 2.0)}

    # A blank bound is no bound, the same as leaving it off.
    blank = client.get(f"/life-data-analysis/api/pareto?asset={ASSET}&pareto_from=&pareto_to=2024-01-31").get_json()
    assert _by_name(blank["pareto"]) == {"Wear": (1, 4.0)}


def test_the_summary_draws_the_pareto_for_the_same_dates(client):
    payload = client.get(f"/life-data-analysis/api/summary?asset={ASSET}&pareto_from=2024-03-01").get_json()
    assert _by_name(payload["pareto"]) == {"Fatigue": (1, 3.0), "Wear": (1, 1.0)}
    # Only the Pareto is narrowed; the trend beside it still covers every month.
    assert payload["trend"]["months"][0] == "2024-01"


@pytest.mark.parametrize(
    "query",
    [
        "pareto_from=15/02/2024",
        "pareto_to=not-a-date",
        "pareto_from=2024-03-01&pareto_to=2024-02-01",
    ],
)
def test_a_bad_date_is_a_400_not_a_server_error(client, query):
    response = client.get(f"/life-data-analysis/api/pareto?asset={ASSET}&{query}")
    assert response.status_code == 400
    assert "Pareto" in response.get_json()["error"]


# ---- the page ----------------------------------------------------------------


def test_the_pareto_panel_has_the_date_inputs_and_the_script_wires_them():
    for element_id in ("lda-pareto-from", "lda-pareto-to", "lda-pareto-reset"):
        assert f'id="{element_id}"' in TEMPLATE, element_id
        assert f'$("{element_id}").addEventListener' in SCRIPT, element_id
    assert 'type="date" id="lda-pareto-from"' in TEMPLATE
    assert 'type="date" id="lda-pareto-to"' in TEMPLATE
    # Both requests that draw the chart send the dates, so a disposition change
    # (which re-fetches the summary) keeps the Pareto on the dates showing.
    assert "/summary?asset=${encodeURIComponent(asset)}&weeks=${riskWeeks()}${paretoRangeQuery()}" in SCRIPT
    assert "/pareto?asset=${encodeURIComponent(asset)}${paretoRangeQuery()}" in SCRIPT
