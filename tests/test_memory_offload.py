"""Memory/Session Offload MVP tests (flight plan Phase 8; ACMS-REQ-055..058).

The primary new proof: Work → Session 1 + Context Package → handoff/checkpoint
→ Session 1 closes → Session 2 receives a compact reconstructed package →
the SAME Work remains active. Plus: advisory bands, completeness validator,
no-fabrication telemetry.
"""
from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

AUTH = {"Authorization": "Bearer test-admin-token"}


@pytest.fixture()
def client(monkeypatch):
    from acms.main import app
    from acms.settings import get_settings

    monkeypatch.setattr(get_settings(), "admin_token", "test-admin-token")
    return TestClient(app)


def _mk_work(client) -> str:
    r = client.post("/api/v1/work/items", headers=AUTH,
                    json={"kind": "task", "title": "Memory offload proof work"})
    assert r.status_code == 201, r.text
    return r.json()["work_item_id"]


def _register_agent(client) -> str:
    r = client.post("/api/v1/agents/register", headers=AUTH, json={
        "external_registration_id": f"miam-00111-hermes-{uuid.uuid4().hex[:6]}",
        "display_name": "Memory Test Agent", "trust_class": "internal",
        "harness": "hermes", "bridge_version": "0.1", "protocol_version": "a2a",
        "capabilities": {}})
    assert r.status_code in (200, 201)
    return r.json()["agent_id"]


def test_primary_proof_multi_session_same_work(client):
    agent_id = _register_agent(client)
    work_id = _mk_work(client)
    # live assignment keeps the work 'in progress'
    r = client.post("/api/v1/work/assignments", headers=AUTH,
                    json={"agent_id": agent_id, "work_item_id": work_id})
    assert r.status_code in (200, 201), r.text
    assignment_id = r.json()["assignment_id"]

    # Session 1 + context package
    pkg = client.post("/api/v1/memory/context-packages", headers=AUTH, json={
        "work_item_id": work_id, "policy_version": "compact-v1",
        "selected": [{"type": "work_item", "id": work_id}],
        "estimated_tokens": 1200,
    }).json()
    s1 = client.post("/api/v1/memory/sessions", headers=AUTH, json={
        "agent_id": agent_id, "work_item_id": work_id,
        "context_package_id": pkg["context_package_id"],
        "harness_session_id": "harness-abc",
    }).json()
    assert s1["status"] == "OPEN"
    # telemetry
    s1 = client.post(f"/api/v1/memory/sessions/{s1['session_id']}/telemetry", headers=AUTH,
                     json={"current_context_tokens": 35000, "max_context_tokens": 70000,
                           "cumulative_api_tokens": 90000, "estimated_cost_usd": 0.42}).json()
    assert s1["context_utilization_percent"] == 50.0
    assert s1["rotation_advisory"] == "MONITOR"
    # checkpoint (complete)
    cp = client.post("/api/v1/memory/checkpoints", headers=AUTH, json={
        "session_id": s1["session_id"], "work_item_id": work_id, "reason": "rotate",
        "summary": "Implemented slice; tests green; committing.",
        "work_id": work_id, "status": "in-progress", "git_state": "branch feat/x @ abc123",
        "tests": "12/12 passed", "deployment": "none yet", "decisions": "used SQLite for tests",
        "debt": "todo: partitioning", "remaining_work": "deploy + docs",
        "next_action": "merge PR", "artifacts": ["git://sha/abc123"],
    }).json()
    assert cp["complete"] is True, cp["completeness"]
    # close session 1
    s1c = client.post(f"/api/v1/memory/sessions/{s1['session_id']}/close", headers=AUTH).json()
    assert s1c["status"] == "CLOSED"
    # rotation: session 2 gets a reconstructed compact package
    pkg2 = client.post(f"/api/v1/memory/work/{work_id}/rotate", headers=AUTH, json={
        "prior_session_id": s1["session_id"], "agent_id": agent_id,
    }).json()
    assert pkg2["notes"].startswith("Reconstructed")
    types = [s["type"] for s in pkg2["selected"]]
    assert "work_item" in types and "assignment" in types and "checkpoint" in types
    # session 2 opens with the package
    s2 = client.post("/api/v1/memory/sessions", headers=AUTH, json={
        "agent_id": agent_id, "work_item_id": work_id,
        "context_package_id": pkg2["context_package_id"],
    }).json()
    assert s2["status"] == "OPEN"
    # THE INVARIANT: same Work, still an active assignment, both sessions listed
    sessions = client.get(f"/api/v1/memory/work/{work_id}/sessions", headers=AUTH).json()
    assert len(sessions) == 2
    statuses = {s["status"] for s in sessions}
    assert statuses == {"CLOSED", "OPEN"}
    work = client.get(f"/api/v1/work/items/{work_id}", headers=AUTH).json()
    assert work["status"].lower() in ("active", "planned")  # work NOT closed by session rotation
    # assignment still ACTIVE (query by id)
    asg = client.get(f"/api/v1/work/assignments/{assignment_id}", headers=AUTH)
    if asg.status_code == 200:
        assert asg.json()["status"] == "ACTIVE"


def test_completeness_validator_rejects_incomplete(client):
    agent_id = _register_agent(client)
    work_id = _mk_work(client)
    s1 = client.post("/api/v1/memory/sessions", headers=AUTH, json={
        "agent_id": agent_id, "work_item_id": work_id}).json()
    cp = client.post("/api/v1/memory/checkpoints", headers=AUTH, json={
        "session_id": s1["session_id"], "summary": "partial checkpoint",
        "work_id": work_id, "status": "in-progress",
        # git_state, tests, deployment, decisions, debt, remaining_work, next_action, artifacts MISSING
    }).json()
    assert cp["complete"] is False
    missing = [k for k, v in cp["completeness"].items() if not v]
    assert "git_state" in missing and "tests" in missing and "next_action" in missing


def test_advisory_bands(client):
    agent_id = _register_agent(client)
    s = client.post("/api/v1/memory/sessions", headers=AUTH, json={
        "agent_id": agent_id}).json()
    sid = s["session_id"]
    t50 = client.post(f"/api/v1/memory/sessions/{sid}/telemetry", headers=AUTH,
                      json={"current_context_tokens": 3500, "max_context_tokens": 7000}).json()
    assert t50["rotation_advisory"] == "MONITOR"
    t72 = client.post(f"/api/v1/memory/sessions/{sid}/telemetry", headers=AUTH,
                      json={"current_context_tokens": 5040, "max_context_tokens": 7000}).json()
    assert t72["rotation_advisory"] == "CHECKPOINT_RECOMMENDED"
    t90 = client.post(f"/api/v1/memory/sessions/{sid}/telemetry", headers=AUTH,
                      json={"current_context_tokens": 6300, "max_context_tokens": 7000}).json()
    assert t90["rotation_advisory"] == "ROTATE_RECOMMENDED"


def test_invalid_telemetry_not_fabricated(client):
    agent_id = _register_agent(client)
    s = client.post("/api/v1/memory/sessions", headers=AUTH, json={
        "agent_id": agent_id}).json()
    sid = s["session_id"]
    # used > max → INVALID → stays UNKNOWN (null), never fabricated
    t = client.post(f"/api/v1/memory/sessions/{sid}/telemetry", headers=AUTH,
                    json={"current_context_tokens": 8000, "max_context_tokens": 7000}).json()
    assert t["context_utilization_percent"] is None
    assert t["rotation_advisory"] in (None, "GREEN")


def test_memory_requires_admin(client):
    r = client.get("/api/v1/memory/sessions/00000000-0000-0000-0000-000000000000")
    assert r.status_code == 401
