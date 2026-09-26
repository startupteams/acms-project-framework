"""Plan §6/§14: web UI page behavior — redirects, role gating, cookie flags,
real-data-only pages, and bearer-token isolation.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from acms.main import app
from acms.settings import get_settings
from acms.ui.session_auth import COOKIE_NAME, issue_token
from tests.test_ui_auth import ADMIN_DN, OBSERVER_DN, WORKER_DN

client = TestClient(app, follow_redirects=False)
AUTH = {"Authorization": "Bearer test-token"}


@pytest.fixture(autouse=True)
def ui_env(monkeypatch):
    s = get_settings()
    saved = {k: getattr(s, k) for k in (
        "session_secret", "session_cookie_secure",
        "ldap_url", "ldap_user_base", "ldap_bind_dn", "ldap_bind_password",
        "ldap_group_admin", "ldap_group_worker", "ldap_group_observer",
    )}
    s.session_secret = "ui-test-secret-0123456789abcdef"
    s.session_cookie_secure = True
    s.ldap_url = "ldaps://lldap.miam.home.arpa:636"
    s.ldap_user_base = "ou=people,dc=miam,dc=home,dc=arpa"
    s.ldap_bind_dn = "uid=acms,ou=people,dc=miam,dc=home,dc=arpa"
    s.ldap_bind_password = "bind-secret"
    s.ldap_group_admin = ADMIN_DN
    s.ldap_group_worker = WORKER_DN
    s.ldap_group_observer = OBSERVER_DN
    yield s
    for key, value in saved.items():
        setattr(s, key, value)


def _login(monkeypatch, role_dn: str, username: str = "jordan"):
    from tests.test_ui_auth import _install_fake_ldap3

    def _entry(dn, groups):
        import types

        e = types.SimpleNamespace()
        e.entry_dn = f"uid={username},ou=people,dc=miam,dc=home,dc=arpa"
        e.attributes = types.SimpleNamespace(
            get=lambda key, default=None: types.SimpleNamespace(values=list(groups))
        )
        return e

    _install_fake_ldap3(monkeypatch, entries=[_entry(username, [role_dn])], rebind_ok=True)
    resp = client.post(
        "/ui/login",
        data={"username": username, "password": "pw"},
        headers={"Host": "acms.miam.home.arpa"},
    )
    assert resp.status_code == 303, resp.text
    assert resp.headers["location"] == "/ui/"
    assert COOKIE_NAME in resp.cookies
    # Plain dict so httpx does not apply the Secure attribute against http://testserver.
    return {COOKIE_NAME: resp.cookies[COOKIE_NAME]}


# ---------------------------------------------------------------- login/logout


def test_login_sets_http_only_secure_lax_cookie(monkeypatch, ui_env):
    from tests.test_ui_auth import _install_fake_ldap3

    import types as _t

    e = _t.SimpleNamespace()
    e.entry_dn = "uid=jordan,ou=people,dc=miam,dc=home,dc=arpa"
    e.attributes = _t.SimpleNamespace(
        get=lambda key, default=None: _t.SimpleNamespace(values=[ADMIN_DN])
    )
    _install_fake_ldap3(monkeypatch, entries=[e], rebind_ok=True)
    resp = client.post("/ui/login", data={"username": "jordan", "password": "pw"})
    assert resp.status_code == 303
    raw = resp.headers["set-cookie"]
    assert "HttpOnly" in raw
    assert "Secure" in raw
    assert "SameSite=lax" in raw


def test_login_page_public_and_logout_clears(monkeypatch, ui_env):
    assert client.get("/ui/login").status_code == 200
    resp = _login(monkeypatch, WORKER_DN, username="worker1")
    logout = client.get("/ui/logout", cookies=resp, follow_redirects=False)
    assert logout.status_code == 303 and logout.headers["location"] == "/ui/login"
    # Cookie cleared via Max-Age=0 / expired attribute.
    assert "max-age=0" in logout.headers["set-cookie"].lower() or "expires=Thu, 01 Jan 1970" in logout.headers["set-cookie"].lower()


def test_bad_or_unmapped_credentials_fail_closed(monkeypatch, ui_env):
    from tests.test_ui_auth import _install_fake_ldap3

    def _entry(groups):
        import types

        e = types.SimpleNamespace()
        e.entry_dn = "uid=x,ou=people,dc=miam,dc=home,dc=arpa"
        e.attributes = types.SimpleNamespace(
            get=lambda key, default=None: types.SimpleNamespace(values=list(groups))
        )
        return e

    # Wrong password (rebind fails) → generic 403, no cookie, no enumeration.
    _install_fake_ldap3(monkeypatch, entries=[_entry([ADMIN_DN])], rebind_ok=False)
    bad_pw = client.post("/ui/login", data={"username": "jordan", "password": "nope"})
    assert bad_pw.status_code == 403
    assert not bad_pw.cookies.get(COOKIE_NAME)

    # Authenticated but no mapped group → 403 denied.
    _install_fake_ldap3(monkeypatch, entries=[_entry([])], rebind_ok=True)
    unmapped = client.post("/ui/login", data={"username": "visitor", "password": "pw"})
    assert unmapped.status_code == 403
    assert not unmapped.cookies.get(COOKIE_NAME)


def test_login_unavailable_when_ldap_down(monkeypatch, ui_env):
    from tests.test_ui_auth import _install_fake_ldap3

    _install_fake_ldap3(monkeypatch, entries=[], bind_raises=True)
    resp = client.post("/ui/login", data={"username": "jordan", "password": "pw"})
    assert resp.status_code == 503


def test_login_disabled_without_session_secret(ui_env):
    ui_env.session_secret = ""
    page = client.get("/ui/login")
    assert page.status_code == 503
    assert "disabled" in page.text.lower()
    post = client.post("/ui/login", data={"username": "u", "password": "p"})
    assert post.status_code == 503


# ---------------------------------------------------------------- page gating


@pytest.mark.parametrize("role_dn,expected_status", [
    (ADMIN_DN, 200),
    (WORKER_DN, 200),
    (OBSERVER_DN, 200),
])
def test_mapped_roles_can_read_all_current_pages(monkeypatch, ui_env, role_dn, expected_status):
    resp = _login(monkeypatch, role_dn)
    for path in ("/ui/", "/ui/agents", "/ui/system"):
        page = client.get(path, cookies=resp)
        assert page.status_code == expected_status, (path, page.text)


def test_unauthenticated_requests_redirect_to_login():
    for path in ("/ui/", "/ui/agents", "/ui/system"):
        resp = client.get(path, follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"] == "/ui/login"


def test_invalid_session_cookie_redirects():
    resp = client.get("/ui/", cookies={COOKIE_NAME: "forged.token"}, follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/ui/login"


# ---------------------------------------------------------------- page content


def test_home_and_system_pages_show_real_data_only(monkeypatch, ui_env):
    resp = _login(monkeypatch, ADMIN_DN)
    home = client.get("/ui/", cookies=resp)
    assert home.status_code == 200
    assert "Not yet implemented" in home.text
    assert "primary assignment" in home.text

    system = client.get("/ui/system", cookies=resp)
    assert system.status_code == 200
    assert "development" in system.text  # ACMS_ENVIRONMENT surface


def test_agents_page_lists_registered_rows(monkeypatch, ui_env):
    reg = client.post(
        "/api/v1/agents/register",
        headers=AUTH,
        json={
            "external_registration_id": "ui-test-agent-01",
            "display_name": "UI Test Agent",
            "trust_class": "internal",
            "harness": "hermes",
            "bridge_version": "0.1",
            "protocol_version": "a2a",
            "capabilities": {"streaming": True, "steering": True},
        },
    )
    assert reg.status_code == 201
    resp = _login(monkeypatch, OBSERVER_DN)
    page = client.get("/ui/agents", cookies=resp)
    assert page.status_code == 200
    assert "UI Test Agent" in page.text
    assert "ui-test-agent-01" not in page.text  # external_registration_id is not a UI field


def test_bearer_token_never_exposed_in_ui(monkeypatch, ui_env):
    """Machine bearer token must never appear in any UI response (plan §6)."""
    resp = _login(monkeypatch, ADMIN_DN)
    for path in ("/ui/", "/ui/agents", "/ui/system", "/ui/login"):
        body = client.get(path, cookies=resp).text
        assert "test-token" not in body
        assert "Bearer" not in body


def test_ui_static_assets_served():
    css = client.get("/ui/static/style.css")
    assert css.status_code == 200
    assert "text/css" in css.headers["content-type"]
