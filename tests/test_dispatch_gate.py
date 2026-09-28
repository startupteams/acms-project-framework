"""Budget-gated A2A dispatch tests (REV2 Phase C, REQ-059 gate).

Covers the plan §5-C5 minimum matrix:
  under budget → dispatch
  soft exceeded → dispatch + warning retained in event trail
  hard exceeded → no NEW dispatch + durable EXECUTION_REJECTED event
  override → dispatch proceeds (audited override honored)
  unknown cost → no fabricated zero, no block
  retry → no duplicate task (idempotency)
  existing in-flight task unaffected by later hard crossing
  role/authority: dispatch endpoint requires bearer token
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

AUTH = {"Authorization": "Bearer test-admin-token"}


@pytest.fixture()
def client(monkeypatch, clean_db):
    from acms.main import app
    from acms.settings import get_settings

    monkeypatch.setattr(get_settings(), "admin_token", "test-admin-token")
    return TestClient(app)


class FakeBridge:
    def __init__(self):
        self.calls = []

    def send_work(self, *, work_key, assignment_key, instruction, session_id=None):
        self.calls.append({"work_key": work_key, "assignment_key": assignment_key,
                           "instruction": instruction, "session_id": session_id})
        return {"run_id": f"run-{len(self.calls)}", "status": "queued"}


@pytest.fixture()
def fake_bridge(monkeypatch):
    from acms import dispatch_service

    fb = FakeBridge()
    monkeypatch.setattr(dispatch_service, "get_bridge_for_agent", lambda agent_id: fb)
    return fb


def _mk_agent(client) -> str:
    r = client.post("/api/v1/agents/register", headers=AUTH, json={
        "external_registration_id": "dispatch-worker-ext",
        "display_name": "Dispatch Worker", "trust_class": "internal",
        "harness": "hermes", "bridge_version": "0.1",
        "protocol_version": "a2a", "capabilities": {"streaming": True}})
    assert r.status_code in (200, 201), r.text
    return r.json()["agent_id"]


def _agent_id(client) -> str:
    """Look up the registered agent's id (stable across clean_db wipes)."""
    from acms.registry import list_agents as _la
    # API path: /api/v1/agents is bearer-gated read; reuse it
    r = client.get("/api/v1/agents", headers=AUTH)
    if r.status_code == 200 and isinstance(r.json(), list) and r.json():
        return r.json()[0]["agent_id"]
    return _mk_agent(client)


def _mk_work_with_assignment(client) -> str:
    agent_id = _mk_agent(client)
    w = client.post("/api/v1/work/items", headers=AUTH,
                    json={"kind": "task", "title": "dispatch-gate"}).json()["work_item_id"]
    a = client.post("/api/v1/work/assignments", headers=AUTH,
                    json={"work_item_id": w, "agent_id": agent_id})
    assert a.status_code in (200, 201), a.text
    return w


def _set_budget(client, work_id, soft=None, hard=None):
    payload = {}
    if soft is not None:
        payload["soft_budget_usd"] = soft
    if hard is not None:
        payload["hard_budget_usd"] = hard
    r = client.put(f"/api/v1/budgets/work/{work_id}", headers=AUTH, json=payload)
    assert r.status_code == 200, r.text


def _mk_session(client, work_id):
    """A memory session for the work item (agent id resolved from assignment)."""
    s = client.post("/api/v1/memory/sessions", headers=AUTH, json={
        "agent_id": _agent_id(client), "work_item_id": work_id}).json()
    return s["session_id"]


def _add_cost(client, work_id, cost):
    sid = _mk_session(client, work_id)
    if cost is not None:
        client.post(f"/api/v1/memory/sessions/{sid}/telemetry",
                    headers=AUTH, json={"estimated_cost_usd": cost})
    client.post(f"/api/v1/memory/sessions/{sid}/close", headers=AUTH)


def _dispatch(client, work_id, **kw):
    body = {"instruction": kw.pop("instruction", "do the thing")}
    body.update(kw)
    return client.post(f"/api/v1/dispatch/work/{work_id}", headers=AUTH, json=body)


def _events(client, event_type):
    from acms.db import engine as _eng  # noqa: F401 — ensures module import
    import asyncio
    from sqlalchemy import select
    from acms.telemetry_models import AgentEventRecord
    from acms.db import get_session  # noqa: F401

    from acms.db import engine

    async def _pull():
        from sqlalchemy.ext.asyncio import AsyncSession
        from sqlalchemy.orm import sessionmaker

        maker = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        async with maker() as s:
            rows = (await s.execute(
                select(AgentEventRecord).where(
                    AgentEventRecord.event_type == event_type)
            )).scalars().all()
            return [(e.event_type, e.summary, e.metadata_json) for e in rows]

    return asyncio.run(_pull())


# ----------------------------------------------------------------- gate tests

def test_dispatch_under_budget(client, fake_bridge):
    w = _mk_work_with_assignment(client)
    r = _dispatch(client, w)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["dispatched"] is True
    assert body["task_id"]
    assert len(fake_bridge.calls) == 1


def test_dispatch_soft_exceeded_still_dispatches_with_warning(client, fake_bridge):
    w = _mk_work_with_assignment(client)
    _set_budget(client, w, soft=1.0)
    _add_cost(client, w, 2.0)
    r = _dispatch(client, w)
    assert r.status_code == 200
    body = r.json()
    assert body["dispatched"] is True
    assert body["budget_check"]["budget_state"] == "SOFT_EXCEEDED"
    assert len(fake_bridge.calls) == 1
    evs = _events(client, "EXECUTION_DISPATCHED")
    assert any("SOFT_EXCEEDED" in (s or "") for _, s, _ in evs)


def test_dispatch_hard_exceeded_blocks_new_execution(client, fake_bridge):
    w = _mk_work_with_assignment(client)
    _set_budget(client, w, soft=1.0, hard=5.0)
    _add_cost(client, w, 7.0)
    r = _dispatch(client, w)
    assert r.status_code == 200
    body = r.json()
    assert body["dispatched"] is False
    assert body["reason"] == "hard_budget_exceeded"
    assert fake_bridge.calls == []  # no cloud execution started
    evs = _events(client, "EXECUTION_REJECTED")
    assert len(evs) == 1
    meta = json.loads(evs[0][2] or "{}")
    assert meta["budget_check"]["allowed"] is False


def test_dispatch_hard_exceeded_with_override_proceeds(client, fake_bridge):
    w = _mk_work_with_assignment(client)
    _set_budget(client, w, soft=1.0, hard=5.0)
    _add_cost(client, w, 7.0)
    r = client.post(f"/api/v1/budgets/work/{w}/override", headers=AUTH,
                    json={"override_by": "jordan", "reason": "approved overage"})
    assert r.status_code == 200, r.text
    r2 = _dispatch(client, w, instruction="approved continuation")
    assert r2.status_code == 200
    assert r2.json()["dispatched"] is True
    assert len(fake_bridge.calls) == 1


def test_dispatch_unknown_cost_never_blocks(client, fake_bridge):
    w = _mk_work_with_assignment(client)
    _set_budget(client, w, soft=1.0, hard=5.0)
    # session exists with NO cost telemetry (unknown) — must not block
    sid = _mk_session(client, w)
    client.post(f"/api/v1/memory/sessions/{sid}/close", headers=AUTH)
    r = _dispatch(client, w)
    assert r.status_code == 200
    body = r.json()
    assert body["dispatched"] is True
    rollup = body["budget_check"]["rollup"]
    assert rollup["sessions_unknown_cost"] >= 1


def test_dispatch_retry_no_duplicate_task(client, fake_bridge):
    w = _mk_work_with_assignment(client)
    r1 = _dispatch(client, w, idempotency_key="job-42")
    assert r1.json()["dispatched"] is True
    r2 = _dispatch(client, w, instruction="retry of same job",
                   idempotency_key="job-42")
    assert r2.status_code == 200
    body = r2.json()
    assert body["dispatched"] is False
    assert body["reason"] == "duplicate"
    assert body["task_id"] == r1.json()["task_id"]
    assert len(fake_bridge.calls) == 1  # bridge hit exactly once


def test_inflight_task_unaffected_by_later_hard_crossing(client, fake_bridge):
    w = _mk_work_with_assignment(client)
    r1 = _dispatch(client, w, instruction="start").json()
    assert r1["dispatched"] is True
    # costs arrive pushing over the hard limit
    _set_budget(client, w, hard=5.0)
    _add_cost(client, w, 9.0)
    # a genuinely NEW dispatch (different idempotency basis) must be blocked...
    r2 = _dispatch(client, w, instruction="new dispatch attempt",
                   idempotency_key="attempt-2").json()
    assert r2["dispatched"] is False
    assert r2["reason"] == "hard_budget_exceeded"
    # ...while the in-flight task remains RUNNING (gate never touches it)
    tasks = client.get("/api/v1/work/tasks", headers=AUTH).json()
    mine = [x for x in tasks if x.get("task_id") == r1["task_id"]]
    assert mine and mine[0]["status"] == "RUNNING"


# ------------------------------------------------------- authority + structure

def test_dispatch_requires_token(client, work_env_none=None):
    w = _mk_work_with_assignment(client)
    r = client.post(f"/api/v1/dispatch/work/{w}", json={"instruction": "x"})
    assert r.status_code in (401, 403)


def test_dispatch_unknown_work_item_404(client):
    r = _dispatch(client, "00000000-0000-0000-0000-000000000000")
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "unknown-work-item"


def test_dispatch_no_assignment_409(client):
    w = client.post("/api/v1/work/items", headers=AUTH,
                    json={"kind": "task", "title": "no-assignment"}).json()["work_item_id"]
    r = _dispatch(client, w)
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "no-active-assignment"


def test_dispatch_no_bridge_target_503(client, monkeypatch):
    from acms import dispatch_service

    w = _mk_work_with_assignment(client)
    monkeypatch.setattr(dispatch_service, "get_bridge_for_agent", lambda agent_id: None)
    r = _dispatch(client, w)
    assert r.status_code == 503
    assert r.json()["detail"]["code"] == "no-bridge-target"
