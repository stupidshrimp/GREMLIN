import importlib


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
    assert client.get("/life-data-analysis/api/assets?refresh=1").status_code == 401


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


def test_role_is_revalidated_on_protected_request(monkeypatch, tmp_path):
    module = _app(monkeypatch, tmp_path)
    module.access_control.save_user(None, "editor", "2468", "editor")
    client = module.app.test_client()
    assert client.post("/auth/login", json={"username": "editor", "pin": "2468"}).status_code == 200
    user = module.access_control.authenticate("editor", "2468")
    module.access_control.save_user(user["id"], "editor", "", "viewer")
    response = client.post("/life-data-analysis/api/refresh-mapping", json={})
    assert response.status_code == 403


def test_account_management_requires_session_csrf_token(monkeypatch, tmp_path):
    module = _app(monkeypatch, tmp_path)
    client = module.app.test_client()
    assert client.post("/auth/login", json={"username": "root", "pin": "secret"}).status_code == 200
    form = {"username": "attacker", "pin": "known-pin", "role": "admin"}

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
