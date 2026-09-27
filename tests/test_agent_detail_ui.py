"""Feature slice 2 — Agent Detail + Background Routines (plan §14; ACMS-REQ-005/008/014/023).

Covers: login gating, 404, full history rendering (assignments, execution
tasks, handoffs, routine inventory), capability manifest honesty (declared vs
not declared), recorded-only assignment distinction, heartbeat absence, and
read access for every mapped role (page is read-only for all roles).
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from acms.main import app
from acms.work_models import WorkItemCreate
from tests.test_ui_auth import ADMIN_DN, OBSERVER_DN, WORKER_DN, _install_fake_ldap3

client = TestClient(app, follow_redirects=False)
AUTH = {"Authorization": "Bearer test-token"}

AGENT = {
    "external_registration_id": "miam-00111-hermes-02",
    "display_name": "Hermes Worker 02",
    "trust_class": "internal",
    "harness": "hermes",
    "bridge_version": "0.1",
    "protocol_version": "a2a",
    "capabilities": {"streaming": True, "steering": True, "background_routine_inventory": True},
}


@pytest.fixture(autouse=True)
def ui_env(monkeypatch):
    from acms.settings import get_settings

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


def _login_admin(monkeypatch):
    import types

    e = types.SimpleNamespace()
    e.entry_dn = "uid=jordan,ou=people,dc=miam,dc=home,dc=arpa"
    e.entry_attributes_as_dict = {"memberOf": [ADMIN_DN], "member": [ADMIN_DN]}
    _install_fake_ldap3(monkeypatch, entries=[e], rebind_ok=True)
    resp = client.post("/ui/login", data={"username": "jordan", "password": "pw"})
    assert resp.status_code == 303
    return {"acms_session": resp.cookies["acms_session"]}


def _login_other(monkeypatch, dn: str, username: str):
    import types

    e = types.SimpleNamespace()
    e.entry_dn = f"uid={username},ou=people,dc=miam,dc=home,dc=arpa"
    e.entry_attributes_as_dict = {"memberOf": [dn], "member": [dn]}
    _install_fake_ldap3(monkeypatch, entries=[e], rebind_ok=True)
    resp = client.post("/ui/login", data={"username": username, "password": "pw"})
    assert resp.status_code == 303
    return {"acms_session": resp.cookies["acms_session"]}


def _register_agent(**overrides):
    r = client.post("/api/v1/agents/register", headers=AUTH, json={**AGENT, **overrides})
    assert r.status_code in (200, 201)
    return r.json()["agent_id"]


def _make_item(title: str, kind: str = "task") -> str:
    r = client.post("/api/v1/work/items", headers=AUTH, json={"kind": kind, "title": title})
    assert r.status_code == 201, r.text
    return r.json()["work_item_id"]


# ---------------------------------------------------------------- gating + 404


def test_agent_detail_requires_login():
    r = client.get("/ui/agents/00000000-0000-0000-0000-000000000000", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/ui/login"


def test_agent_detail_404(monkeypatch, ui_env):
    cookies = _login_admin(monkeypatch)
    r = client.get("/ui/agents/00000000-0000-0000-0000-000000000000", cookies=cookies)
    assert r.status_code == 404


# ---------------------------------------------------------------- full render


def test_agent_detail_renders_full_history(monkeypatch, ui_env):
    agent_id = _register_agent()
    item_id = _make_item("Slice 2: Agent Detail")
    cookies = _login_admin(monkeypatch)

    # Assignment (recorded only — no A2A yet).
    r = client.post(
        f"/ui/work/{item_id}/assign", cookies=cookies,
        data={"agent_id": agent_id}, follow_redirects=False,
    )
    assert r.status_code == 303, r.headers.get("location", "")

    # Background routine inventory (agent-reported via machine API).
    rr = client.put(
        f"/api/v1/work/agents/{agent_id}/routines", headers=AUTH,
        json={"name": "nightly-backup", "purpose": "verify backups",
              "schedule": "daily 03:05", "enabled": True, "latest_status": "ok"},
    )
    assert rr.status_code in (200, 201), rr.text

    # Execution task linked to the work item.
    tr = client.post("/api/v1/work/tasks", headers=AUTH,
                     json={"work_item_id": item_id, "agent_id": agent_id})
    assert tr.status_code == 201, tr.text

    # Handoff on the assigned work item.
    hr = client.post(
        f"/ui/work/{item_id}/handoffs", cookies=cookies,
        data={"title": "Kickoff", "body_markdown": "# Handoff\n\nObserved: kickoff complete."},
        follow_redirects=False,
    )
    assert hr.status_code == 303

    page = client.get(f"/ui/agents/{agent_id}", cookies=cookies)
    assert page.status_code == 200, page.text

    # Identity block.
    assert "Hermes Worker 02" in page.text
    assert "hermes" in page.text
    assert "a2a" in page.text
    assert agent_id in page.text

    # Capability manifest: declared AND not-declared flags both shown honestly.
    assert "streaming — declared" in page.text
    assert "pause — not declared" in page.text
    assert "background routine inventory — declared" in page.text

    # Primary assignment + honesty notices.
    assert "ACTIVE" in page.text
    assert "Slice 2: Agent Detail" in page.text
    assert "Recorded assignment only" in page.text
    # Slice 3+4 landed: liveness now exists (UNKNOWN until first contact), so the
    # old "not yet implemented" heartbeat note must be GONE (plan §5 honesty).
    assert "not yet implemented" not in page.text
    assert "UNKNOWN" in page.text  # never-contacted state, not fabricated

    # History tables.
    assert "Assignment history" in page.text
    assert "Execution-task history" in page.text
    assert "RUNNING" in page.text
    assert "Handoffs" in page.text
    assert "Observed: kickoff complete." in page.text

    # Routine inventory + no-scheduling honesty note.
    assert "nightly-backup" in page.text
    assert "daily 03:05" in page.text
    assert "does not schedule" in page.text


def test_agent_detail_no_active_assignment(monkeypatch, ui_env):
    agent_id = _register_agent(external_registration_id="miam-00111-hermes-idle")
    cookies = _login_admin(monkeypatch)
    page = client.get(f"/ui/agents/{agent_id}", cookies=cookies)
    assert page.status_code == 200
    assert "No active primary assignment" in page.text
    assert "No assignments recorded" in page.text


# ---------------------------------------------------------------- role access


def test_agent_detail_read_for_all_roles(monkeypatch, ui_env):
    agent_id = _register_agent(external_registration_id="miam-00111-hermes-03")
    worker = _login_other(monkeypatch, WORKER_DN, "worker1")
    observer = _login_other(monkeypatch, OBSERVER_DN, "observer1")
    assert client.get(f"/ui/agents/{agent_id}", cookies=worker).status_code == 200
    assert client.get(f"/ui/agents/{agent_id}", cookies=observer).status_code == 200


# ---------------------------------------------------------------- cross-links


def test_agents_list_links_to_detail(monkeypatch, ui_env):
    agent_id = _register_agent(external_registration_id="miam-00111-hermes-04")
    cookies = _login_admin(monkeypatch)
    listing = client.get("/ui/agents", cookies=cookies)
    assert listing.status_code == 200
    assert f'href="/ui/agents/{agent_id}"' in listing.text


def test_work_detail_links_assigned_agent(monkeypatch, ui_env):
    agent_id = _register_agent(external_registration_id="miam-00111-hermes-05")
    item_id = _make_item("Cross-link check")
    cookies = _login_admin(monkeypatch)
    client.post(
        f"/ui/work/{item_id}/assign", cookies=cookies,
        data={"agent_id": agent_id}, follow_redirects=False,
    )
    detail = client.get(f"/ui/work/{item_id}", cookies=cookies)
    assert detail.status_code == 200
    assert f'href="/ui/agents/{agent_id}"' in detail.text
