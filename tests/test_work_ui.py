"""Feature slice 1 — Work Management UI (plan §13; ACMS-REQ-007/008/013/014/036).

Covers: role gating (admin mutate / worker+observer read), list + filters,
create/edit, hierarchy, assignment invariant surfacing, recorded-vs-delivered
distinction, and 404 handling.
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
    "external_registration_id": "miam-00111-hermes-01",
    "display_name": "Hermes Worker 01",
    "trust_class": "internal",
    "harness": "hermes",
    "bridge_version": "0.1",
    "protocol_version": "a2a",
    "capabilities": {"streaming": True, "steering": True},
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


def _register_agent():
    r = client.post("/api/v1/agents/register", headers=AUTH, json=AGENT)
    assert r.status_code in (200, 201)
    return r.json()["agent_id"]


def _make_item(**kwargs):
    import asyncio

    payload = WorkItemCreate(title=kwargs.get("title", "Test task"), kind="task")
    from acms.db import get_session  # not used directly; service needs a session

    # Use the API instead — cleaner inside TestClient context.
    r = client.post("/api/v1/work/items", headers=AUTH, json={
        "kind": kwargs.get("kind", "task"),
        "title": kwargs.get("title", "Test task"),
        "scope_markdown": kwargs.get("scope", ""),
        "parent_id": kwargs.get("parent_id"),
    })
    assert r.status_code == 201, r.text
    return r.json()["work_item_id"]


# ---------------------------------------------------------------- list + gating


def test_work_nav_and_list_require_login():
    r = client.get("/ui/work", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/ui/login"


def test_work_list_renders_items(monkeypatch, ui_env):
    _register_agent()
    _make_item(title="Slice 1: Work UI")
    cookies = _login_admin(monkeypatch)
    page = client.get("/ui/work", cookies=cookies)
    assert page.status_code == 200
    assert "Slice 1: Work UI" in page.text
    assert "Work items" in page.text
    # Nav contains Work link on all pages.
    assert 'href="/ui/work"' in page.text


def test_work_filters(monkeypatch, ui_env):
    _make_item(title="Planned one")  # default planned
    cookies = _login_admin(monkeypatch)
    page = client.get("/ui/work", cookies=cookies, params={"status_filter": "planned"})
    assert page.status_code == 200
    assert "Planned one" in page.text
    page = client.get("/ui/work", cookies=cookies, params={"status_filter": "completed"})
    assert page.status_code == 200
    assert "Planned one" not in page.text


def test_create_and_edit_via_ui(monkeypatch, ui_env):
    cookies = _login_admin(monkeypatch)
    # Create through the form endpoint.
    r = client.post(
        "/ui/work/new",
        cookies=cookies,
        data={"kind": "project", "parent_id": "", "title": "UI Created Project", "scope_markdown": "# Scope"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    detail_url = r.headers["location"]
    detail = client.get(detail_url, cookies=cookies)
    assert detail.status_code == 200
    assert "UI Created Project" in detail.text
    assert "# Scope" in detail.text
    item_id = detail_url.rsplit("/", 1)[1]

    # Edit title + status.
    r2 = client.post(
        f"/ui/work/{item_id}/edit",
        cookies=cookies,
        data={"title": "UI Created Project v2", "status_value": "active", "scope_markdown": "# Scope"},
        follow_redirects=False,
    )
    assert r2.status_code == 303
    detail2 = client.get(detail_url, cookies=cookies)
    assert "UI Created Project v2" in detail2.text


def test_worker_observer_cannot_mutate(monkeypatch, ui_env):
    worker = _login_other(monkeypatch, WORKER_DN, "worker1")
    r = client.post(
        "/ui/work/new",
        cookies=worker,
        data={"kind": "task", "parent_id": "", "title": "nope", "scope_markdown": ""},
        follow_redirects=False,
    )
    assert r.status_code == 403

    observer = _login_other(monkeypatch, OBSERVER_DN, "observer1")
    ro = client.post(
        "/ui/work/new",
        cookies=observer,
        data={"kind": "task", "parent_id": "", "title": "nope", "scope_markdown": ""},
        follow_redirects=False,
    )
    assert ro.status_code == 403

    # But reads are allowed.
    assert client.get("/ui/work", cookies=worker).status_code == 200
    assert client.get("/ui/work", cookies=observer).status_code == 200


# ---------------------------------------------------------------- assignments


def test_assign_and_close_via_ui(monkeypatch, ui_env):
    agent_id = _register_agent()
    item_id = _make_item(title="Assign me")
    cookies = _login_admin(monkeypatch)

    # Assign through the UI form.
    r = client.post(
        f"/ui/work/{item_id}/assign",
        cookies=cookies,
        data={"agent_id": agent_id},
        follow_redirects=False,
    )
    assert r.status_code == 303, r.headers.get("location", "")
    detail = client.get(f"/ui/work/{item_id}", cookies=cookies)
    assert detail.status_code == 200
    assert "Recorded assignment only" in detail.text  # delivery distinction (plan §13)
    assert "Hermes Worker 01" in detail.text  # agent shown by display name

    # Second assignment for the same item must surface the invariant error.
    r2 = client.post(
        f"/ui/work/{item_id}/assign",
        cookies=cookies,
        data={"agent_id": agent_id},
        follow_redirects=False,
    )
    assert r2.status_code == 303
    assert "agent-has-active-assignment" in r2.headers["location"]

    # Close via UI.
    assignments = client.get("/api/v1/work/assignments", headers=AUTH).json()
    active = [a for a in assignments if a["work_item_id"] == item_id and a["status"] == "ACTIVE"]
    assert active, "expected one ACTIVE assignment"
    r3 = client.post(
        f"/ui/work/assignments/{active[0]['assignment_id']}/close",
        cookies=cookies,
        data={"new_status": "COMPLETED", "work_item_id": item_id},
        follow_redirects=False,
    )
    assert r3.status_code == 303
    detail2 = client.get(f"/ui/work/{item_id}", cookies=cookies)
    assert "COMPLETED" in detail2.text


def test_assign_unknown_agent_redirects_with_error(monkeypatch, ui_env):
    item_id = _make_item(title="Bad assign")
    cookies = _login_admin(monkeypatch)
    r = client.post(
        f"/ui/work/{item_id}/assign",
        cookies=cookies,
        data={"agent_id": "00000000-0000-0000-0000-000000000000"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "assignment+refused" in r.headers["location"] or "assignment%20refused" in r.headers["location"]


def test_work_detail_404(monkeypatch, ui_env):
    cookies = _login_admin(monkeypatch)
    r = client.get("/ui/work/nonexistent-id", cookies=cookies, follow_redirects=False)
    assert r.status_code == 404


def test_work_new_form_renders(monkeypatch, ui_env):
    """Window-5 §5 regression: GET /ui/work/new must render the form (200 HTML).

    Bug: the route was registered AFTER /{work_item_id}, so FastAPI bound
    work_item_id="new" and the handler returned 404 {"detail": "work item not
    found"} — exactly the screenshot in the plan.
    """
    cookies = _login_admin(monkeypatch)
    r = client.get("/ui/work/new", cookies=cookies)
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert 'action="/ui/work/new"' in r.text


def test_work_new_form_admin_only(monkeypatch, ui_env):
    """Worker/observer hitting GET /new gets 403, not a JSON ID-lookup 404."""
    cookies = _login_other(monkeypatch, WORKER_DN, "worker")
    r = client.get("/ui/work/new", cookies=cookies)
    assert r.status_code == 403


def test_real_id_and_unknown_id_distinct(monkeypatch, ui_env):
    """§5.2: real id → 200, unknown id → 404, static /new → 200 — all three coexist."""
    cookies = _login_admin(monkeypatch)
    item_id = _make_item(title="Route-order probe")
    assert client.get(f"/ui/work/{item_id}", cookies=cookies).status_code == 200
    r = client.get("/ui/work/does-not-exist", cookies=cookies, follow_redirects=False)
    assert r.status_code == 404
    assert client.get("/ui/work/new", cookies=cookies).status_code == 200


# ---------------------------------------------------------------- hierarchy + handoffs


def test_parent_child_and_handoff(monkeypatch, ui_env):
    cookies = _login_admin(monkeypatch)
    parent_id = _make_item(title="Parent project", kind="project")
    r = client.post(
        "/ui/work/new",
        cookies=cookies,
        data={"kind": "task", "parent_id": parent_id, "title": "Child task", "scope_markdown": ""},
        follow_redirects=False,
    )
    assert r.status_code == 303
    detail = client.get(f"/ui/work/{parent_id}", cookies=cookies)
    assert "Child task" in detail.text  # children table

    hr = client.post(
        f"/ui/work/{parent_id}/handoffs",
        cookies=cookies,
        data={"title": "Kickoff", "body_markdown": "# Handoff\n\nObserved: kickoff complete."},
        follow_redirects=False,
    )
    assert hr.status_code == 303
    detail2 = client.get(f"/ui/work/{parent_id}", cookies=cookies)
    assert "Kickoff" in detail2.text
    assert "Observed: kickoff complete." in detail2.text


def test_created_by_records_ui_user(monkeypatch, ui_env):
    cookies = _login_admin(monkeypatch)
    r = client.post(
        "/ui/work/new",
        cookies=cookies,
        data={"kind": "task", "parent_id": "", "title": "Provenance check", "scope_markdown": ""},
        follow_redirects=False,
    )
    assert r.status_code == 303
    item_id = r.headers["location"].rsplit("/", 1)[1]
    item = client.get(f"/api/v1/work/items/{item_id}", headers=AUTH).json()
    assert item["created_by"] == "jordan"
