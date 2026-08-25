"""The help contacts: the footer's Help dialog and the login dialog's help button.

Both are drawn from the same SUPPORT_CONTACTS list through one partial, so what
is worth pinning down is that the list reaches both places -- a missing context
variable renders as an empty loop rather than raising, which is exactly the kind
of breakage that ships unnoticed.
"""

import importlib

import pytest


def _app(monkeypatch, tmp_path):
    monkeypatch.setenv("GREMLIN_ACCESS_DB_PATH", str(tmp_path / "accesscontrol.db"))
    monkeypatch.setenv("GREMLIN_DB_PATH", str(tmp_path / "gremlin.db"))
    monkeypatch.setenv("GREMLIN_ADMIN_USERNAME", "root")
    monkeypatch.setenv("GREMLIN_ADMIN_PIN", "secret")
    import app
    return importlib.reload(app)


def _login(module):
    client = module.app.test_client()
    response = client.post("/auth/login", json={"username": "root", "pin": "secret"})
    assert response.status_code == 200, response.get_data(as_text=True)
    return client


@pytest.mark.parametrize("page", ["/", "/about", "/reliability-links"])
def test_the_footer_offers_every_contact_on_every_page(monkeypatch, tmp_path, page):
    """The names are not printed in the footer itself -- they are a click behind it."""
    module = _app(monkeypatch, tmp_path)
    body = module.app.test_client().get(page).get_data(as_text=True)
    assert 'id="footerHelpButton"' in body
    assert 'id="footerHelpDialog"' in body
    # The button has to name the dialog it opens, or the click handler in
    # layout.js is wiring up two halves of nothing.
    assert 'aria-controls="footerHelpDialog"' in body
    for contact in module.SUPPORT_CONTACTS:
        assert contact["name"] in body, contact["name"]
        assert f'mailto:{contact["email"]}' in body, contact["email"]


def test_the_footer_keeps_the_contacts_out_of_the_footer_itself(monkeypatch, tmp_path):
    """The block of prose the button replaced does not come back alongside it."""
    body = _app(monkeypatch, tmp_path).app.test_client().get("/about").get_data(as_text=True)
    footer = body.split('<footer', 1)[1].split('</footer>', 1)[0]
    assert "please contact" not in footer
    assert "support-contacts" not in footer


def test_the_footer_help_button_survives_signing_in(monkeypatch, tmp_path):
    """It answers questions, not just access requests, so it is not a signed-out thing."""
    module = _app(monkeypatch, tmp_path)
    body = _login(module).get("/about").get_data(as_text=True)
    assert 'id="footerHelpButton"' in body
    assert 'id="footerHelpDialog"' in body
    for contact in module.SUPPORT_CONTACTS:
        assert f'mailto:{contact["email"]}' in body, contact["email"]


def test_the_login_dialog_offers_the_contacts_to_a_visitor(monkeypatch, tmp_path):
    module = _app(monkeypatch, tmp_path)
    body = module.app.test_client().get("/").get_data(as_text=True)
    assert 'id="accountHelpButton"' in body
    assert 'id="supportDialog"' in body
    # The button has to name the dialog it opens, or the click handler in
    # layout.js is wiring up two halves of nothing.
    assert 'aria-controls="supportDialog"' in body


def test_the_login_help_button_is_gone_once_signed_in(monkeypatch, tmp_path):
    """There is no login form to be stuck at, so neither the button nor its dialog is drawn."""
    module = _app(monkeypatch, tmp_path)
    body = _login(module).get("/").get_data(as_text=True)
    assert 'id="accountHelpButton"' not in body
    assert 'id="supportDialog"' not in body


def test_every_contact_is_a_name_and_a_company_address(monkeypatch, tmp_path):
    """A contact missing either half is a contact nobody can reach."""
    for contact in _app(monkeypatch, tmp_path).SUPPORT_CONTACTS:
        assert contact["name"].strip()
        assert contact["email"].strip().endswith("@sandc.com"), contact
