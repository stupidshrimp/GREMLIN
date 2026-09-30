"""Correcting one record's disposition from the analysis tables.

An analysis lists the work orders it was built from, and that is where a
misclassified one gets noticed. Each of those tables turns a record's number into
a button that opens that record's disposition in place, and saving it refreshes
the analysis. These pin the three things that loop rests on:

* every analysis row names the record behind it (and, for a Weibull observation,
  whether that record is a work order or a PM), since a task id alone is not a
  key -- two records can share one, and the save is by mapped_record_id;
* the single-record endpoint hands back that record, scoped to the asset, with
  the same options the disposition table offers;
* a save made that way really does change the analysis the next time it runs.

The client half is read from disk the way the other script tests read it.
"""

import importlib
import re
import sqlite3
import tempfile
import unittest
from pathlib import Path

from services.life_data_service import LifeDataService

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = (ROOT / "static" / "js" / "life_data_analysis.js").read_text()
STYLES = (ROOT / "static" / "css" / "life_data_analysis.css").read_text()
TEMPLATE = (ROOT / "templates" / "perform_analysis.html").read_text()

ASSET = "A-1"
# (task id, completed date) for the corrective work orders dispositioned onto the
# one mechanism. Three dated failures give two closed life intervals.
WORK_ORDERS = [("101", "2024-01-15"), ("102", "2024-02-15"), ("103", "2024-03-15")]


def _seed(service: LifeDataService, *, with_pm: bool = False) -> dict:
    """One mode/mechanism, the failures on it, and optionally an approved PM reset."""

    ids: dict = {"wo": {}}
    with service.write_connection() as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS import_batch (import_batch_id INTEGER PRIMARY KEY, status TEXT)")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS raw_cmms_record ("
            "raw_record_id INTEGER PRIMARY KEY, import_batch_id INTEGER NOT NULL DEFAULT 1, raw_json TEXT NOT NULL)"
        )
        conn.execute("INSERT INTO import_batch (import_batch_id, status) VALUES (1, 'COMPLETED')")
        mode_id = conn.execute("INSERT INTO failure_mode (failure_mode_name) VALUES ('Bearing')").lastrowid
        mechanism_id = conn.execute(
            "INSERT INTO failure_mechanism (failure_mechanism_name, failure_mode_id) VALUES ('Wear', ?)",
            (mode_id,),
        ).lastrowid
        other_mechanism_id = conn.execute(
            "INSERT INTO failure_mechanism (failure_mechanism_name, failure_mode_id) VALUES ('Fatigue', ?)",
            (mode_id,),
        ).lastrowid
        ids.update(mode_id=int(mode_id), mechanism_id=int(mechanism_id), other_mechanism_id=int(other_mechanism_id))

        def add_record(raw_id, task_id, completed, record_class):
            conn.execute(
                "INSERT INTO raw_cmms_record (raw_record_id, import_batch_id, raw_json) VALUES (?, 1, '{}')",
                (raw_id,),
            )
            return int(
                conn.execute(
                    """
                    INSERT INTO mapped_cmms_record (
                        raw_record_id, import_batch_id, asset_number, task_id, task_name,
                        completed_date_final, downtime_hours, record_class_auto,
                        is_corrective_wo_candidate, is_pm_candidate
                    ) VALUES (?, 1, ?, ?, ?, ?, 1.5, ?, ?, ?)
                    """,
                    (
                        raw_id,
                        ASSET,
                        task_id,
                        f"Task {task_id}",
                        completed,
                        record_class,
                        int(record_class == "CORRECTIVE_WO"),
                        int(record_class == "PM"),
                    ),
                ).lastrowid
            )

        for index, (task_id, completed) in enumerate(WORK_ORDERS, start=1):
            mapped_id = add_record(index, task_id, completed, "CORRECTIVE_WO")
            ids["wo"][task_id] = mapped_id
            conn.execute(
                """
                INSERT INTO event_disposition (
                    mapped_record_id, record_class_final, disposition_category, include_in_event_processing,
                    include_in_weibull_candidate, failure_mode_id, failure_mechanism_id
                ) VALUES (?, 'CORRECTIVE_WO', 'INCLUDED_FAILURE', 1, 1, ?, ?)
                """,
                (mapped_id, mode_id, mechanism_id),
            )
        # The asset's taxonomy options, as a WO save would have recorded them, so
        # a PM reset target can point at them.
        conn.execute(
            "INSERT INTO asset_failure_mode_option (asset_number, failure_mode_id, is_active) VALUES (?, ?, 1)",
            (ASSET, mode_id),
        )
        conn.execute(
            "INSERT INTO asset_failure_mechanism_option (asset_number, failure_mechanism_id, failure_mode_id, is_active) "
            "VALUES (?, ?, ?, 1)",
            (ASSET, mechanism_id, mode_id),
        )
        if with_pm:
            pm_id = add_record(len(WORK_ORDERS) + 1, "900", "2024-02-01", "PM")
            ids["pm"] = pm_id
            conn.execute(
                """
                INSERT INTO event_disposition (
                    mapped_record_id, record_class_final, disposition_category, include_in_event_processing,
                    include_in_weibull_candidate, reset_target_failure_mode_id, reset_target_failure_mechanism_id,
                    pm_reset_inclusion_decision, pm_reset_renewal_rationale
                ) VALUES (?, 'PM', 'INCLUDED_PM_RESET_EVENT', 1, 1, ?, ?, 'APPROVED_RESET', 'Bearing replaced')
                """,
                (pm_id, mode_id, mechanism_id),
            )
    return ids


class AnalysisRowsNameTheirRecordTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.service = LifeDataService(Path(tmp.name) / "gremlin.db", refresh_on_startup=False)
        self.ids = _seed(self.service, with_pm=True)

    def _perform(self):
        return self.service.perform_weibull_analysis(
            ASSET,
            grouping_level="FAILURE_MECHANISM",
            failure_mode_id=self.ids["mode_id"],
            failure_mechanism_id=self.ids["mechanism_id"],
        )

    def test_each_weibull_observation_names_the_record_that_closed_it(self):
        observations = self._perform().observations
        closing = {obs["source_task_id"]: obs for obs in observations if obs["source_mapped_record_id"] is not None}
        # 101 only ever opens the first interval, so it closes no row of its own.
        self.assertEqual(set(closing), {"900", "102", "103"})
        for task_id in ("102", "103"):
            self.assertEqual(closing[task_id]["source_mapped_record_id"], self.ids["wo"][task_id])
            self.assertEqual(closing[task_id]["source_event_role"], "FAILURE_EVENT")
        # A PM reset is a PM disposition, which the editor has to ask for by kind.
        self.assertEqual(closing["900"]["source_mapped_record_id"], self.ids["pm"])
        self.assertEqual(closing["900"]["source_event_role"], "PM_RESET_EVENT")

    def test_the_current_life_row_has_no_record_to_open(self):
        current_life = [obs for obs in self._perform().observations if obs["observation_type"] == "RIGHT_CENSORED_LIFE"]
        self.assertEqual(len(current_life), 1)
        self.assertIsNone(current_life[0]["source_mapped_record_id"])
        self.assertIsNone(current_life[0]["source_event_role"])

    def test_a_saved_analysis_reads_the_same_record_ids_back(self):
        performed = self._perform()
        saved = self.service.load_saved_weibull_analysis(
            ASSET,
            grouping_level="FAILURE_MECHANISM",
            failure_mode_id=self.ids["mode_id"],
            failure_mechanism_id=self.ids["mechanism_id"],
        )
        key = lambda obs: (obs["source_mapped_record_id"], obs["source_event_role"])  # noqa: E731
        self.assertEqual([key(o) for o in saved.observations], [key(o) for o in performed.observations])

    def test_the_pm_table_names_the_corrective_work_order_behind_each_row(self):
        result = self.service.pm_effectiveness(
            ASSET, failure_mechanism_id=self.ids["mechanism_id"], failure_mode_id=self.ids["mode_id"]
        )
        self.assertTrue(result["rows"])
        for row in result["rows"]:
            self.assertEqual(row["corrective_mapped_record_id"], self.ids["wo"][row["corrective_wo_number"]])

    def test_the_trend_and_downtime_tables_already_carry_it(self):
        trend = self.service.failure_mode_trend(ASSET)
        records = [record for mechanism in trend["mechanisms"] for record in mechanism["records"]]
        self.assertTrue(records)
        for record in records:
            self.assertEqual(record["mapped_record_id"], self.ids["wo"][record["task_id"]])
        downtime = self.service.downtime_driver_analysis(
            ASSET, failure_mechanism_id=self.ids["mechanism_id"], failure_mode_id=self.ids["mode_id"]
        )
        for event in downtime["top_events"]:
            self.assertEqual(event["mapped_record_id"], self.ids["wo"][event["task_id"]])


class DispositionRecordTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.service = LifeDataService(Path(tmp.name) / "gremlin.db", refresh_on_startup=False)
        self.ids = _seed(self.service)

    def test_it_is_the_row_the_disposition_table_shows_for_that_record(self):
        mapped_id = self.ids["wo"]["102"]
        record = self.service.disposition_record(ASSET, mapped_id)
        table_rows = {row["mapped_record_id"]: row for row in self.service.disposition_rows(ASSET, "wo")}
        self.assertEqual(record, table_rows[mapped_id])
        self.assertEqual(record["disposition_category"], "INCLUDED_FAILURE")
        self.assertEqual(record["failure_mechanism"], "Wear")

    def test_a_record_on_another_asset_is_not_found(self):
        self.assertIsNone(self.service.disposition_record("SOME-OTHER-ASSET", self.ids["wo"]["102"]))
        self.assertIsNone(self.service.disposition_record(ASSET, 999999))

    def test_it_is_found_even_after_a_record_class_takes_it_off_the_table(self):
        """The analysis can still hold it, so it has to stay correctable."""

        mapped_id = self.ids["wo"]["102"]
        with sqlite3.connect(self.service.db_path) as conn:
            conn.execute(
                "UPDATE mapped_cmms_record SET record_class_final = 'INSPECTION', is_corrective_wo_candidate = 0 "
                "WHERE mapped_record_id = ?",
                (mapped_id,),
            )
            conn.execute("UPDATE event_disposition SET record_class_final = 'INSPECTION' WHERE mapped_record_id = ?", (mapped_id,))
        self.assertNotIn(mapped_id, {row["mapped_record_id"] for row in self.service.disposition_rows(ASSET, "wo")})
        self.assertIsNotNone(self.service.disposition_record(ASSET, mapped_id))

    def test_a_save_made_from_the_table_moves_the_work_order_out_of_the_analysis(self):
        """The whole loop: the analysis names the record, the save is the usual one."""

        run = lambda: self.service.perform_weibull_analysis(  # noqa: E731
            ASSET,
            grouping_level="FAILURE_MECHANISM",
            failure_mode_id=self.ids["mode_id"],
            failure_mechanism_id=self.ids["mechanism_id"],
        )
        before = run()
        target = next(obs for obs in before.observations if obs["source_task_id"] == "103")
        self.assertEqual(before.failure_count, 2)

        # The payload the editor sends: the same shape the disposition table saves.
        self.service.save_dispositions(
            [
                {
                    "mapped_record_id": target["source_mapped_record_id"],
                    "kind": "wo",
                    "disposition_category": "INCLUDED_FAILURE",
                    "disposition_text": "Fatigue crack, not wear.",
                    "record_class_final": "CORRECTIVE_WO",
                    "include_in_weibull_candidate": True,
                    "failure_mode_id": self.ids["mode_id"],
                    "failure_mechanism_id": self.ids["other_mechanism_id"],
                    "failure_mode_text": "Bearing",
                    "failure_mechanism_text": "Fatigue",
                }
            ]
        )

        after = run()
        self.assertEqual(after.failure_count, 1)
        self.assertNotIn(
            target["source_mapped_record_id"], {obs["source_mapped_record_id"] for obs in after.observations}
        )
        record = self.service.disposition_record(ASSET, target["source_mapped_record_id"])
        self.assertEqual(record["failure_mechanism"], "Fatigue")
        self.assertEqual(record["disposition_notes"], "Fatigue crack, not wear.")
        # The previous disposition is kept, just no longer current.
        with sqlite3.connect(self.service.db_path) as conn:
            history = conn.execute(
                "SELECT COUNT(*), SUM(is_current) FROM event_disposition WHERE mapped_record_id = ?",
                (target["source_mapped_record_id"],),
            ).fetchone()
        self.assertEqual(history, (2, 1))


# ---- the endpoint --------------------------------------------------------


def _client(monkeypatch, tmp_path):
    monkeypatch.setenv("GREMLIN_DB_PATH", str(tmp_path / "gremlin.db"))
    monkeypatch.setenv("GREMLIN_ACCESS_DB_PATH", str(tmp_path / "accesscontrol.db"))
    import app

    module = importlib.reload(app)
    return module, module.app.test_client()


def _record_url(asset=ASSET, kind="wo", mapped_record_id=None):
    return f"/life-data-analysis/api/dispositions/record?asset={asset}&kind={kind}&mapped_record_id={mapped_record_id}"


def test_the_endpoint_returns_the_record_with_the_tables_options(monkeypatch, tmp_path):
    module, client = _client(monkeypatch, tmp_path)
    ids = _seed(module.get_life_data_service())
    mapped_id = ids["wo"]["102"]

    payload = client.get(_record_url(mapped_record_id=mapped_id)).get_json()
    assert payload["row"]["mapped_record_id"] == mapped_id
    assert payload["row"]["taskID"] == "102"
    assert payload["kind"] == "wo"

    # The editor is built from the same options as the disposition table, so a
    # record offers the same choices wherever it is edited.
    table = client.get(f"/life-data-analysis/api/dispositions?asset={ASSET}&kind=wo").get_json()
    for key in (
        "categories",
        "record_classes",
        "pm_reset_decisions",
        "mode_options",
        "mechanism_options",
        "narrative_columns",
        "modeled_population_placeholder",
    ):
        assert payload[key] == table[key], key


def test_the_endpoint_offers_the_pm_options_for_a_pm(monkeypatch, tmp_path):
    module, client = _client(monkeypatch, tmp_path)
    ids = _seed(module.get_life_data_service(), with_pm=True)
    payload = client.get(_record_url(kind="pm", mapped_record_id=ids["pm"])).get_json()
    assert "INCLUDED_PM_RESET_EVENT" in payload["categories"]
    assert "INCLUDED_FAILURE" not in payload["categories"]
    assert "CORRECTIVE_WO" not in payload["record_classes"]
    assert payload["row"]["pm_reset_inclusion_decision"] == "APPROVED_RESET"


def test_the_endpoint_refuses_a_record_from_another_asset(monkeypatch, tmp_path):
    module, client = _client(monkeypatch, tmp_path)
    ids = _seed(module.get_life_data_service())
    response = client.get(_record_url(asset="OTHER", mapped_record_id=ids["wo"]["102"]))
    assert response.status_code == 404
    assert "not found" in response.get_json()["error"]


def test_the_endpoint_needs_a_record_id_and_a_kind(monkeypatch, tmp_path):
    _module, client = _client(monkeypatch, tmp_path)
    assert client.get(_record_url(mapped_record_id="")).status_code == 400
    assert client.get(_record_url(mapped_record_id="abc")).status_code == 400
    assert client.get(_record_url(kind="bogus", mapped_record_id=1)).status_code == 400


def test_an_id_wider_than_sqlite_is_not_found_rather_than_a_server_error(monkeypatch, tmp_path):
    _module, client = _client(monkeypatch, tmp_path)
    assert client.get(_record_url(mapped_record_id=str(2**70))).status_code == 404


def test_saving_still_takes_an_editor(monkeypatch, tmp_path):
    """The editor saves through the ordinary endpoint, which keeps its own gate."""

    module, client = _client(monkeypatch, tmp_path)
    ids = _seed(module.get_life_data_service())
    response = client.post(
        "/life-data-analysis/api/dispositions/save",
        json={"dispositions": [{"mapped_record_id": ids["wo"]["102"], "kind": "wo", "disposition_category": "UNKNOWN"}]},
    )
    assert response.status_code == 401


# ---- the browser side ----------------------------------------------------


def test_the_script_asks_the_endpoint_the_server_serves():
    assert "`${API}/dispositions/record?${params.toString()}`" in SCRIPT
    for name in ("asset", "kind", "mapped_record_id"):
        assert re.search(rf"\b{name}[:,]", SCRIPT[SCRIPT.index("async function openRecordDisposition"):]), name


def test_the_editor_saves_through_the_ordinary_save_endpoint():
    editor = SCRIPT[SCRIPT.index("function editRecordDisposition"):SCRIPT.index("function refreshAfterRecordDisposition")]
    assert "`${API}/dispositions/save`, { dispositions: [payload] }" in editor
    # Built from the same controls as a disposition-table row, so the defaults
    # and the payload cannot drift between the two.
    assert "buildDispositionControls(row, data, dispositionTaxonomy(data))" in editor
    assert "dispositionPayloadFromRow(rowState, kind)" in editor
    table = SCRIPT[SCRIPT.index("function renderDispositionEditor"):SCRIPT.index("function buildSelect")]
    assert "buildDispositionControls(row, data, taxonomy)" in table


def test_every_analysis_table_turns_its_record_number_into_the_button():
    # Weibull data table: the closing record, of whichever kind it is.
    assert "recordNumberCell(obs.source_task_id, {" in SCRIPT
    assert "mappedRecordId: obs.source_mapped_record_id" in SCRIPT
    assert "kind: observationRecordKind(obs)" in SCRIPT
    # Work Orders in Trend, PM-to-Failure Detail and Top Downtime Events.
    assert 'recordNumberCell(record.task_id, { mappedRecordId: record.mapped_record_id, kind: "wo" })' in SCRIPT
    assert (
        'recordNumberCell(row.corrective_wo_number, { mappedRecordId: row.corrective_mapped_record_id, kind: "wo" })'
        in SCRIPT
    )
    assert 'recordNumberCell(ev.task_id, { mappedRecordId: ev.mapped_record_id, kind: "wo" }, "—")' in SCRIPT


def test_only_an_editor_gets_the_button():
    cell = SCRIPT[SCRIPT.index("function recordNumberCell"):SCRIPT.index("async function openRecordDisposition")]
    assert "if (!CAN_EDIT || !ref || ref.mappedRecordId == null || !ref.kind)" in cell
    assert "event.stopPropagation();" in cell
    assert 'const RECORD_EDIT_HINT = CAN_EDIT ?' in SCRIPT
    assert "{% if auth_can_edit %} Click a Corrective WO Number" in TEMPLATE


def test_the_weibull_role_maps_onto_the_disposition_kind():
    kind = SCRIPT[SCRIPT.index("function observationRecordKind"):SCRIPT.index("function recordNumberCell")]
    assert 'obs.source_event_role === "FAILURE_EVENT") return "wo"' in kind
    assert 'obs.source_event_role === "PM_RESET_EVENT") return "pm"' in kind


def test_a_weibull_save_reruns_the_group_on_screen():
    refresh = SCRIPT[SCRIPT.index("function refreshAfterRecordDisposition"):]
    assert "runAnalysisForGroup(state.latestResultGroup" in refresh
    assert "state.latestResultGroup = group;" in SCRIPT
    # The other analyses re-read their own endpoints inside refreshSummary().
    assert "refreshSummary();" in refresh[: refresh.index("\n  }\n")]


def test_the_option_lists_open_above_the_modal():
    """They are portaled to <body>, so they have to out-stack the backdrop."""

    backdrop = re.search(r"\.lda-modal-backdrop \{[^}]*z-index: (\d+)", STYLES)
    portal = re.search(r"\.lda-portal-list \{[^}]*z-index: (\d+)", STYLES)
    assert backdrop and portal
    assert int(portal.group(1)) > int(backdrop.group(1))
