"""The work-order disposition standard's endpoints and pages.

Reads are open to whoever may see Life Data Analysis, as the rest of its API is;
every write -- a disposition, a code, a saved Weibull run -- needs an editor, and
records who made it.
"""

import importlib

from tests.test_standard_disposition_service import ACCEPTANCE, ASSET_IDS, PFS, S09

API = "/life-data-analysis/api/standard"


def _app(monkeypatch, tmp_path):
    monkeypatch.setenv("GREMLIN_DB_PATH", str(tmp_path / "gremlin.db"))
    monkeypatch.setenv("GREMLIN_ACCESS_DB_PATH", str(tmp_path / "accesscontrol.db"))
    import app

    return importlib.reload(app)


def _client(module, *, role=None):
    client = module.app.test_client()
    if role is not None:
        module.access_control.save_user(None, f"{role}1", "1357", role, "facilities", "engineer")
        with client.session_transaction() as session:
            session["user"] = module.access_control.authenticate(f"{role}1", "1357")
    return client


def _seed(module):
    life = module.get_life_data_service()
    with life.write_connection() as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS import_batch (import_batch_id INTEGER PRIMARY KEY, status TEXT)")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS raw_cmms_record ("
            "raw_record_id INTEGER PRIMARY KEY, import_batch_id INTEGER NOT NULL DEFAULT 1, raw_json TEXT NOT NULL)"
        )
        conn.execute("INSERT INTO import_batch (import_batch_id, status) VALUES (1, 'COMPLETED')")
        rows = [(900009, S09, "S09 housekeeping", "2023-06-01")] + [
            (record[0], record[1], record[2], record[3]) for record in ACCEPTANCE
        ]
        for record_id, asset, title, created in rows:
            conn.execute("INSERT INTO raw_cmms_record (raw_record_id, raw_json) VALUES (?, '{}')", (record_id,))
            conn.execute(
                """
                INSERT INTO mapped_cmms_record (mapped_record_id, raw_record_id, import_batch_id, asset_number,
                    asset_id_raw, immediate_parent_asset_id, task_id, task_name, created_date_final,
                    completed_date_final, record_class_auto, mapping_version)
                VALUES (?, ?, 1, ?, ?, ?, ?, ?, ?, ?, 'CORRECTIVE_WO', 'v4')
                """,
                (record_id, record_id, asset, ASSET_IDS[asset], None if asset == PFS else ASSET_IDS[PFS],
                 str(record_id), title, created, created),
            )
    return [{"mapped_record_id": record[0], **record[6]} for record in ACCEPTANCE]


def test_reading_needs_no_role_but_writing_needs_an_editor(monkeypatch, tmp_path):
    module = _app(monkeypatch, tmp_path)
    dispositions = _seed(module)
    viewer = _client(module, role="viewer")

    page = viewer.get(f"{API}/dispositions?asset={PFS}").get_json()
    assert page["counts"]["all"] == len(ACCEPTANCE)
    assert [option["asset_number"] for option in page["options"]["sub_assets"]] == [S09]
    assert viewer.get(f"{API}/code-lists").status_code == 200

    assert viewer.post(f"{API}/dispositions/save", json={"dispositions": dispositions}).status_code == 403
    assert viewer.post(f"{API}/code-lists", json={"code_list": "COMPONENT", "code": "X", "name": "X"}).status_code == 403
    selection = {"selection": {"assets": [PFS], "locations": [S09], "components": ["Filter"]}}
    assert viewer.post(f"{API}/weibull/save", json=selection).status_code == 403


def test_an_editor_dispositions_analyses_and_saves(monkeypatch, tmp_path):
    module = _app(monkeypatch, tmp_path)
    dispositions = _seed(module)
    editor = _client(module, role="editor")

    # S05 and S11 have no records of their own here, so only S09 is offered; the
    # dispositions naming them are left out.
    known = [item for item in dispositions if item.get("sub_asset") in (None, S09)]
    response = editor.post(f"{API}/dispositions/save", json={"dispositions": known})
    assert response.status_code == 200, response.get_json()
    assert response.get_json()["saved"] == len(known)

    record = editor.get(f"{API}/dispositions/record?mapped_record_id=175552").get_json()
    assert record["record"]["disposition"]["decided_by"] == "editor1"
    assert record["history"][0]["is_current"] == 1

    selection = {"assets": [PFS], "locations": [S09], "components": ["Filter"], "failure_mechanisms": ["CONT"]}
    patterns = editor.post(f"{API}/patterns", json={"selection": selection, "group_by": "failure_mode"}).get_json()
    # The filter PM is in the pattern views too, as found on inspection.
    assert {row["key"] for row in patterns["pareto"]["rows"]} == {"FTS", "UST", "LOO", "DEG"}

    preview = editor.post(f"{API}/weibull", json={"selection": selection, "analysis_cutoff": "2024-12-31"}).get_json()
    assert preview["result"]["analysis_id"] is None
    assert preview["result"]["population_definition"].startswith("POP: asset=4002; location=4002-S09")

    saved = editor.post(f"{API}/weibull/save", json={"selection": selection, "analysis_cutoff": "2024-12-31"}).get_json()
    analysis_id = saved["result"]["analysis_id"]
    assert analysis_id is not None
    listed = editor.get(f"{API}/weibull/saved?asset={PFS}").get_json()["analyses"]
    assert [row["analysis_id"] for row in listed] == [analysis_id]
    assert listed[0]["run_by"] == "editor1"
    read_back = editor.get(f"{API}/weibull/saved/{analysis_id}").get_json()["result"]
    assert read_back["population_definition"] == saved["result"]["population_definition"]


def test_a_refused_save_says_which_record_and_why(monkeypatch, tmp_path):
    module = _app(monkeypatch, tmp_path)
    _seed(module)
    editor = _client(module, role="editor")
    response = editor.post(
        f"{API}/dispositions/save",
        json={"dispositions": [{"mapped_record_id": 175552, "disposition_category": "HELD"}]},
    )
    assert response.status_code == 400
    assert response.get_json()["error"] == "Task 175552: HELD needs a disposition note saying what the record is missing."


def test_the_endpoints_refuse_malformed_requests(monkeypatch, tmp_path):
    module = _app(monkeypatch, tmp_path)
    _seed(module)
    client = _client(module, role="editor")
    assert client.get(f"{API}/dispositions").status_code == 400
    assert client.get(f"{API}/dispositions/record?mapped_record_id=abc").status_code == 400
    assert client.get(f"{API}/dispositions/record?mapped_record_id={2**70}").status_code == 404
    assert client.get(f"{API}/population-options").status_code == 400
    assert client.post(f"{API}/patterns", json={}).status_code == 400
    assert client.post(f"{API}/patterns", json={"selection": {}, "group_by": "colour"}).status_code == 400
    assert client.post(f"{API}/patterns", json={"selection": {}, "date_from": "May 1"}).status_code == 400
    assert client.post(f"{API}/dispositions/save", json={"dispositions": []}).status_code == 400
    assert client.get(f"{API}/weibull/saved/9999").status_code == 404


def test_the_pages_follow_the_life_data_analysis_rules(monkeypatch, tmp_path):
    module = _app(monkeypatch, tmp_path)
    anonymous = _client(module)
    viewer = _client(module, role="viewer")
    editor = _client(module, role="editor")

    # Analysis Population needs an account, like Perform an Analysis.
    assert anonymous.get("/life-data-analysis/population-analysis").status_code == 403
    assert viewer.get("/life-data-analysis/population-analysis").status_code == 200
    # The disposition editor needs an editor, like Disposition.
    assert viewer.get("/life-data-analysis/disposition/standard").status_code == 403
    page = editor.get("/life-data-analysis/disposition/standard")
    assert page.status_code == 200
    assert b"wo_disposition.js" in page.data
    # The existing pages link to the new ones.
    assert b"/life-data-analysis/disposition/standard" in editor.get("/life-data-analysis/disposition").data
    assert b"/life-data-analysis/population-analysis" in editor.get("/life-data-analysis/perform-analysis").data
