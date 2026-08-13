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
