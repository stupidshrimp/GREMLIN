"""The developer area's user-activity page and the endpoint behind it."""

import importlib

import pytest


def _app(monkeypatch, tmp_path):
    monkeypatch.setenv("GREMLIN_ACCESS_DB_PATH", str(tmp_path / "accesscontrol.db"))
    monkeypatch.setenv("GREMLIN_ADMIN_USERNAME", "root")
    monkeypatch.setenv("GREMLIN_ADMIN_PIN", "secret")
    import app
    return importlib.reload(app)


def _signed_in(module, username="root", pin="secret"):
    client = module.app.test_client()
    user = module.access_control.authenticate(username, pin)
    with client.session_transaction() as session:
        session["user"] = user
    return client


def test_activity_is_admin_only(monkeypatch, tmp_path):
    module = _app(monkeypatch, tmp_path)
    anonymous = module.app.test_client()
    assert anonymous.get("/developer/activity").status_code == 403
    assert anonymous.get("/developer/api/activity").status_code == 403

    module.access_control.save_user(None, "operator", "2468", "editor")
    editor = _signed_in(module, "operator", "2468")
    assert editor.get("/developer/activity").status_code == 403
    assert editor.get("/developer/api/activity").status_code == 403


def test_the_page_renders_for_an_administrator(monkeypatch, tmp_path):
    module = _app(monkeypatch, tmp_path)
    response = _signed_in(module).get("/developer/activity")
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "User activity" in body
    # The sub-navigation carries the new tab on every developer page.
    assert "/developer/activity" in _signed_in(module).get("/developer/database").get_data(as_text=True)


def test_a_login_through_the_endpoint_shows_up_in_the_report(monkeypatch, tmp_path):
    module = _app(monkeypatch, tmp_path)
    client = module.app.test_client()
    assert client.post("/auth/login", json={"username": "root", "pin": "secret"}).status_code == 200
    assert client.post("/auth/login", json={"username": "root", "pin": "wrong"}).status_code == 401

    payload = _signed_in(module).get("/developer/api/activity?days=7").get_json()
    assert payload["window"]["days"] == 7
    assert payload["summary"]["successful_logins"] == 1
    assert payload["summary"]["failed_logins"] == 1
    assert payload["summary"]["accounts"] == 1
    assert payload["access_db_path"] == str(module.ACCESS_DB_PATH)
    assert [user["username"] for user in payload["users"]] == ["root"]
    assert len(payload["daily"]) == 7
    assert len(payload["hourly"]) == 24
    assert payload["recent_logins"][0]["outcome"] == "failure"
    assert payload["retention"]["login_events"] == 2


def test_the_default_window_is_used_when_none_is_given(monkeypatch, tmp_path):
    module = _app(monkeypatch, tmp_path)
    payload = _signed_in(module).get("/developer/api/activity").get_json()
    assert payload["window"]["days"] == module.ACTIVITY_DEFAULT_DAYS


@pytest.mark.parametrize("days", ["0", "-5", "9999", "lots", "7.5"])
def test_an_unusable_window_is_a_bad_request(monkeypatch, tmp_path, days):
    module = _app(monkeypatch, tmp_path)
    response = _signed_in(module).get(f"/developer/api/activity?days={days}")
    assert response.status_code == 400
    assert str(module.ACTIVITY_MAX_DAYS) in response.get_json()["error"]


def test_reading_activity_is_not_itself_recorded_as_a_change(monkeypatch, tmp_path):
    """The page reads; opening it must not add to the audit trail it displays."""

    module = _app(monkeypatch, tmp_path)
    client = _signed_in(module)
    client.get("/developer/activity")
    client.get("/developer/api/activity")
    assert client.get("/developer/api/activity").get_json()["summary"]["changes"] == 0
