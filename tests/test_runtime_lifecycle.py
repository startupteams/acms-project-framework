"""Runtime lifecycle integration (flight plan Phase 3, REV4 §12/§13).

Administrator Start/Stop/Restart/Reconcile runtime controls via Server
Manager; runtime events land in the semantic event history; Work preservation
on stop with active assignment; A2A controls stay separate.
"""
from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

AUTH = {"Authorization": "Bearer test-admin-token"}


@pytest.fixture()
def client(monkeypatch):
    import anyio

    from acms.db import SessionLocal
    from acms.main import app
    from acms.settings import get_settings

    # register work/telemetry/SM tables on Base.metadata (conftest only imports acms.models)
    from acms.db import Base as _Base
    from acms import work_models as _wm  # noqa: F401
    from acms import telemetry_models as _tm  # noqa: F401
    from acms import server_manager_api as _sm  # noqa: F401

    async def _ensure_tables():
        async with engine.begin() as conn:
            await conn.run_sync(_Base.metadata.create_all)

    import acms.db as _acms_db
    engine = _acms_db.engine
    anyio.run(_ensure_tables)
    async def _ensure():
        async with SessionLocal() as session:
            await session.execute(text(
                "CREATE TABLE IF NOT EXISTS provisioning_requests ("
                " id VARCHAR(36) PRIMARY KEY, agent_id VARCHAR(36), request_id VARCHAR(64),"
                " job_id VARCHAR(64), runtime_id VARCHAR(64), state VARCHAR(30),"
                " authority VARCHAR(60), approver VARCHAR(120), error TEXT,"
                " attempt INTEGER, created_at TIMESTAMP, updated_at TIMESTAMP)"
            ))
            await session.commit()

    anyio.run(_ensure)
    monkeypatch.setattr(get_settings(), "admin_token", "test-admin-token")
    monkeypatch.setattr(get_settings(), "server_manager_base_url", "http://sm.test")
    monkeypatch.setattr(get_settings(), "server_manager_token", "tok")
    return TestClient(app)


class FakeSM:
    """In-memory SM client stand-in (monkeypatched methods)."""

    desired = {}
    actual = {}
    reconcile_calls = []
    desired_calls = []

    @classmethod
    def reset(cls):
        cls.desired = {}
        cls.actual = {}
        cls.reconcile_calls = []
        cls.desired_calls = []

    @classmethod
    def handle(cls, runtime_id="rt-1", actual="RUNNING", raw_extra=None):
        from acms.server_manager_client import RuntimeHandle

        raw = {"runtime_id": runtime_id, "acms_agent_id": "a", "desired_state": cls.desired.get(runtime_id, "DESIRED_RUNNING"),
               "actual_state": actual, "recovery_count": 1, "last_error": None,
               "last_reconcile_at": "2026-09-28T01:00:00+00:00", "node": "miam00111", "vmid": 124,
               "state_sync_health": "VERIFIED", "hermes_state_branch": "agent/22ac1b19",
               "last_state_commit_sha": "180dd563", "provisioning_request_id": "req-x"}
        raw.update(raw_extra or {})
        return RuntimeHandle(runtime_id=runtime_id, acms_agent_id="a", actual_state=actual,
                             vmid=124, node="miam00111", hermes_state_branch="agent/22ac1b19", raw=raw)


@pytest.fixture()
def sm_patches(monkeypatch):
    from acms.server_manager_client import ServerManagerClient

    def fake_list(self, acms_agent_id=None):
        return [FakeSM.handle()]

    def fake_desired(self, runtime_id, desired_state, reason):
        FakeSM.desired_calls.append((runtime_id, desired_state, reason))
        FakeSM.desired[runtime_id] = desired_state
        FakeSM.actual[runtime_id] = "RUNNING" if desired_state == "DESIRED_RUNNING" else "STOPPED"
        return FakeSM.handle(actual=FakeSM.actual[runtime_id])

    def fake_reconcile(self, runtime_id):
        FakeSM.reconcile_calls.append(runtime_id)
        desired = FakeSM.desired.get(runtime_id, "DESIRED_RUNNING")
        FakeSM.actual[runtime_id] = "RUNNING" if desired == "DESIRED_RUNNING" else "STOPPED"
        return {"runtime_id": runtime_id, "desired": desired, "before": "STOPPED",
                "after": FakeSM.actual[runtime_id],
                "action": "noop" if desired == "DESIRED_RUNNING" else "shutdown", "error": None,
                "detail": {}}

    monkeypatch.setattr(ServerManagerClient, "list_runtimes", fake_list)
    monkeypatch.setattr(ServerManagerClient, "set_desired_state", fake_desired)
    monkeypatch.setattr(ServerManagerClient, "reconcile", fake_reconcile)
    FakeSM.reset()
    return FakeSM


def _create_agent(client, name="acms-worker-001"):
    r = client.post("/api/v1/server-manager/agents", json={
        "display_name": name, "authority": "pre-authorized_sprint_execution_context",
        "approver": "jordan"}, headers=AUTH)
    assert r.status_code == 201, r.text
    return r.json()["agent_id"]


def test_runtime_requires_admin(client):
    r = client.get("/api/v1/fleet/agents/whatever/runtime")
    assert r.status_code == 401


def test_runtime_view(client, sm_patches):
    agent_id = _create_agent(client)
    r = client.get(f"/api/v1/fleet/agents/{agent_id}/runtime", headers=AUTH)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["desired_state"] == "DESIRED_RUNNING"
    assert d["actual_state"] == "RUNNING"
    assert d["state_sync_health"] == "VERIFIED"
    assert d["hermes_state_branch"] == "agent/22ac1b19"


def test_start_and_reconcile_flow(client, sm_patches):
    agent_id = _create_agent(client)
    sm_patches.desired["rt-1"] = "DESIRED_STOPPED"
    sm_patches.actual["rt-1"] = "STOPPED"
    r = client.post(f"/api/v1/fleet/agents/{agent_id}/runtime/start",
                    json={"reason": "flight test start"}, headers=AUTH)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["action"] == "START"
    assert d["desired_state"] == "DESIRED_RUNNING"
    assert ("rt-1", "DESIRED_RUNNING", "flight test start") in sm_patches.desired_calls
    assert sm_patches.reconcile_calls == ["rt-1"]  # reconcile executed after desired set


def test_stop_with_active_work_preserves_work_and_events(client, sm_patches):
    import anyio

    from acms.db import SessionLocal
    from acms.work_models import AssignmentRecord, WorkItemRecord

    agent_id = _create_agent(client)
    # create an ACTIVE assignment for this agent
    async def _mk():
        async with SessionLocal() as db:
            now = __import__("datetime").datetime.now(__import__("datetime").timezone.utc)
            wi = WorkItemRecord(work_item_id=str(uuid.uuid4()), title="flight work",
                                created_by="flight-test", created_at=now, updated_at=now)
            asg = AssignmentRecord(assignment_id=str(uuid.uuid4()), agent_id=agent_id,
                                       work_item_id=wi.work_item_id, status="ACTIVE",
                                       assigned_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc))
            db.add_all([wi, asg])
            await db.commit()

    anyio.run(_mk)

    r = client.post(f"/api/v1/fleet/agents/{agent_id}/runtime/stop",
                    json={"reason": "flight test stop"}, headers=AUTH)
    assert r.status_code == 200, r.text
    assert r.json()["action"] == "STOP"

    # Work assignment still ACTIVE
    async def _check_assignment():
        from sqlalchemy import select
        async with SessionLocal() as db:
            rows = (await db.execute(select(AssignmentRecord).where(
                AssignmentRecord.agent_id == agent_id))).scalars().all()
            return [x.status for x in rows]

    statuses = anyio.run(_check_assignment)
    assert statuses == ["ACTIVE"]

    # semantic events recorded
    r2 = client.get(f"/api/v1/fleet/events?agent_id={agent_id}", headers=AUTH)
    types = [e["event_type"] for e in r2.json()]
    assert "RUNTIME_STOP" in types
    assert "RUNTIME_STOPPED_WITH_ACTIVE_WORK" in types


def test_restart_issues_stop_then_start(client, sm_patches):
    agent_id = _create_agent(client)
    r = client.post(f"/api/v1/fleet/agents/{agent_id}/runtime/restart",
                    json={"reason": "flight test restart"}, headers=AUTH)
    assert r.status_code == 200, r.text
    seq = [(d, s) for _, d, s in sm_patches.desired_calls]
    assert seq == [("DESIRED_STOPPED", "flight test restart"),
                   ("DESIRED_RUNNING", "flight test restart")]


def test_reconcile_now(client, sm_patches):
    agent_id = _create_agent(client)
    r = client.post(f"/api/v1/fleet/agents/{agent_id}/runtime/reconcile",
                    json={"reason": "flight test reconcile"}, headers=AUTH)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["action"] == "reconcile"
    assert sm_patches.reconcile_calls == ["rt-1"]
    r2 = client.get(f"/api/v1/fleet/events?agent_id={agent_id}", headers=AUTH)
    assert "RUNTIME_RECONCILED" in [e["event_type"] for e in r2.json()]


def test_sm_unreachable_maps_502(client, monkeypatch):
    from acms.server_manager_client import ServerManagerClient, ServerManagerError

    agent_id = _create_agent(client)
    def fake_list(self, acms_agent_id=None):
        raise ServerManagerError("connection refused")
    monkeypatch.setattr(ServerManagerClient, "list_runtimes", fake_list)
    r = client.get(f"/api/v1/fleet/agents/{agent_id}/runtime", headers=AUTH)
    assert r.status_code == 502
