from concurrent.futures import ThreadPoolExecutor
import hashlib
import os
import time
from threading import Barrier

import pytest

import sqlite3

from services.access_control import (
    DEFAULT_DEPARTMENT,
    DEFAULT_STAFF_LEVEL,
    DEPARTMENT_ALL,
    DEPARTMENTS,
    LOGIN_FAILURE_LIMIT,
    STAFF_LEVELS,
    AccessControl,
    department_label,
    department_scope,
    staff_level_label,
)


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


def test_account_credentials_obey_login_input_bounds(tmp_path):
    control = AccessControl(tmp_path / "accesscontrol.db")
    control.ensure_schema(initial_username="root", initial_pin="secret")
    for username, pin, message in (
        ("u" * 129, "pin", "Username must be 128"),
        ("user", "p" * 257, "PIN must be 256"),
    ):
        try:
            control.save_user(None, username, pin, "editor")
        except ValueError as exc:
            assert message in str(exc)
        else:
            raise AssertionError("An unusable credential was accepted")

    admin = control.authenticate("root", "secret")
    try:
        control.save_user(admin["id"], "root", "p" * 257, "admin")
    except ValueError:
        pass
    else:
        raise AssertionError("An unusable replacement PIN was accepted")
    assert control.authenticate("root", "secret") is not None


def test_existing_user_schema_gains_credential_version(tmp_path):
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
    account_scope = "account:" + hashlib.sha256(b"root").hexdigest()
    with control._connect() as conn:
        account = conn.execute(
            "SELECT failure_count, locked_until FROM login_attempts WHERE scope_key=?",
            (account_scope,),
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


def test_login_attempt_scope_keys_are_fixed_length_for_oversized_input(tmp_path):
    control = AccessControl(tmp_path / "accesscontrol.db")
    control.ensure_schema(initial_username="root", initial_pin="secret")
    control.authenticate_limited("x" * 1_000_000, "y" * 10_000, "z" * 1_000_000)
    with control._connect() as conn:
        keys = [row[0] for row in conn.execute("SELECT scope_key FROM login_attempts")]
    assert len(keys) == 2
    assert all(len(key) <= len("account:") + 64 for key in keys)


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


def test_bootstrap_refuses_credentials_the_login_path_cannot_check(tmp_path):
    """An unloggable-into administrator is worse than no administrator.

    authenticate_limited skips the users lookup entirely past these bounds, and
    has_users() would then report the table as populated, so a later start
    would not replace the account. Refuse it while a restart can still fix it.
    """
    control = AccessControl(tmp_path / "accesscontrol.db")
    with pytest.raises(ValueError, match="Username must be"):
        control.ensure_schema(initial_username="a" * 129, initial_pin="secret")
    with pytest.raises(ValueError, match="PIN must be"):
        control.ensure_schema(initial_username="admin", initial_pin="p" * 257)
    # Whitespace is truthy, so it clears the "configured together" check and
    # would otherwise be stored as an empty username nobody can type.
    with pytest.raises(ValueError, match="must not be blank"):
        control.ensure_schema(initial_username="   ", initial_pin="secret")

    # Refused loudly and completely: startup stops, and no account was left
    # behind that has_users() would count as a bootstrap already done.
    control.ensure_schema()
    assert control.list_users() == []

    # The bootstrap that stays inside the bounds still works, and can log in.
    control.ensure_schema(initial_username="a" * 128, initial_pin="p" * 256)
    assert control.authenticate_limited("a" * 128, "p" * 256, "client")[0]["role"] == "admin"


def test_the_signing_key_is_generated_once_and_kept(tmp_path):
    path = tmp_path / "accesscontrol.db"
    control = AccessControl(path)
    control.ensure_schema()
    secret = control.shared_secret_key()

    assert len(secret) >= 32
    # Stable across calls, across instances, and therefore across the workers
    # and restarts that would otherwise each invent a key of their own.
    assert control.shared_secret_key() == secret
    reopened = AccessControl(path)
    reopened.ensure_schema()
    assert reopened.shared_secret_key() == secret


def test_workers_starting_together_agree_on_one_signing_key(tmp_path):
    path = tmp_path / "accesscontrol.db"
    AccessControl(path).ensure_schema()
    barrier = Barrier(4)

    def start():
        barrier.wait()
        return AccessControl(path).shared_secret_key()

    with ThreadPoolExecutor(max_workers=4) as executor:
        keys = list(executor.map(lambda _: start(), range(4)))

    assert len(set(keys)) == 1


def test_stale_bootstrap_values_do_not_break_an_established_deployment(tmp_path):
    """GREMLIN_ADMIN_* creates the first administrator and nothing else.

    On a database that already has accounts these values are inert, so a stale
    or mistyped one left in .env must not take a working deployment offline on
    the next restart to report something that changes nothing.
    """
    path = tmp_path / "accesscontrol.db"
    control = AccessControl(path)
    control.ensure_schema(initial_username="root", initial_pin="secret")

    for username, pin in (("   ", "secret"), ("u" * 129, "secret"), ("root", "p" * 257)):
        control.ensure_schema(initial_username=username, initial_pin=pin)
    # Half-configured is equally inert once there is an administrator.
    control.ensure_schema(initial_username="root", initial_pin=None)
    control.ensure_schema(initial_username=None, initial_pin="secret")

    assert [user["username"] for user in control.list_users()] == ["root"]
    assert control.authenticate("root", "secret") is not None


def test_a_half_configured_bootstrap_is_still_refused_on_a_fresh_database(tmp_path):
    control = AccessControl(tmp_path / "accesscontrol.db")
    with pytest.raises(RuntimeError, match="configured together"):
        control.ensure_schema(initial_username="root", initial_pin=None)


def test_the_access_database_is_never_left_readable_by_others(tmp_path):
    """It holds the password hashes and the signing key; reading it is enough."""
    if os.name != "posix":
        pytest.skip("POSIX mode bits do not govern access on this platform")
    path = tmp_path / "accesscontrol.db"
    AccessControl(path).ensure_schema(initial_username="root", initial_pin="secret")
    assert path.stat().st_mode & 0o077 == 0

    # Narrowed on every start, not only on the one that creates the file. A
    # file widened after the fact -- or left behind by the refused bootstrap
    # below -- is otherwise never narrowed again.
    os.chmod(path, 0o644)
    AccessControl(path).ensure_schema()
    assert path.stat().st_mode & 0o077 == 0


def test_a_refused_bootstrap_does_not_leave_a_readable_file_behind(tmp_path):
    """sqlite creates the file on connect, before validation can refuse.

    The first start therefore leaves the file on disk however it ends, and a
    corrected restart finds it already existing -- so narrowing only on
    creation would have left it world-readable permanently, holding the
    administrator hash and the signing key.
    """
    if os.name != "posix":
        pytest.skip("POSIX mode bits do not govern access on this platform")
    path = tmp_path / "accesscontrol.db"
    with pytest.raises(ValueError):
        AccessControl(path).ensure_schema(initial_username="   ", initial_pin="secret")
    assert path.exists()
    assert path.stat().st_mode & 0o077 == 0

    control = AccessControl(path)
    control.ensure_schema(initial_username="root", initial_pin="secret")
    control.shared_secret_key()
    assert path.stat().st_mode & 0o077 == 0


def test_login_attempt_rows_are_capped(tmp_path, monkeypatch):
    """Hashing bounds each key's size; this bounds how many arrive.

    sqlite does not hand freed pages back to the filesystem, so an unbounded
    burst inside the failure window would permanently enlarge the file.
    """
    monkeypatch.setattr("services.access_control.LOGIN_ATTEMPT_ROW_CAP", 20)
    control = AccessControl(tmp_path / "accesscontrol.db")
    control.ensure_schema(initial_username="root", initial_pin="secret")

    # A fresh username every time, which is what a distributed caller submits:
    # each one is an account scope nothing will ever look up again.
    for index in range(200):
        control.authenticate_limited(f"nobody-{index}", "wrong", f"client-{index}")

    with control._connect() as conn:
        unlocked = conn.execute(
            "SELECT COUNT(*) FROM login_attempts WHERE locked_until <= ?", (time.time(),)
        ).fetchone()[0]
    # The cap is enforced before the current request's own two scopes are
    # written, so the standing bound is the cap plus those two -- bounded,
    # which is the whole point, rather than growing with the burst.
    assert unlocked <= 20 + 2


def test_capping_never_evicts_a_real_account_lockout(tmp_path, monkeypatch):
    """A flood must not be a way to clear the protection on someone's account.

    That is what makes capping locked rows safe: a lockout guarding a real
    account is identifiable, because the scope key is a digest of the username
    and the deployment has a listable number of accounts.
    """
    monkeypatch.setattr("services.access_control.LOGIN_ATTEMPT_ROW_CAP", 5)
    control = AccessControl(tmp_path / "accesscontrol.db")
    control.ensure_schema(initial_username="root", initial_pin="secret")

    for _ in range(LOGIN_FAILURE_LIMIT):
        control.authenticate_limited("root", "wrong", "attacker")
    _user, retry_after = control.authenticate_limited("root", "secret", "attacker")
    assert retry_after > 0

    # Flood well past the cap, every request locking out a fresh scope pair --
    # which is exactly the shape that would evict the lockout above if locked
    # rows were evicted indiscriminately.
    for index in range(60):
        for _ in range(LOGIN_FAILURE_LIMIT):
            control.authenticate_limited(f"nobody-{index}", "wrong", f"client-{index}")

    # The real account is still locked, and its correct PIN is still held.
    _user, retry_after = control.authenticate_limited("root", "secret", "attacker")
    assert retry_after > 0


def test_locked_scopes_are_capped_too(tmp_path, monkeypatch):
    """Five failures per fresh scope used to sail past the cap entirely."""
    monkeypatch.setattr("services.access_control.LOGIN_ATTEMPT_ROW_CAP", 20)
    control = AccessControl(tmp_path / "accesscontrol.db")
    control.ensure_schema(initial_username="root", initial_pin="secret")

    for index in range(120):
        for _ in range(LOGIN_FAILURE_LIMIT):
            control.authenticate_limited(f"nobody-{index}", "wrong", f"client-{index}")

    with control._connect() as conn:
        total = conn.execute("SELECT COUNT(*) FROM login_attempts").fetchone()[0]
        locked = conn.execute(
            "SELECT COUNT(*) FROM login_attempts WHERE locked_until > ?", (time.time(),)
        ).fetchone()[0]
    assert locked > 0, "the flood should be producing lockouts, or this proves nothing"
    # cap, plus the one real account that is held back, plus this request's two.
    assert total <= 20 + 1 + 2


# -- department and staff level ------------------------------------------------
#
# Two fields recorded against each account that gate nothing: which pages and
# sidebar entries each department and level should see has not been decided.
# What is worth pinning down until it is, is that the values survive a round
# trip, that the column refuses anything outside the catalog, and that adding
# the columns to an existing accounts file cannot narrow an account that is
# already in it.


def test_department_and_level_round_trip(tmp_path):
    control = AccessControl(tmp_path / "accesscontrol.db")
    control.ensure_schema(initial_username="root", initial_pin="secret")
    control.save_user(None, "planner", "2468", "editor", "operations_maintenance", "engineer")

    assert control.authenticate("planner", "2468")["department"] == "operations_maintenance"
    stored = next(user for user in control.list_users() if user["username"] == "planner")
    assert (stored["department"], stored["staff_level"]) == ("operations_maintenance", "engineer")

    # Editing without a new PIN keeps the hash and still moves both fields, which
    # is the case the developer page's Save button submits most often.
    control.save_user(stored["id"], "planner", "", "editor", "facilities", "leadership")
    moved = control.authenticate("planner", "2468")
    assert (moved["department"], moved["staff_level"]) == ("facilities", "leadership")
    assert moved["credential_version"] == 1, "moving somebody between departments is not a credential change"


def test_an_account_defaults_to_the_unrestricted_values(tmp_path):
    control = AccessControl(tmp_path / "accesscontrol.db")
    control.ensure_schema(initial_username="root", initial_pin="secret")
    control.save_user(None, "viewer", "1111", "viewer")
    for username in ("root", "viewer"):
        user = next(row for row in control.list_users() if row["username"] == username)
        assert (user["department"], user["staff_level"]) == (DEFAULT_DEPARTMENT, DEFAULT_STAFF_LEVEL)


@pytest.mark.parametrize(
    "department,staff_level,message",
    [
        ("engineering", "engineer", "Department must be one of"),
        ("", "engineer", "Department must be one of"),
        ("operations", "manager", "Level must be one of"),
        ("operations", "", "Level must be one of"),
    ],
)
def test_a_value_outside_the_catalog_is_refused(tmp_path, department, staff_level, message):
    """Never coerced to the default: both defaults are the *widest* values, so a
    misspelling that fell through to one would quietly hand out more access than
    was asked for once these fields do filter pages."""

    control = AccessControl(tmp_path / "accesscontrol.db")
    control.ensure_schema(initial_username="root", initial_pin="secret")
    with pytest.raises(ValueError, match=message):
        control.save_user(None, "somebody", "2468", "editor", department, staff_level)
    assert control.authenticate("somebody", "2468") is None


def test_the_column_itself_refuses_a_value_outside_the_catalog(tmp_path):
    """The catalog is in the schema, not only in save_user's argument checks."""

    control = AccessControl(tmp_path / "accesscontrol.db")
    control.ensure_schema(initial_username="root", initial_pin="secret")
    with control._connect() as conn:
        for column, value in (("department", "engineering"), ("staff_level", "manager")):
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(f"UPDATE users SET {column}=? WHERE username='root'", (value,))


def test_an_existing_accounts_file_gains_both_columns_unrestricted(tmp_path):
    """Migration must not narrow anybody: an account that predates these fields
    has no recorded department, and guessing one would take pages away from a
    real person the first time something filters on it."""

    path = tmp_path / "legacy-accesscontrol.db"
    with sqlite3.connect(path) as conn:
        conn.execute("""CREATE TABLE users (
            id INTEGER PRIMARY KEY, username TEXT, password_hash TEXT, role TEXT,
            created_at TEXT, updated_at TEXT)""")
        conn.execute("INSERT INTO users(username,password_hash,role) VALUES ('incumbent','x','admin')")
    control = AccessControl(path)
    control.ensure_schema()

    with control._connect() as conn:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(users)")}
    assert {"department", "staff_level"} <= columns
    incumbent = control.list_users()[0]
    assert (incumbent["department"], incumbent["staff_level"]) == (DEFAULT_DEPARTMENT, DEFAULT_STAFF_LEVEL)


def test_a_department_reports_the_single_departments_it_stands_for():
    """The only thing that reads the combined value, so that whatever eventually
    filters by department does not parse the string for itself."""

    assert department_scope("facilities") == ("facilities",)
    assert department_scope("operations_maintenance") == ("operations", "maintenance")
    assert set(department_scope(DEPARTMENT_ALL)) == {"facilities", "operations", "maintenance"}
    # Every stored value resolves to real departments, including the combined
    # ones -- so a department added to the catalog and forgotten here shows up.
    for department in DEPARTMENTS:
        assert set(department_scope(department)) <= {"facilities", "operations", "maintenance"}
        assert department_scope(department), department


def test_the_catalogs_carry_a_label_for_every_value():
    """The developer page's dropdowns draw these; a value with no label would be
    offered to an administrator as its raw column value."""

    for department in DEPARTMENTS:
        assert department_label(department) != department
    for level in STAFF_LEVELS:
        assert staff_level_label(level) != level
