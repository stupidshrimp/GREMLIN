"""The Include in Weibull Candidate box shows what a saved disposition stored.

Once a record has a disposition, the save always stores an explicit include flag,
so a work order saved as INCLUDED_FAILURE with the box unticked is out of the
Weibull fit. The box used to be ticked for it anyway, because the category alone
implied inclusion -- and the row still looked unchanged, so the screen and the
database disagreed until somebody edited the notes on that row and saved, which
quietly put it back into the fit.

These run the page's own buildDispositionControls under node, against rows
straight from the two endpoints that feed it: the disposition table's and the
single-record editor's on Perform an Analysis.
"""

import importlib
import json
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest

ANALYSIS_JS = Path(__file__).resolve().parent.parent / "static" / "js" / "life_data_analysis.js"

ASSET = "A-1"


def _seed(service) -> dict:
    """Work orders and PMs saved each way the box can come out, plus undispositioned ones."""

    ids: dict = {}
    with service.write_connection() as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS import_batch (import_batch_id INTEGER PRIMARY KEY, status TEXT)")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS raw_cmms_record ("
            "raw_record_id INTEGER PRIMARY KEY, import_batch_id INTEGER NOT NULL DEFAULT 1, raw_json TEXT NOT NULL)"
        )
        conn.execute("INSERT INTO import_batch (import_batch_id, status) VALUES (1, 'COMPLETED')")
        mode_id = conn.execute("INSERT INTO failure_mode (failure_mode_name) VALUES ('Bearing')").lastrowid
        mechanism_id = conn.execute(
            "INSERT INTO failure_mechanism (failure_mechanism_name, failure_mode_id) VALUES ('Wear', ?)", (mode_id,)
        ).lastrowid
        ids.update(mode_id=int(mode_id), mechanism_id=int(mechanism_id))
        for index, (task_id, record_class) in enumerate(
            [
                ("wo-off", "CORRECTIVE_WO"),
                ("wo-on", "CORRECTIVE_WO"),
                ("wo-new", "CORRECTIVE_WO"),
                ("pm-off", "PM"),
                ("pm-new", "PM"),
            ],
            start=1,
        ):
            conn.execute("INSERT INTO raw_cmms_record (raw_record_id, raw_json) VALUES (?, '{}')", (index,))
            ids[task_id] = int(
                conn.execute(
                    """
                    INSERT INTO mapped_cmms_record (
                        raw_record_id, import_batch_id, asset_number, task_id, task_name, completed_date_final,
                        record_class_auto, is_corrective_wo_candidate, is_pm_candidate
                    ) VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        index,
                        ASSET,
                        task_id,
                        f"Task {task_id}",
                        f"2024-0{index}-15",
                        record_class,
                        int(record_class == "CORRECTIVE_WO"),
                        int(record_class == "PM"),
                    ),
                ).lastrowid
            )

    wo = {
        "kind": "wo",
        "disposition_category": "INCLUDED_FAILURE",
        "record_class_final": "CORRECTIVE_WO",
        "failure_mode_id": ids["mode_id"],
        "failure_mechanism_id": ids["mechanism_id"],
    }
    # Through the ordinary save, so each is stored the way the screen's save stores it.
    service.save_dispositions(
        [
            {**wo, "mapped_record_id": ids["wo-off"], "include_in_weibull_candidate": False},
            {**wo, "mapped_record_id": ids["wo-on"], "include_in_weibull_candidate": True},
            {
                "mapped_record_id": ids["pm-off"],
                "kind": "pm",
                "disposition_category": "INCLUDED_PM_RESET_EVENT",
                "record_class_final": "PM",
                "pm_reset_decision": "APPROVED_RESET",
                "pm_reset_rationale": "Bearing replaced",
                "reset_target_failure_mode_id": ids["mode_id"],
                "reset_target_failure_mechanism_id": ids["mechanism_id"],
                "include_in_weibull_candidate": False,
            },
        ]
    )
    return ids


def _stored_include(service, mapped_record_id):
    with sqlite3.connect(service.db_path) as conn:
        return conn.execute(
            "SELECT include_in_weibull_candidate FROM event_disposition WHERE mapped_record_id = ? AND is_current = 1",
            (mapped_record_id,),
        ).fetchone()[0]


@pytest.fixture
def seeded(monkeypatch, tmp_path):
    monkeypatch.setenv("GREMLIN_DB_PATH", str(tmp_path / "gremlin.db"))
    monkeypatch.setenv("GREMLIN_ACCESS_DB_PATH", str(tmp_path / "accesscontrol.db"))
    import app

    module = importlib.reload(app)
    service = module.get_life_data_service()
    return service, module.app.test_client(), _seed(service)


def _table(client, kind):
    return client.get(f"/life-data-analysis/api/dispositions?asset={ASSET}&kind={kind}").get_json()


def _record(client, kind, mapped_record_id):
    return client.get(
        f"/life-data-analysis/api/dispositions/record?asset={ASSET}&kind={kind}&mapped_record_id={mapped_record_id}"
    ).get_json()


# ---- what the rows carry --------------------------------------------------


def test_a_row_says_whether_it_has_a_disposition(seeded):
    """The box needs to tell "never dispositioned" from "saved unticked"."""

    _service, client, ids = seeded
    rows = {row["mapped_record_id"]: row for row in _table(client, "wo")["rows"]}
    assert rows[ids["wo-new"]]["event_disposition_id"] is None
    assert rows[ids["wo-new"]]["include_in_weibull_candidate"] is None
    assert rows[ids["wo-off"]]["event_disposition_id"] is not None
    assert rows[ids["wo-off"]]["include_in_weibull_candidate"] == 0
    assert _record(client, "wo", ids["wo-off"])["row"]["event_disposition_id"] == rows[ids["wo-off"]]["event_disposition_id"]


def test_sorting_the_column_reads_the_stored_flag(seeded):
    """The server's order has to agree with the boxes it is ordering."""

    service, _client, ids = seeded
    ordered = [
        row["mapped_record_id"]
        for row in service.disposition_rows(ASSET, "wo", sort="include_in_weibull_candidate", sort_dir="desc")
    ]
    assert ordered[0] == ids["wo-on"]
    assert set(ordered[1:]) == {ids["wo-off"], ids["wo-new"]}


# ---- the box itself, under node --------------------------------------------

# buildDispositionControls and the payload helpers it saves through, lifted out
# of the page script the way the tour tests lift theirs, and run against
# stand-ins for the DOM builders they call. For every row it reports whether the
# box is ticked, then edits only the notes and reports what a save would send.
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

const el = (tag, attrs) => Object.assign({ tag, value: "", checked: false }, attrs || {});
const buildSelect = (options, current) => ({ tag: "select", value: current });
const buildTaxonomyCombobox = (options, idKey, nameKey, currentId) => {
  const match = currentId == null ? null : options.find((opt) => Number(opt[idKey]) === Number(currentId));
  const input = { value: match ? match[nameKey] : "" };
  return {
    nodes: [input],
    input,
    getValue: () => input.value.trim(),
    getSelectedId: () => (match ? Number(match[idKey]) : null),
  };
};
eval(["mechKey", "resolveMechanismId", "dispositionTaxonomy", "dispositionPayloadFromRow", "buildDispositionControls"]
  .map(grab).join(";"));

const payloads = JSON.parse(fs.readFileSync(process.argv[3], "utf8"));
const out = [];
payloads.forEach((data) => {
  (data.rows || [data.row]).forEach((row) => {
    const { rowState } = buildDispositionControls(row, data, dispositionTaxonomy(data));
    const ticked = rowState.include.checked;
    rowState.notes.value += " checked the bearing housing";
    const edited = dispositionPayloadFromRow(rowState, data.kind);
    out.push({ mapped_record_id: row.mapped_record_id, kind: data.kind, ticked, edited });
  });
});
console.log(JSON.stringify(out));
"""


def _boxes(tmp_path, payloads):
    """(kind, mapped_record_id) -> {ticked, edited} for every row the payloads carry."""

    runner = tmp_path / "boxes.js"
    runner.write_text(_HARNESS)
    data = tmp_path / "payloads.json"
    data.write_text(json.dumps(payloads))
    result = subprocess.run(
        ["node", str(runner), str(ANALYSIS_JS), str(data)],
        capture_output=True, text=True, check=True, timeout=30,
    )
    return {(row["kind"], row["mapped_record_id"]): row for row in json.loads(result.stdout)}


needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="needs node to run the disposition controls")


@needs_node
def test_a_saved_row_shows_the_flag_it_was_saved_with(seeded, tmp_path):
    _service, client, ids = seeded
    boxes = _boxes(tmp_path, [_table(client, "wo"), _table(client, "pm")])

    # Saved INCLUDED_FAILURE / INCLUDED_PM_RESET_EVENT + APPROVED_RESET, unticked.
    assert boxes[("wo", ids["wo-off"])]["ticked"] is False
    assert boxes[("pm", ids["pm-off"])]["ticked"] is False
    assert boxes[("wo", ids["wo-on"])]["ticked"] is True


@needs_node
def test_the_single_record_editor_shows_it_too(seeded, tmp_path):
    """Perform an Analysis opens one record through the same controls."""

    _service, client, ids = seeded
    boxes = _boxes(tmp_path, [_record(client, "wo", ids["wo-off"]), _record(client, "pm", ids["pm-off"])])
    assert boxes[("wo", ids["wo-off"])]["ticked"] is False
    assert boxes[("pm", ids["pm-off"])]["ticked"] is False


@needs_node
def test_an_undispositioned_row_defaults_as_before(seeded, tmp_path):
    _service, client, ids = seeded
    wo, pm = _table(client, "wo"), _table(client, "pm")
    boxes = _boxes(tmp_path, [wo, pm])
    # Nothing saved means no category, so nothing is implied.
    assert boxes[("wo", ids["wo-new"])]["ticked"] is False
    assert boxes[("pm", ids["pm-new"])]["ticked"] is False

    # Without a saved disposition, a category that implies inclusion still ticks it.
    new_wo = next(row for row in wo["rows"] if row["mapped_record_id"] == ids["wo-new"])
    new_pm = next(row for row in pm["rows"] if row["mapped_record_id"] == ids["pm-new"])
    implied = _boxes(
        tmp_path,
        [
            {**wo, "rows": [{**new_wo, "disposition_category": "INCLUDED_FAILURE"}]},
            {
                **pm,
                "rows": [
                    {
                        **new_pm,
                        "disposition_category": "INCLUDED_PM_RESET_EVENT",
                        "pm_reset_inclusion_decision": "APPROVED_RESET",
                    }
                ],
            },
        ],
    )
    assert implied[("wo", ids["wo-new"])]["ticked"] is True
    assert implied[("pm", ids["pm-new"])]["ticked"] is True


@needs_node
def test_editing_the_notes_does_not_put_a_row_back_into_the_fit(seeded, tmp_path):
    """The reported loss: save a notes-only edit and the flag has to survive."""

    service, client, ids = seeded
    boxes = _boxes(tmp_path, [_table(client, "wo"), _table(client, "pm")])
    edited = [boxes[("wo", ids["wo-off"])]["edited"], boxes[("pm", ids["pm-off"])]["edited"]]
    assert [payload["include_in_weibull_candidate"] for payload in edited] == [False, False]

    service.save_dispositions(edited)
    assert _stored_include(service, ids["wo-off"]) == 0
    assert _stored_include(service, ids["pm-off"]) == 0
