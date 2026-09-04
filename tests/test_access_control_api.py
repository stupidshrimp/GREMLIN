import importlib

import pytest


def _app(monkeypatch, tmp_path):
    monkeypatch.setenv("GREMLIN_ACCESS_DB_PATH", str(tmp_path / "accesscontrol.db"))
    monkeypatch.setenv("GREMLIN_ADMIN_USERNAME", "root")
    monkeypatch.setenv("GREMLIN_ADMIN_PIN", "secret")
    import app
    return importlib.reload(app)


def test_write_routes_require_login(monkeypatch, tmp_path):
    module = _app(monkeypatch, tmp_path)
    client = module.app.test_client()
    routes = [
        "/life-data-analysis/api/refresh-mapping",
        "/life-data-analysis/api/dispositions/save",
        "/life-data-analysis/api/dispositions/excel",
        "/life-data-analysis/api/perform-analysis",
        "/life-data-analysis/api/calculate-all",
        "/life-data-analysis/api/parameter-adjustment",
        "/life-data-analysis/api/weibull-report",
    ]
    for route in routes:
        assert client.post(route, json={}).status_code == 401, route
    assert client.get("/life-data-analysis/api/assets?refresh=1").status_code == 405


def test_saved_analysis_is_readable_without_logging_in(monkeypatch, tmp_path):
    """The read-only twin of perform-analysis must stay open to viewers and visitors.

    Performing an analysis writes, so it is editor-only; reading back an already-saved
    fit is what lets a signed-out visitor open the Weibull view. Asserting on the exact
    status would only restate the empty temp database, so this checks the request is not
    turned away by the role gate.
    """
    monkeypatch.setenv("GREMLIN_DB_PATH", str(tmp_path / "gremlin.db"))
    module = _app(monkeypatch, tmp_path)
    response = module.app.test_client().get(
        "/life-data-analysis/api/saved-analysis?asset=A-1&grouping_level=FAILURE_MECHANISM"
        "&failure_mode_id=1&failure_mechanism_id=1"
    )
    assert response.status_code not in (401, 403, 405)
    assert response.is_json


def test_fresh_database_has_no_published_login(monkeypatch, tmp_path):
    monkeypatch.setenv("GREMLIN_ACCESS_DB_PATH", str(tmp_path / "empty.db"))
    monkeypatch.delenv("GREMLIN_ADMIN_USERNAME", raising=False)
    monkeypatch.delenv("GREMLIN_ADMIN_PIN", raising=False)
    import app
    module = importlib.reload(app)
    response = module.app.test_client().post(
        "/auth/login", json={"username": "admin", "pin": "1336"}
    )
    assert response.status_code == 503
    assert "No accounts are configured" in response.get_json()["error"]


def test_login_rejects_non_object_json(monkeypatch, tmp_path):
    module = _app(monkeypatch, tmp_path)
    client = module.app.test_client()
    for payload in ('"x"', "[1]", "42", "null"):
        response = client.post("/auth/login", data=payload, content_type="application/json")
        assert response.status_code == 400
        assert response.is_json
        assert "JSON object" in response.get_json()["error"]


def test_login_rejects_cross_origin_form_posts(monkeypatch, tmp_path):
    module = _app(monkeypatch, tmp_path)
    response = module.app.test_client().post(
        "/auth/login", data={"username": "root", "pin": "secret"}
    )
    assert response.status_code == 415
    assert response.is_json


def test_login_failures_are_throttled_by_account_and_client(monkeypatch, tmp_path):
    module = _app(monkeypatch, tmp_path)
    client = module.app.test_client()
    for _ in range(4):
        assert client.post("/auth/login", json={"username": "root", "pin": "wrong"}).status_code == 401
    response = client.post("/auth/login", json={"username": "root", "pin": "wrong"})
    assert response.status_code == 429
    assert int(response.headers["Retry-After"]) > 0
    # Even the correct PIN is held until the temporary lock expires.
    assert client.post("/auth/login", json={"username": "root", "pin": "secret"}).status_code == 429


def test_bodyless_write_routes_require_json(monkeypatch, tmp_path):
    module = _app(monkeypatch, tmp_path)
    client = module.app.test_client()
    assert client.post("/auth/login", json={"username": "root", "pin": "secret"}).status_code == 200
    assert client.post("/life-data-analysis/api/refresh-mapping").status_code == 415
    assert client.post("/metrics/api/availability/config/group/test/reset").status_code == 415


def test_audit_failure_does_not_turn_committed_write_into_failure(monkeypatch, tmp_path):
    module = _app(monkeypatch, tmp_path)
    user = module.access_control.authenticate("root", "secret")

    def audit_failure(_user, _action):
        raise module.sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(module.access_control, "record_change", audit_failure)
    protected = module.requires_role("editor")(lambda: module.jsonify({"saved": True}))
    with module.app.test_request_context("/test-write", method="POST"):
        module.session["user"] = user
        response = module.make_response(protected())

    assert response.status_code == 200
    assert response.get_json() == {"saved": True}
    assert response.headers["X-GREMLIN-Audit-Warning"] == "audit-entry-not-stored"


def test_role_is_revalidated_on_protected_request(monkeypatch, tmp_path):
    module = _app(monkeypatch, tmp_path)
    module.access_control.save_user(None, "editor", "2468", "editor")
    client = module.app.test_client()
    assert client.post("/auth/login", json={"username": "editor", "pin": "2468"}).status_code == 200
    user = module.access_control.authenticate("editor", "2468")
    module.access_control.save_user(user["id"], "editor", "", "viewer")
    response = client.post("/life-data-analysis/api/refresh-mapping", json={})
    assert response.status_code == 403


def test_pin_change_revokes_existing_session(monkeypatch, tmp_path):
    module = _app(monkeypatch, tmp_path)
    module.access_control.save_user(None, "editor", "old-pin", "editor")
    client = module.app.test_client()
    assert client.post("/auth/login", json={"username": "editor", "pin": "old-pin"}).status_code == 200
    user = module.access_control.authenticate("editor", "old-pin")
    module.access_control.save_user(user["id"], "editor", "new-pin", "editor")

    response = client.post("/life-data-analysis/api/refresh-mapping", json={})
    assert response.status_code == 401
    with client.session_transaction() as browser_session:
        assert "user" not in browser_session
    assert module.access_control.authenticate("editor", "old-pin") is None
    assert module.access_control.authenticate("editor", "new-pin") is not None


def test_account_management_requires_session_csrf_token(monkeypatch, tmp_path):
    module = _app(monkeypatch, tmp_path)
    client = module.app.test_client()
    assert client.post("/auth/login", json={"username": "root", "pin": "secret"}).status_code == 200
    # Department and level are part of the account form now, and save_user
    # validates them rather than defaulting them, so a post that omits either is
    # rejected before CSRF is reached -- which would make this test pass for the
    # wrong reason.
    form = {
        "username": "attacker",
        "pin": "known-pin",
        "role": "admin",
        "department": "all",
        "staff_level": "all",
    }

    assert client.post("/developer/access/users", data=form).status_code == 403
    assert client.post(
        "/developer/access/users", data={**form, "csrf_token": "forged"}
    ).status_code == 403
    assert module.access_control.authenticate("attacker", "known-pin") is None

    # Rendering the trusted form establishes a token in this signed session.
    assert client.get("/developer").status_code == 200
    with client.session_transaction() as session:
        token = session["csrf_token"]
    response = client.post(
        "/developer/access/users", data={**form, "csrf_token": token}
    )
    assert response.status_code == 302
    assert module.access_control.authenticate("attacker", "known-pin")["role"] == "admin"


def test_disposition_spreadsheet_import_requires_csrf(monkeypatch, tmp_path):
    module = _app(monkeypatch, tmp_path)
    module.access_control.save_user(None, "editor", "2468", "editor")
    client = module.app.test_client()
    assert client.post("/auth/login", json={"username": "editor", "pin": "2468"}).status_code == 200

    url = "/life-data-analysis/api/dispositions/excel?asset=A1&kind=wo"
    response = client.post(url, data={"file": (b"not-a-workbook", "attack.xlsx")})
    assert response.status_code == 403
    assert "form expired" in response.get_json()["error"].lower()

    # A token issued into the same signed session passes the CSRF gate; the
    # intentionally invalid workbook then reaches normal upload validation.
    with client.session_transaction() as browser_session:
        browser_session["csrf_token"] = "trusted-token"
    response = client.post(
        url,
        data={"csrf_token": "trusted-token", "file": (b"not-a-workbook", "attack.xlsx")},
    )
    assert response.status_code != 403


def _assert_read_only_ui(client):
    """No page offers a control that writes, whoever this read-only caller is."""
    configuration = client.get("/configuration").data
    assert b"Asset group schedules" not in configuration
    assert b"Refresh CMMS mapping" not in configuration
    assert b"Linked downtime rules" not in configuration
    # Named rather than silently missing: the section says who may edit it.
    assert b"editable by authorized users only" in configuration
    analysis = client.get("/life-data-analysis/perform-analysis").data
    assert b'id="lda-perform" hidden' in analysis
    assert b'id="lda-disposition-wo" hidden' in analysis
    assert b'id="lda-disposition-pm" hidden' in analysis
    # Not merely hidden: this card is nothing but a write, and selecting an
    # asset is what un-hides it, so it must not be in the page at all.
    assert b"lda-calculate-all" not in analysis
    assert client.get("/life-data-analysis/disposition").status_code == 403
    # The workflow cards moved onto the home page, so that is where the
    # Disposition card explains itself to a caller who may only read: still on
    # the page, but a button that names the role rather than a link into a 403.
    landing = client.get("/").data
    assert b"life-data-card-locked" in landing
    assert b"Only authorized users can open Disposition." in landing
    assert b'href="/life-data-analysis/disposition"' not in landing
    assert b'name="gremlin-can-edit" content="false"' in landing


def test_guest_ui_offers_no_write_controls(monkeypatch, tmp_path):
    module = _app(monkeypatch, tmp_path)
    client = module.app.test_client()
    _assert_read_only_ui(client)
    # Nobody is signed in, so the card sends them to the login rather than to
    # an administrator who cannot help until they have an account.
    assert b"Log in with the person icon" in client.get("/").data


def test_viewer_ui_offers_no_write_controls(monkeypatch, tmp_path):
    """A signed-in viewer can write no more than a signed-out visitor can."""
    module = _app(monkeypatch, tmp_path)
    module.access_control.save_user(None, "viewer", "1357", "viewer")
    client = module.app.test_client()
    assert client.post("/auth/login", json={"username": "viewer", "pin": "1357"}).status_code == 200
    _assert_read_only_ui(client)
    # This one already has an account: logging in again would change nothing,
    # so the card names the role their account is missing instead.
    assert b"It needs the editor role" in client.get("/").data


def test_refused_disposition_page_explains_itself(monkeypatch, tmp_path):
    module = _app(monkeypatch, tmp_path)
    module.access_control.save_user(None, "viewer", "1357", "viewer")
    client = module.app.test_client()
    assert client.post("/auth/login", json={"username": "viewer", "pin": "1357"}).status_code == 200
    response = client.get("/life-data-analysis/disposition")
    assert response.status_code == 403
    # The refusal names the role and the account, rather than quietly serving
    # some other page that looks like nothing went wrong.
    assert b"Disposition is for editors." in response.data
    assert b"editor role" in response.data
    assert b"viewer" in response.data


def test_editor_ui_exposes_editing_and_disposition_controls(monkeypatch, tmp_path):
    module = _app(monkeypatch, tmp_path)
    module.access_control.save_user(None, "editor", "2468", "editor")
    client = module.app.test_client()
    assert client.post("/auth/login", json={"username": "editor", "pin": "2468"}).status_code == 200
    configuration = client.get("/configuration").data
    assert b"Asset group schedules" in configuration
    assert b"Refresh CMMS mapping" in configuration
    analysis = client.get("/life-data-analysis/perform-analysis").data
    assert b'id="lda-perform" hidden' not in analysis
    assert b"lda-calculate-all" in analysis
    assert client.get("/life-data-analysis/disposition").status_code == 200
    # An editor gets the card as a working link, with nothing left to explain.
    landing = client.get("/").data
    assert b'href="/life-data-analysis/disposition"' in landing
    assert b"life-data-card-locked" not in landing


def test_leaving_the_developer_page_needs_the_session_csrf_token(monkeypatch, tmp_path):
    module = _app(monkeypatch, tmp_path)
    client = module.app.test_client()
    assert client.post("/auth/login", json={"username": "root", "pin": "secret"}).status_code == 200

    # The button now ends the whole session, so a cross-site form must not be
    # able to press it.
    assert client.post("/developer/lock").status_code == 403
    assert client.get("/developer").status_code == 200

    with client.session_transaction() as browser_session:
        token = browser_session["csrf_token"]
    assert client.post("/developer/lock", data={"csrf_token": token}).status_code == 302
    assert client.get("/developer").status_code == 403


def test_access_database_defaults_next_to_configured_gremlin_database(monkeypatch, tmp_path):
    gremlin_path = tmp_path / "data" / "GREMLIN.db"
    gremlin_path.parent.mkdir()
    monkeypatch.setenv("GREMLIN_DB_PATH", str(gremlin_path))
    monkeypatch.delenv("GREMLIN_ACCESS_DB_PATH", raising=False)
    monkeypatch.setenv("GREMLIN_ADMIN_USERNAME", "root")
    monkeypatch.setenv("GREMLIN_ADMIN_PIN", "secret")
    import app
    module = importlib.reload(app)
    assert module.ACCESS_DB_PATH == gremlin_path.with_name("accesscontrol.db")
    assert module.ACCESS_DB_PATH.exists()


def test_workers_sharing_an_access_database_sign_cookies_alike(monkeypatch, tmp_path):
    """Two processes must accept each other's sessions with no configuration.

    A key generated per process would make an authenticated write fail
    whenever the next request happened to land on a different worker, and
    would sign everyone out on restart. Reloading the module is the closest
    this suite gets to a second worker starting against the same database.
    """
    monkeypatch.delenv("GREMLIN_SECRET_KEY", raising=False)
    first = _app(monkeypatch, tmp_path).app.secret_key
    second = _app(monkeypatch, tmp_path).app.secret_key

    assert first == second
    assert len(first) >= 32


def test_an_exported_secret_key_still_wins(monkeypatch, tmp_path):
    monkeypatch.setenv("GREMLIN_SECRET_KEY", "operator-chosen-key")
    assert _app(monkeypatch, tmp_path).app.secret_key == "operator-chosen-key"


def test_logout_requires_the_session_csrf_token(monkeypatch, tmp_path):
    """Signing out is a session write, and /developer/lock already guards it.

    SameSite keeps the cookie off a cross-site form post, but a hostile page on
    another origin of the same site is still same-site, and being signed out
    mid-edit is a nuisance somebody would have to diagnose.
    """
    module = _app(monkeypatch, tmp_path)
    client = module.app.test_client()
    assert client.post("/auth/login", json={"username": "root", "pin": "secret"}).status_code == 200

    assert client.post("/auth/logout").status_code == 403
    assert client.post("/auth/logout", data={"csrf_token": "forged"}).status_code == 403
    # Still signed in: the refused requests changed nothing.
    with client.session_transaction() as browser_session:
        assert browser_session["user"]["username"] == "root"

    # The token the page carries is issued by rendering a page in this session.
    assert b'name="gremlin-csrf-token"' in client.get("/").data
    with client.session_transaction() as browser_session:
        token = browser_session["csrf_token"]
    assert client.post("/auth/logout", data={"csrf_token": token}).status_code == 200
    with client.session_transaction() as browser_session:
        assert "user" not in browser_session


def test_the_login_limiter_sees_the_real_client_behind_a_trusted_proxy(monkeypatch, tmp_path):
    """Otherwise the proxy's address is one shared client for the whole plant.

    Five wrong PINs from anybody would lock that shared scope and hand every
    other user a 429 for fifteen minutes, correct credentials included.
    """
    monkeypatch.setenv("GREMLIN_TRUSTED_PROXY_HOPS", "1")
    module = _app(monkeypatch, tmp_path)
    client = module.app.test_client()

    # Four failures from one forwarded client, then a fifth that locks it.
    for _ in range(5):
        client.post(
            "/auth/login",
            json={"username": "root", "pin": "wrong"},
            headers={"X-Forwarded-For": "10.0.0.9"},
        )
    locked = client.post(
        "/auth/login", json={"username": "nobody", "pin": "x"},
        headers={"X-Forwarded-For": "10.0.0.9"},
    )
    assert locked.status_code == 429

    # A different browser behind the same proxy is unaffected by that lockout.
    other = client.post(
        "/auth/login", json={"username": "nobody", "pin": "x"},
        headers={"X-Forwarded-For": "10.0.0.10"},
    )
    assert other.status_code == 401


def test_a_nonsense_proxy_hop_count_is_refused_rather_than_ignored(monkeypatch, tmp_path):
    """Silently ignoring it would leave the deployment believing it is throttled."""
    module = _app(monkeypatch, tmp_path)
    for value in ("yes", "0", "-1", "1.5", "one"):
        with pytest.raises(RuntimeError, match="GREMLIN_TRUSTED_PROXY_HOPS"):
            module._parse_trusted_proxy_hops(value)
    # Absent means "served directly", which is the ordinary case and not an error.
    assert module._parse_trusted_proxy_hops(None) == 0
    assert module._parse_trusted_proxy_hops("   ") == 0
    assert module._parse_trusted_proxy_hops(" 2 ") == 2


# -- department and staff level ------------------------------------------------


def _admin_client(module):
    client = module.app.test_client()
    assert client.post("/auth/login", json={"username": "root", "pin": "secret"}).status_code == 200
    assert client.get("/developer").status_code == 200
    with client.session_transaction() as session:
        return client, session["csrf_token"]


def test_the_access_page_offers_every_department_and_level(monkeypatch, tmp_path):
    module = _app(monkeypatch, tmp_path)
    client, _token = _admin_client(module)
    body = client.get("/developer/access").get_data(as_text=True)

    assert 'name="department"' in body
    assert 'name="staff_level"' in body
    for department in module.DEPARTMENTS:
        assert f'value="{department}"' in body, department
    for level in module.STAFF_LEVELS:
        assert f'value="{level}"' in body, level
    assert "Operations &amp; Maintenance" in body, "the combined department is drawn by its label"


def test_an_administrator_can_set_and_then_change_them(monkeypatch, tmp_path):
    module = _app(monkeypatch, tmp_path)
    client, token = _admin_client(module)

    created = client.post("/developer/access/users", data={
        "csrf_token": token, "username": "planner", "pin": "2468",
        "role": "editor", "department": "maintenance", "staff_level": "associate",
    })
    assert created.status_code == 302
    stored = module.access_control.authenticate("planner", "2468")
    assert (stored["department"], stored["staff_level"]) == ("maintenance", "associate")

    # The Save button on an existing row: no new PIN, both fields moved.
    changed = client.post("/developer/access/users", data={
        "csrf_token": token, "user_id": str(stored["id"]), "username": "planner", "pin": "",
        "role": "editor", "department": "operations_maintenance", "staff_level": "leadership",
    })
    assert changed.status_code == 302
    moved = module.access_control.authenticate("planner", "2468")
    assert (moved["department"], moved["staff_level"]) == ("operations_maintenance", "leadership")


@pytest.mark.parametrize("field,value", [("department", "engineering"), ("staff_level", "manager")])
def test_a_value_outside_the_catalog_is_a_bad_request(monkeypatch, tmp_path, field, value):
    module = _app(monkeypatch, tmp_path)
    client, token = _admin_client(module)
    form = {
        "csrf_token": token, "username": "planner", "pin": "2468",
        "role": "editor", "department": "operations", "staff_level": "engineer",
        field: value,
    }
    response = client.post("/developer/access/users", data=form)
    assert response.status_code == 400
    assert module.access_control.authenticate("planner", "2468") is None


def test_neither_field_changes_what_an_account_may_reach(monkeypatch, tmp_path):
    """Nothing is gated on department or level yet, and this is what says so.

    Whichever page these two eventually hide, it is not hidden today: an editor
    in one department reaches exactly what an editor in another does. Expect to
    rewrite this test the day a filtering rule is written -- that is its job.
    """

    module = _app(monkeypatch, tmp_path)
    module.access_control.save_user(None, "one", "1111", "editor", "facilities", "associate")
    module.access_control.save_user(None, "two", "2222", "editor", "operations", "leadership")

    pages = ["/", "/life-data-analysis/perform-analysis", "/life-data-analysis/disposition"]
    seen = []
    for username, pin in (("one", "1111"), ("two", "2222")):
        client = module.app.test_client()
        user = module.access_control.authenticate(username, pin)
        with client.session_transaction() as session:
            session["user"] = user
        seen.append([client.get(page).status_code for page in pages])
    assert seen[0] == seen[1]
    assert 403 not in seen[0], "an editor is turned away from none of these"


def test_the_account_dialog_shows_what_the_account_records(monkeypatch, tmp_path):
    module = _app(monkeypatch, tmp_path)
    module.access_control.save_user(None, "planner", "2468", "editor", "operations_maintenance", "engineer")
    client = module.app.test_client()
    with client.session_transaction() as session:
        session["user"] = module.access_control.authenticate("planner", "2468")

    # Asserted on the dialog's own line rather than on the page: "Engineer" is a
    # common enough word on Home that searching the whole body would pass whether
    # or not the dialog drew anything.
    body = client.get("/").get_data(as_text=True)
    line = next(part for part in body.split("<p") if 'class="account-dialog-scope"' in part)
    assert "Operations &amp; Maintenance" in line
    assert "Engineer" in line
