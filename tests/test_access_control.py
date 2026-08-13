from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from services.access_control import AccessControl


def test_users_are_hashed_and_authenticate(tmp_path):
    control = AccessControl(tmp_path / "accesscontrol.db")
    control.ensure_schema(initial_username="admin", initial_pin="1336")
    control.save_user(None, "operator", "2468", "editor")
    user = control.authenticate("operator", "2468")
    assert user["role"] == "editor"
    assert control.authenticate("operator", "wrong") is None
    with control._connect() as conn:
        stored = conn.execute("SELECT password_hash FROM users WHERE username='operator'").fetchone()[0]
    assert stored != "2468"


def test_existing_user_schema_gains_credential_version(tmp_path):
    import sqlite3

    path = tmp_path / "legacy-accesscontrol.db"
    with sqlite3.connect(path) as conn:
        conn.execute("""CREATE TABLE users (
            id INTEGER PRIMARY KEY, username TEXT, password_hash TEXT, role TEXT,
            created_at TEXT, updated_at TEXT)""")
    control = AccessControl(path)
    control.ensure_schema()
    with control._connect() as conn:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(users)")}
    assert "credential_version" in columns


def test_parallel_schema_bootstrap_creates_one_admin_without_errors(tmp_path):
    path = tmp_path / "accesscontrol.db"
    barrier = Barrier(5)

    def bootstrap(_index):
        barrier.wait()
        control = AccessControl(path)
        control.ensure_schema(initial_username="root", initial_pin="secret")
        return control.list_users()

    with ThreadPoolExecutor(max_workers=5) as executor:
        results = list(executor.map(bootstrap, range(5)))

    assert all(len(users) == 1 for users in results)
    users = AccessControl(path).list_users()
    assert [(user["username"], user["role"]) for user in users] == [("root", "admin")]


def test_cannot_delete_current_or_last_admin(tmp_path):
    control = AccessControl(tmp_path / "accesscontrol.db")
    control.ensure_schema(initial_username="admin", initial_pin="1336")
    admin = control.authenticate("admin", "1336")
    try:
        control.delete_user(admin["id"], admin["id"])
    except ValueError as exc:
        assert "currently in use" in str(exc)
    else:
        raise AssertionError("current account was deleted")


def test_no_published_default_account_is_created(tmp_path):
    control = AccessControl(tmp_path / "accesscontrol.db")
    control.ensure_schema()
    assert control.list_users() == []
    assert control.authenticate("admin", "1336") is None


def test_last_admin_cannot_be_demoted(tmp_path):
    control = AccessControl(tmp_path / "accesscontrol.db")
    control.ensure_schema(initial_username="root", initial_pin="secret")
    admin = control.authenticate("root", "secret")
    try:
        control.save_user(admin["id"], "root", "", "editor")
    except ValueError as exc:
        assert "last administrator" in str(exc)
    else:
        raise AssertionError("last administrator was demoted")


def test_get_user_reflects_role_changes_and_deletion(tmp_path):
    control = AccessControl(tmp_path / "accesscontrol.db")
    control.ensure_schema(initial_username="root", initial_pin="secret")
    control.save_user(None, "operator", "2468", "editor")
    operator = control.authenticate("operator", "2468")
    control.save_user(operator["id"], "operator", "", "viewer")
    assert control.get_user(operator["id"])["role"] == "viewer"
    control.delete_user(operator["id"], current_user_id=999)
    assert control.get_user(operator["id"]) is None


def test_parallel_login_failures_are_counted_atomically(tmp_path):
    control = AccessControl(tmp_path / "accesscontrol.db")
    control.ensure_schema(initial_username="root", initial_pin="secret")
    barrier = Barrier(5)

    def fail(index):
        barrier.wait()
        return control.authenticate_limited("root", "wrong", f"client-{index}")

    with ThreadPoolExecutor(max_workers=5) as executor:
        outcomes = list(executor.map(fail, range(5)))

    assert all(user is None for user, _retry_after in outcomes)
    assert any(retry_after > 0 for _user, retry_after in outcomes)
    with control._connect() as conn:
        account = conn.execute(
            "SELECT failure_count, locked_until FROM login_attempts WHERE scope_key='account:root'"
        ).fetchone()
    assert account["failure_count"] == 5
    assert account["locked_until"] > 0


def test_stale_login_attempt_scopes_are_swept(tmp_path, monkeypatch):
    control = AccessControl(tmp_path / "accesscontrol.db")
    control.ensure_schema(initial_username="root", initial_pin="secret")
    with control._connect() as conn:
        conn.execute(
            "INSERT INTO login_attempts VALUES ('client:stale', 1, 1, 0)"
        )
    monkeypatch.setattr("services.access_control.time.time", lambda: 10_000.0)
    control.authenticate_limited("unknown", "wrong", "current")
    with control._connect() as conn:
        assert conn.execute(
            "SELECT 1 FROM login_attempts WHERE scope_key='client:stale'"
        ).fetchone() is None


def test_parallel_admin_demotions_preserve_an_administrator(tmp_path):
    control = AccessControl(tmp_path / "accesscontrol.db")
    control.ensure_schema(initial_username="admin-one", initial_pin="secret-one")
    control.save_user(None, "admin-two", "secret-two", "admin")
    admins = {user["username"]: user["id"] for user in control.list_users()}
    barrier = Barrier(2)

    def demote(username):
        barrier.wait()
        try:
            control.save_user(admins[username], username, "", "editor")
            return True
        except ValueError as exc:
            assert "last administrator" in str(exc)
            return False

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(demote, admins))

    assert sorted(results) == [False, True]
    assert sum(user["role"] == "admin" for user in control.list_users()) == 1


def test_parallel_admin_deletions_preserve_an_administrator(tmp_path):
    control = AccessControl(tmp_path / "accesscontrol.db")
    control.ensure_schema(initial_username="admin-one", initial_pin="secret-one")
    control.save_user(None, "admin-two", "secret-two", "admin")
    admin_ids = [user["id"] for user in control.list_users()]
    barrier = Barrier(2)

    def delete(user_id):
        barrier.wait()
        try:
            control.delete_user(user_id, current_user_id=999)
            return True
        except ValueError as exc:
            assert "last administrator" in str(exc)
            return False

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(delete, admin_ids))

    assert sorted(results) == [False, True]
    assert sum(user["role"] == "admin" for user in control.list_users()) == 1
