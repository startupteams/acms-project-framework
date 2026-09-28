"""Runtime lifecycle UI (Phase 3) — agent detail runtime panel + admin controls."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from acms.main import app
from tests.test_ui_auth import ADMIN_DN, OBSERVER_DN, _install_fake_ldap3

client = TestClient(app, follow_redirects=False)
AUTH = {"Authorization": "Bearer test-token"}

AGENT = {
    "external_registration_id": "miam-00111-hermes-rt-ui",
    "display_name": "Runtime UI Test Agent",
    "trust_class": "internal",
    "harness": "hermes",
    "bridge_version": "0.1",
    "protocol_version": "a2a",
    "capabilities": {},
}


@pytest.fixture(autouse=True)
def ui_env(monkeypatch):
    from acms.settings import get_settings

    s = get_settings()
    saved = {k: getattr(s, k) for k in (
        "session_secret", "session_cookie_secure",
        "ldap_url", "ldap_user_base", "ldap_bind_dn", "ldap_bind_password",
        "ldap_group_admin", "ldap_group_worker", "ldap_group_observer",
        "server_manager_base_url", "server_manager_token",
    )}
    s.session_secret = "ui-test-secret-0123456789abcdef"
    s.session_cookie_secure = True
    s.ldap_url = "ldaps://lldap.miam.home.arpa:636"
    s.ldap_user_base = "ou=people,dc=miam,dc=home,dc=arpa"
    s.ldap_bind_dn = "uid=acms,ou=people,dc=miam,dc=home,dc=arpa"
    s.ldap_bind_password = "bind-secret"
    s.ldap_group_admin = ADMIN_DN
    s.ldap_group_worker = ""
    s.ldap_group_observer = OBSERVER_DN
    s.server_manager_base_url = "http://sm.test"
    s.server_manager_token = "tok"
    yield s
    for key, value in saved.items():
        setattr(s, key, value)


class FakeSMHandle:
    def __init__(self, raw):
        self.runtime_id = raw.get("runtime_id", "rt-1")
        self.acms_agent_id = raw.get("acms_agent_id", "a")
        self.actual_state = raw.get("actual_state", "RUNNING")
        self.vmid = raw.get("vmid", 124)
        self.node = raw.get("node", "miam00111")
        self.hermes_state_branch = raw.get("hermes_state_branch", "agent/22ac1b19")
        self.raw = raw


@pytest.fixture()
def sm_fake(monkeypatch):
    from acms.server_manager_client import ServerManagerClient

    calls = []

    def fake_list(self, acms_agent_id=None):
        calls.append(("list", acms_agent_id))
        return [FakeSMHandle({"runtime_id": "rt-1", "desired_state": "DESIRED_RUNNING",
                              "actual_state": "RUNNING", "recovery_count": 1,
                              "last_error": None, "last_reconcile_at": "2026-09-28T01:00:00+00:00",
                              "node": "miam00111", "vmid": 124, "state_sync_health": "VERIFIED",
                              "hermes_state_branch": "agent/22ac1b19",
                              "last_state_commit_sha": "180dd563c746fa88ebb55844ff25a9fe1f9d8c26",
                              "acms_agent_id": acms_agent_id})]

    def fake_desired(self, runtime_id, desired_state, reason):
        calls.append(("desired", runtime_id, desired_state, reason))
        actual = "RUNNING" if desired_state == "DESIRED_RUNNING" else "STOPPED"
        return FakeSMHandle({"runtime_id": runtime_id, "desired_state": desired_state,
                             "actual_state": actual, "acms_agent_id": "a"})

    def fake_reconcile(self, runtime_id):
        calls.append(("reconcile", runtime_id))
        return {"runtime_id": runtime_id, "desired": "DESIRED_RUNNING", "before": "STOPPED",
                "after": "RUNNING", "action": "noop", "error": None, "detail": {}}

    monkeypatch.setattr(ServerManagerClient, "list_runtimes", fake_list)
    monkeypatch.setattr(ServerManagerClient, "set_desired_state", fake_desired)
    monkeypatch.setattr(ServerManagerClient, "reconcile", fake_reconcile)
    return calls


def _login(monkeypatch, dn: str, username: str):
    import types

    e = types.SimpleNamespace()
    e.entry_dn = f"uid={username},ou=people,dc=miam,dc=home,dc=arpa"
    e.entry_attributes_as_dict = {"memberOf": [dn], "member": [dn]}
    _install_fake_ldap3(monkeypatch, entries=[e], rebind_ok=True)
    resp = client.post("/ui/login", data={"username": username, "password": "pw"})
    assert resp.status_code == 303
    assert resp.headers.get("location") == "/ui/"
    return {"acms_session": resp.cookies["acms_session"]}


def _register_agent(**overrides):
    r = client.post("/api/v1/agents/register", headers=AUTH, json={**AGENT, **overrides})
    assert r.status_code in (200, 201)
    return r.json()["agent_id"]


def test_agent_detail_shows_runtime_panel(monkeypatch, ui_env, sm_fake):
    agent_id = _register_agent()
    hdr = _login(monkeypatch, ADMIN_DN, "jordan")
    r = client.get(f"/ui/agents/{agent_id}", cookies=hdr, follow_redirects=False)
    assert r.status_code == 200, f"{r.status_code} loc={r.headers.get('location')} {r.text[:200]}"
    body = r.text
    assert "Runtime lifecycle" in body
    assert "DESIRED_RUNNING" in body
    assert "agent/22ac1b19" in body
    assert "180dd563" in body  # sha short form
    assert "Reconcile now" in body


def test_runtime_action_requires_admin(monkeypatch, ui_env, sm_fake):
    agent_id = _register_agent()
    hdr = _login(monkeypatch, OBSERVER_DN, "observer1")
    r = client.post(f"/ui/agents/{agent_id}/runtime/start", cookies=hdr,
                    follow_redirects=False)
    assert r.status_code == 403


def test_runtime_stop_via_ui(monkeypatch, ui_env, sm_fake):
    agent_id = _register_agent()
    hdr = _login(monkeypatch, ADMIN_DN, "jordan")
    r = client.post(f"/ui/agents/{agent_id}/runtime/stop", cookies=hdr,
                    follow_redirects=False)
    assert r.status_code == 303, r.text
    assert r.headers.get("location") == f"/ui/agents/{agent_id}"
    kinds = [c[0] for c in sm_fake]
    assert "desired" in kinds and "reconcile" in kinds
    stops = [c for c in sm_fake if c[0] == "desired" and c[2] == "DESIRED_STOPPED"]
    assert stops
