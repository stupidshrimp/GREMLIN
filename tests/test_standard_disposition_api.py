"""Life Data Analysis's endpoints and pages, read from the work-order disposition standard.

Reads are open to whoever may see Life Data Analysis; every write -- a
disposition, a code, a saved Weibull run, an adjustment, a numbered report --
needs an editor, and records who made it.
"""

import importlib

from tests.test_standard_disposition_service import ACCEPTANCE, ASSET_IDS, PFS, S09

API = "/life-data-analysis/api"


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

    # The Disposition tour's example is the asset with the most work orders.
    assert editor.get(f"{API}/disposition-tour-example").get_json() == {"asset_number": PFS}

    record = editor.get(f"{API}/dispositions/record?mapped_record_id=175552").get_json()
    assert record["record"]["disposition"]["decided_by"] == "editor1"
    assert record["history"][0]["is_current"] == 1

    selection = {"assets": [PFS], "locations": [S09], "components": ["Filter"], "failure_mechanisms": ["CONT"]}
    summary = editor.post(f"{API}/summary", json={"selection": selection, "group_by": "failure_mode"}).get_json()
    # The pattern views default to every category the spec marks for them, so the
    # filter PM shows too, as found on inspection.
    assert {row["key"] for row in summary["pareto"]["rows"]} == {"FTS", "UST", "LOO", "DEG"}
    failures_only = {"selection": {**selection, "categories": ["FAILURE"]}}
    assert editor.post(f"{API}/downtime-drivers", json=failures_only).get_json()["summary"]["work_order_count"] == 4
    assert editor.post(f"{API}/pm-effectiveness", json=failures_only).get_json()["pms_performed"] == 1
    assert editor.post(f"{API}/repeat-fixes", json={**failures_only, "window_hours": 24}).get_json()["window_hours"] == 24

    preview = editor.post(f"{API}/weibull", json={"selection": selection, "analysis_cutoff": "2024-12-31"}).get_json()
    assert preview["result"]["analysis_id"] is None
    assert preview["result"]["population_definition"].startswith("POP: asset=4002; location=4002-S09")
    assert editor.post(f"{API}/saved-analysis", json={"selection": selection}).get_json()["result"] is None

    saved = editor.post(f"{API}/weibull/save", json={"selection": selection, "analysis_cutoff": "2024-12-31"}).get_json()
    analysis_id = saved["result"]["analysis_id"]
    assert analysis_id is not None
    listed = editor.get(f"{API}/saved-analyses?asset={PFS}").get_json()["analyses"]
    assert [row["analysis_id"] for row in listed] == [analysis_id]
    assert listed[0]["run_by"] == "editor1"
    read_back = editor.get(f"{API}/saved-analysis/{analysis_id}").get_json()["result"]
    assert read_back["population_definition"] == saved["result"]["population_definition"]
    latest = editor.post(f"{API}/saved-analysis", json={"selection": {**selection, "categories": ["FAILURE"]}}).get_json()
    assert latest["result"]["analysis_id"] == analysis_id
    # Four filter failures are too few to fit, so there is nothing to adjust or report.
    assert editor.post(f"{API}/parameter-adjustment", json={
        "analysis_id": analysis_id, "beta": 2, "eta": 900, "reason": "why"}).status_code == 400
    report = editor.post(f"{API}/weibull-report", json={"analysis_id": analysis_id})
    assert report.status_code == 400
    assert "needs at least 5" in report.get_json()["error"]


def test_a_viewer_reads_every_analysis_but_cannot_save_one(monkeypatch, tmp_path):
    module = _app(monkeypatch, tmp_path)
    _seed(module)
    viewer = _client(module, role="viewer")
    selection = {"selection": {"assets": [PFS]}}
    for path in ("summary", "downtime-drivers", "pm-effectiveness", "repeat-fixes", "weibull", "saved-analysis"):
        assert viewer.post(f"{API}/{path}", json=selection).status_code == 200, path
    assert viewer.get(f"{API}/population-options?asset={PFS}").status_code == 200
    assert viewer.get(f"{API}/tour-example").status_code == 200
    for path in ("parameter-adjustment", "weibull-report"):
        assert viewer.post(f"{API}/{path}", json={"analysis_id": 1}).status_code == 403, path
    assert viewer.get(f"{API}/dispositions/excel?asset={PFS}").status_code == 200


def test_the_workbook_round_trips_through_the_api(monkeypatch, tmp_path):
    import io

    module = _app(monkeypatch, tmp_path)
    _seed(module)
    editor = _client(module, role="editor")
    workbook = editor.get(f"{API}/dispositions/excel?asset={PFS}&scope=not_reviewed")
    assert workbook.status_code == 200
    assert "not_reviewed" in workbook.headers["Content-Disposition"]
    with editor.session_transaction() as session:
        session["csrf_token"] = token = "form-token"
    response = editor.post(
        f"{API}/dispositions/excel?asset={PFS}",
        data={"file": (io.BytesIO(workbook.data), "dispositions.xlsx"), "csrf_token": token},
        content_type="multipart/form-data",
    )
    assert response.status_code == 200, response.get_json()
    assert response.get_json() == {
        "saved": 0, "unchanged": len(ACCEPTANCE), "kept_newer": [], "columns_not_read": [], "columns_missing": [],
    }
    garbage = editor.post(
        f"{API}/dispositions/excel?asset={PFS}",
        data={"file": (io.BytesIO(b"not a workbook"), "dispositions.xlsx"), "csrf_token": token},
        content_type="multipart/form-data",
    )
    assert garbage.status_code == 400


def test_the_list_sorts_filters_and_downloads_what_it_shows(monkeypatch, tmp_path):
    import io
    import json
    import zipfile

    module = _app(monkeypatch, tmp_path)
    dispositions = _seed(module)
    viewer = _client(module, role="viewer")
    # Only S09 has a record of its own here, so only it is a sub-asset.
    module._standard_service().save_dispositions(
        [disposition for disposition in dispositions if disposition.get("sub_asset") in (S09, None)], decided_by="tim"
    )
    filters = json.dumps({"component": ["Filter"]})
    page = viewer.get(f"{API}/dispositions?asset={PFS}&filters={filters}&sort=task_id&dir=desc").get_json()
    task_ids = [int(row["task_id"]) for row in page["rows"]]
    assert task_ids and task_ids == sorted(task_ids, reverse=True)
    assert {row["disposition"]["component_code"] for row in page["rows"]} == {"Filter"}
    assert page["sort"] == {"key": "task_id", "dir": "desc"} and page["filters"] == {"component": ["Filter"]}

    values = viewer.get(f"{API}/dispositions/column-values?asset={PFS}&column=component&filters={filters}").get_json()
    assert "Filter" in [item["value"] for item in values["values"]] and len(values["values"]) > 1
    assert viewer.get(f"{API}/dispositions/column-values?asset={PFS}&column=colour").status_code == 400
    assert viewer.get(f"{API}/dispositions?asset={PFS}&filters=not-json").status_code == 400

    # The workbook holds the rows the list shows: a header and one row each.
    workbook = viewer.get(f"{API}/dispositions/excel?asset={PFS}&filters={filters}")
    with zipfile.ZipFile(io.BytesIO(workbook.data)) as archive:
        sheet = archive.read("xl/worksheets/sheet1.xml").decode()
    assert sheet.count("<row ") == page["displayed_count"] + 1


def test_a_save_hands_back_the_saved_record_and_history_comes_alone(monkeypatch, tmp_path):
    module = _app(monkeypatch, tmp_path)
    dispositions = _seed(module)
    editor = _client(module, role="editor")
    one = dispositions[0]
    saved = editor.post(f"{API}/dispositions/save", json={"dispositions": [one]}).get_json()
    assert saved["saved"] == 1
    [record] = saved["records"]
    # The record as the page's list holds it, labels and all.
    assert record["mapped_record_id"] == one["mapped_record_id"]
    assert record["disposition"]["disposition_category"] == one["disposition_category"]
    assert {"asset_label", "location_label", "plant_times"} <= set(record)
    history = editor.get(f"{API}/dispositions/record?mapped_record_id={one['mapped_record_id']}&history_only=1").get_json()
    assert set(history) == {"history"} and len(history["history"]) == 1


def test_an_editor_deletes_an_unused_code_and_a_viewer_cannot(monkeypatch, tmp_path):
    module = _app(monkeypatch, tmp_path)
    _seed(module)
    editor = _client(module, role="editor")
    assert editor.post(f"{API}/code-lists", json={"code_list": "COMPONENT", "code": "Boiler", "name": "Boiler"}).status_code == 200
    viewer = _client(module, role="viewer")
    assert viewer.post(f"{API}/code-lists/delete", json={"code_list": "COMPONENT", "code": "Boiler"}).status_code == 403
    deleted = editor.post(f"{API}/code-lists/delete", json={"code_list": "COMPONENT", "code": "Boiler"})
    assert deleted.status_code == 200
    assert "Boiler" not in [entry["code"] for entry in deleted.get_json()["code_lists"]["COMPONENT"]["codes"]]
    missing = editor.post(f"{API}/code-lists/delete", json={"code_list": "COMPONENT", "code": "Boiler"})
    assert missing.status_code == 400 and "not in the component list" in missing.get_json()["error"]
    assert editor.post(f"{API}/code-lists/delete", data="x").status_code == 400


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
    assert client.post(f"{API}/summary", json={}).status_code == 400
    assert client.post(f"{API}/summary", json={"selection": {}, "group_by": "colour"}).status_code == 400
    assert client.post(f"{API}/summary", json={"selection": {}, "date_from": "May 1"}).status_code == 400
    assert client.post(f"{API}/summary", json={"selection": {}, "weeks": 99}).status_code == 400
    assert client.post(f"{API}/repeat-fixes", json={"selection": {}, "window_hours": 9999}).status_code == 400
    assert client.post(f"{API}/dispositions/save", json={"dispositions": []}).status_code == 400
    assert client.get(f"{API}/saved-analysis/9999").status_code == 404
    assert client.post(f"{API}/parameter-adjustment", json={"analysis_id": 9999, "beta": 1, "eta": 1, "reason": "x"}).status_code == 404
    assert client.post(f"{API}/weibull-report", json={"analysis_id": 9999}).status_code == 404
    assert client.post(f"{API}/weibull-report", json={}).status_code == 400


def test_one_disposition_page_and_one_analysis_page(monkeypatch, tmp_path):
    module = _app(monkeypatch, tmp_path)
    anonymous = _client(module)
    viewer = _client(module, role="viewer")
    editor = _client(module, role="editor")

    assert anonymous.get("/life-data-analysis/perform-analysis").status_code == 403
    analysis = viewer.get("/life-data-analysis/perform-analysis")
    assert analysis.status_code == 200
    assert b"life_data_analysis.js" in analysis.data
    assert b"disposition_form.js" in analysis.data
    assert viewer.get("/life-data-analysis/disposition").status_code == 403
    page = editor.get("/life-data-analysis/disposition")
    assert page.status_code == 200
    assert b"disposition.js" in page.data
    assert b"life_data_analysis.js" not in page.data
    # The pages that came before are gone, not left beside these.
    for retired in ("/life-data-analysis/population-analysis", "/life-data-analysis/disposition/standard"):
        assert editor.get(retired).status_code == 404, retired
    assert editor.get(f"{API}/standard/code-lists").status_code == 404


def test_the_code_lists_workbook_is_an_administrators(monkeypatch, tmp_path):
    import io

    module = _app(monkeypatch, tmp_path)
    _seed(module)
    excel = "/developer/api/code-lists/excel"
    # The page and both halves of the workbook are kept to administrators: an
    # editor has the one-code-at-a-time dialog, but not the bulk upload.
    editor = _client(module, role="editor")
    assert editor.get("/developer/code-lists").status_code == 403
    assert editor.get(excel).status_code == 403
    with editor.session_transaction() as session:
        session["csrf_token"] = "form-token"
    refused = editor.post(
        excel,
        data={"file": (io.BytesIO(b"x"), "code_lists.xlsx"), "csrf_token": "form-token"},
        content_type="multipart/form-data",
    )
    assert refused.status_code == 403

    admin = _client(module, role="admin")
    page = admin.get("/developer/code-lists")
    assert page.status_code == 200
    assert b"Download code lists" in page.data
    workbook = admin.get(excel)
    assert workbook.status_code == 200
    assert "gremlin_code_lists_" in workbook.headers["Content-Disposition"]

    # A form from another session is refused before the file is read.
    with admin.session_transaction() as session:
        session["csrf_token"] = token = "form-token"
    stale = admin.post(
        excel,
        data={"file": (io.BytesIO(workbook.data), "code_lists.xlsx"), "csrf_token": "another"},
        content_type="multipart/form-data",
    )
    assert stale.status_code == 403

    life = module.get_life_data_service()
    rows = [list(row) for row in life._read_xlsx(io.BytesIO(workbook.data))]
    rows = [row + [None] * (len(rows[0]) - len(row)) for row in rows]
    blank = next(row for row in rows[1:] if not any(row))
    for column, value in {"code_list": "COMPONENT", "code": "Boiler", "name": "Boiler / water heater"}.items():
        blank[rows[0].index(column)] = value
    filled = tmp_path / "filled.xlsx"
    life._write_xlsx(filled, rows, "Code lists")
    response = admin.post(
        excel,
        data={"file": (io.BytesIO(filled.read_bytes()), "code_lists.xlsx"), "csrf_token": token},
        content_type="multipart/form-data",
    )
    assert response.status_code == 200, response.get_json()
    outcome = response.get_json()
    assert [item["code"] for item in outcome["added"]] == ["Boiler"]
    assert outcome["updated"] == []
    assert "Boiler" in [code["code"] for code in outcome["code_lists"]["COMPONENT"]["codes"]]
    codes = admin.get(f"{API}/code-lists").get_json()["code_lists"]["COMPONENT"]["codes"]
    assert "Boiler" in [code["code"] for code in codes]

    # A refused row comes back as a 400 that names it, and a file that is not a
    # workbook as one that says so.
    blank[rows[0].index("code")] = "Light"
    blank[rows[0].index("name")] = None
    life._write_xlsx(filled, rows, "Code lists")
    bad = admin.post(
        excel,
        data={"file": (io.BytesIO(filled.read_bytes()), "code_lists.xlsx"), "csrf_token": token},
        content_type="multipart/form-data",
    )
    assert bad.status_code == 400
    assert "(Component Light): Give the code a name." in bad.get_json()["error"]
    garbage = admin.post(
        excel,
        data={"file": (io.BytesIO(b"not a workbook"), "code_lists.xlsx"), "csrf_token": token},
        content_type="multipart/form-data",
    )
    assert garbage.status_code == 400
    assert "not a workbook" in garbage.get_json()["error"]
    missing = admin.post(excel, data={"csrf_token": token}, content_type="multipart/form-data")
    assert missing.status_code == 400
