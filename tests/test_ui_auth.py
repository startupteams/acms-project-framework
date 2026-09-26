"""ACMS-REQ-023 / plan §6: LLDAP-backed human login, role mapping, session cookies.

LDAP is exercised through a fake ``ldap3`` module so the real
``acms.ui.ldap_auth._authenticate_sync`` flow (service bind → search → memberOf →
user rebind → group mapping) is tested without a live directory.
"""
from __future__ import annotations

import sys
import types

import pytest

from acms.settings import get_settings
from acms.ui import ldap_auth
from acms.ui.roles import Role, highest_role
from acms.ui.session_auth import COOKIE_NAME, issue_token, session_configured, verify_token

ADMIN_DN = "cn=acms-admins,ou=groups,dc=miam,dc=home,dc=arpa"
WORKER_DN = "cn=acms-workers,ou=groups,dc=miam,dc=home,dc=arpa"
OBSERVER_DN = "cn=acms-observers,ou=groups,dc=miam,dc=home,dc=arpa"


def _install_fake_ldap3(monkeypatch, *, entries, rebind_ok=True, bind_raises=False):
    """Install a fake ldap3 package in sys.modules for the duration of a test."""
    fake = types.ModuleType("ldap3")
    fake.NONE = object()

    class FakeServer:
        def __init__(self, *args, **kwargs):
            pass

    class FakeEntry:
        def __init__(self, dn, groups):
            self.entry_dn = dn
            self.entry_attributes_as_dict = {"memberOf": list(groups), "member": list(groups)}

    class FakeConnection:
        def __init__(self, server, user=None, password=None, auto_bind=False, raise_exceptions=False, receive_timeout=None):
            if bind_raises:
                raise ldap_auth.Exception  # any exception class works

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def search(self, base, filt, attributes=None, size_limit=None):
            self.entries = entries

        def rebind(self, user, password):
            return rebind_ok

    fake.Server = FakeServer
    fake.Connection = FakeConnection
    utils = types.ModuleType("ldap3.utils")
    conv = types.ModuleType("ldap3.utils.conv")
    conv.escape_filter_chars = lambda s: s
    utils.conv = conv
    fake.utils = utils
    fake.Exception = RuntimeError
    monkeypatch.setitem(sys.modules, "ldap3", fake)
    monkeypatch.setitem(sys.modules, "ldap3.utils", utils)
    monkeypatch.setitem(sys.modules, "ldap3.utils.conv", conv)


@pytest.fixture
def ui_settings(monkeypatch):
    """Point the cached Settings instance at test LDAP/session config."""
    s = get_settings()
    saved = {
        "session_secret": s.session_secret,
        "session_cookie_secure": s.session_cookie_secure,
        "ldap_url": s.ldap_url,
        "ldap_user_base": s.ldap_user_base,
        "ldap_bind_dn": s.ldap_bind_dn,
        "ldap_bind_password": s.ldap_bind_password,
        "ldap_group_admin": s.ldap_group_admin,
        "ldap_group_worker": s.ldap_group_worker,
        "ldap_group_observer": s.ldap_group_observer,
    }
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


def test_highest_role_maps_configured_groups(ui_settings):
    assert highest_role([ADMIN_DN]) is Role.ADMINISTRATOR
    assert highest_role([WORKER_DN]) is Role.WORKER
    assert highest_role([OBSERVER_DN]) is Role.OBSERVER
    assert highest_role([]) is None
    assert highest_role(["cn=unrelated,ou=groups,dc=example,dc=org"]) is None
    # Multi-group membership: highest-privilege wins deterministically.
    assert highest_role([OBSERVER_DN, WORKER_DN, ADMIN_DN]) is Role.ADMINISTRATOR
    # Case/whitespace insensitive against directory output.
    assert highest_role([f"  {ADMIN_DN.upper()}  "]) is Role.ADMINISTRATOR


def test_session_token_roundtrip_and_failures(ui_settings):
    token = issue_token("jordan", Role.ADMINISTRATOR.value)
    user = verify_token(token)
    assert user is not None
    assert user.username == "jordan"
    assert user.role == Role.ADMINISTRATOR.value

    assert verify_token(token[:-2] + "xx") is None  # tampered signature
    assert verify_token("garbage") is None
    assert verify_token(None) is None
    expired = issue_token("jordan", Role.WORKER.value, lifetime_minutes=-1)
    assert verify_token(expired) is None


def test_session_fail_closed_without_secret(monkeypatch, ui_settings):
    ui_settings.session_secret = ""
    assert session_configured() is False
    with pytest.raises(RuntimeError):
        issue_token("jordan", Role.WORKER.value)
    assert verify_token(issue_token.__name__) is None  # verify path also closed


@pytest.mark.asyncio
async def test_ldap_authenticate_maps_roles(monkeypatch, ui_settings):
    def _entry(dn, groups):
        e = types.SimpleNamespace()
        e.entry_dn = dn
        e.entry_attributes_as_dict = {"memberOf": list(groups), "member": list(groups)}
        return e

    _install_fake_ldap3(
        monkeypatch,
        entries=[_entry("uid=jordan,ou=people,dc=miam,dc=home,dc=arpa", [ADMIN_DN])],
        rebind_ok=True,
    )
    result = await ldap_auth.authenticate("jordan", "correct-password")
    assert result.role is Role.ADMINISTRATOR

    _install_fake_ldap3(monkeypatch, entries=[], rebind_ok=True)
    result = await ldap_auth.authenticate("nobody", "pw")
    assert result.role is None  # unknown user denied generically

    _install_fake_ldap3(
        monkeypatch,
        entries=[_entry("uid=jordan,ou=people,dc=miam,dc=home,dc=arpa", [ADMIN_DN])],
        rebind_ok=False,
    )
    result = await ldap_auth.authenticate("jordan", "wrong-password")
    assert result.role is None  # wrong password denied generically

    _install_fake_ldap3(
        monkeypatch,
        entries=[_entry("uid=worker,ou=people,dc=miam,dc=home,dc=arpa", [])],
        rebind_ok=True,
    )
    result = await ldap_auth.authenticate("worker", "pw")
    assert result.role is None  # authenticated but unmapped → denied (plan §6)


@pytest.mark.asyncio
async def test_ldap_unavailable_on_bind_failure(monkeypatch, ui_settings):
    _install_fake_ldap3(monkeypatch, entries=[], bind_raises=True)
    with pytest.raises(ldap_auth.LdapUnavailable):
        await ldap_auth.authenticate("jordan", "pw")


@pytest.mark.asyncio
async def test_ldap_unavailable_when_unconfigured(monkeypatch, ui_settings):
    ui_settings.ldap_url = ""
    with pytest.raises(ldap_auth.LdapUnavailable):
        await ldap_auth.authenticate("jordan", "pw")


@pytest.mark.asyncio
async def test_ldap_rejects_empty_credentials():
    result = await ldap_auth.authenticate("", "")
    assert result.role is None
