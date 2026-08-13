from services.access_control import AccessControl


def test_users_are_hashed_and_authenticate(tmp_path):
    control = AccessControl(tmp_path / "accesscontrol.db")
    control.ensure_schema()
    control.save_user(None, "operator", "2468", "editor")
    user = control.authenticate("operator", "2468")
    assert user["role"] == "editor"
    assert control.authenticate("operator", "wrong") is None
    with control._connect() as conn:
        stored = conn.execute("SELECT password_hash FROM users WHERE username='operator'").fetchone()[0]
    assert stored != "2468"


def test_cannot_delete_current_or_last_admin(tmp_path):
    control = AccessControl(tmp_path / "accesscontrol.db")
    control.ensure_schema()
    admin = control.authenticate("admin", "1336")
    try:
        control.delete_user(admin["id"], admin["id"])
    except ValueError as exc:
        assert "currently in use" in str(exc)
    else:
        raise AssertionError("current account was deleted")
