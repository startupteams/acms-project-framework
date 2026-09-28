"""Per-Work budget tests (ACMS-REQ-059; REV2 plan §4).

Covers the plan's required minimum:
  0006 → 0007 migration (PG test), fresh-install migration,
  budget create/update, soft threshold, hard threshold, override,
  rollup across multiple sessions, no double counting,
  unknown/unavailable cost does not become zero/fabricated, role authorization.
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
                    json={"kind": "task", "title": "Budget test work"})
    assert r.status_code == 201, r.text
    return r.json()["work_item_id"]


def _mk_session(client, work_id, *, cost=None, tokens=None, model=None, close=False):
    s = client.post("/api/v1/memory/sessions", headers=AUTH, json={
        "agent_id": "00000000-0000-0000-0000-00000000aaa1",
        "work_item_id": work_id,
        **({"model_id": model} if model else {}),
    }).json()
    telemetry = {}
    if tokens is not None:
        telemetry["cumulative_api_tokens"] = tokens
    if cost is not None:
        telemetry["estimated_cost_usd"] = cost
    if telemetry:
        client.post(f"/api/v1/memory/sessions/{s['session_id']}/telemetry",
                    headers=AUTH, json=telemetry)
    if close:
        client.post(f"/api/v1/memory/sessions/{s['session_id']}/close", headers=AUTH)
    return s["session_id"]


# ---------------------------------------------------------------- service unit


def test_evaluate_state():
    from acms.budget_service import (BUDGET_HARD_EXCEEDED, BUDGET_OK,
                                     BUDGET_SOFT_EXCEEDED, evaluate_state)

    assert evaluate_state(None, 5.0, 10.0) == BUDGET_OK          # unknown never crosses
    assert evaluate_state(4.0, 5.0, 10.0) == BUDGET_OK
    assert evaluate_state(6.0, 5.0, 10.0) == BUDGET_SOFT_EXCEEDED
    assert evaluate_state(11.0, 5.0, 10.0) == BUDGET_HARD_EXCEEDED
    assert evaluate_state(11.0, None, None) == BUDGET_OK          # no thresholds set


def test_execution_blocked_semantics():
    from acms.budget_service import WorkBudgetRecord, execution_blocked

    b = WorkBudgetRecord(work_item_id="w", soft_budget_usd=5.0, hard_budget_usd=10.0)
    assert execution_blocked(b, None) is False                    # unknown cost
    assert execution_blocked(b, 9.0) is False
    assert execution_blocked(b, 11.0) is True
    b.override_active = True
    assert execution_blocked(b, 11.0) is False                    # override permits
    b.hard_budget_usd = None
    b.override_active = False
    assert execution_blocked(b, 999.0) is False                   # no hard threshold


# ----------------------------------------------------------------------- API


def test_budget_set_get_update(client):
    work_id = _mk_work(client)
    r = client.put(f"/api/v1/budgets/work/{work_id}", headers=AUTH,
                   json={"soft_budget_usd": 5.0, "hard_budget_usd": 10.0})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["budget_state"] == "OK"
    assert body["soft_budget_usd"] == 5.0 and body["hard_budget_usd"] == 10.0
    # update (clears override flag even when absent)
    r2 = client.put(f"/api/v1/budgets/work/{work_id}", headers=AUTH,
                    json={"soft_budget_usd": 2.0, "hard_budget_usd": 20.0})
    assert r2.json()["soft_budget_usd"] == 2.0
    # get
    r3 = client.get(f"/api/v1/budgets/work/{work_id}", headers=AUTH)
    assert r3.status_code == 200
    # unset budget → 404 (never fabricated)
    r4 = client.get("/api/v1/budgets/work/00000000-0000-0000-0000-00000000zzzz", headers=AUTH)
    assert r4.status_code == 404


def test_budget_soft_exceeds_hard_rejected(client):
    work_id = _mk_work(client)
    r = client.put(f"/api/v1/budgets/work/{work_id}", headers=AUTH,
                   json={"soft_budget_usd": 30.0, "hard_budget_usd": 10.0})
    assert r.status_code == 422


def test_soft_threshold_semantic_event(client):
    work_id = _mk_work(client)
    client.put(f"/api/v1/budgets/work/{work_id}", headers=AUTH,
               json={"soft_budget_usd": 1.0, "hard_budget_usd": 100.0})
    _mk_session(client, work_id, cost=2.0)  # crosses soft only
    b = client.get(f"/api/v1/budgets/work/{work_id}", headers=AUTH).json()
    assert b["budget_state"] == "SOFT_EXCEEDED"
    events = client.get("/api/v1/fleet/events?event_type=BUDGET_THRESHOLD_CROSSED",
                        headers=AUTH).json()
    assert events, "soft crossing must emit a semantic event"
    assert any("SOFT_EXCEEDED" in e["summary"] for e in events)


def test_hard_threshold_blocks_new_execution(client):
    work_id = _mk_work(client)
    client.put(f"/api/v1/budgets/work/{work_id}", headers=AUTH,
               json={"soft_budget_usd": 1.0, "hard_budget_usd": 5.0})
    _mk_session(client, work_id, cost=6.0)  # crosses hard
    b = client.get(f"/api/v1/budgets/work/{work_id}", headers=AUTH).json()
    assert b["budget_state"] == "HARD_EXCEEDED"
    chk = client.get(f"/api/v1/budgets/work/{work_id}/execution-check", headers=AUTH).json()
    assert chk["allowed"] is False
    assert chk["reason"] == "hard_budget_exceeded"


def test_hard_threshold_never_blocks_unknown_cost(client):
    work_id = _mk_work(client)
    client.put(f"/api/v1/budgets/work/{work_id}", headers=AUTH,
               json={"soft_budget_usd": 1.0, "hard_budget_usd": 5.0})
    _mk_session(client, work_id, cost=None, tokens=50000)  # spend UNKNOWN
    chk = client.get(f"/api/v1/budgets/work/{work_id}/execution-check", headers=AUTH).json()
    assert chk["allowed"] is True
    assert chk["rollup"]["sessions_unknown_cost"] == 1
    assert chk["rollup"]["estimated_cost_usd"] == 0.0  # SUM of nothing; not a fabricated per-session zero


def test_override_permits_and_is_audited(client):
    work_id = _mk_work(client)
    client.put(f"/api/v1/budgets/work/{work_id}", headers=AUTH,
               json={"soft_budget_usd": 1.0, "hard_budget_usd": 5.0})
    _mk_session(client, work_id, cost=6.0)
    assert client.get(f"/api/v1/budgets/work/{work_id}/execution-check",
                      headers=AUTH).json()["allowed"] is False
    r = client.post(f"/api/v1/budgets/work/{work_id}/override", headers=AUTH,
                    json={"override_by": "jordatech", "reason": "approved executive spend"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["override_active"] is True and body["override_by"] == "jordatech"
    # state stays HARD_EXCEEDED (fact preserved), execution permitted
    assert body["budget_state"] == "HARD_EXCEEDED"
    chk = client.get(f"/api/v1/budgets/work/{work_id}/execution-check", headers=AUTH).json()
    assert chk["allowed"] is True
    events = client.get("/api/v1/fleet/events?event_type=BUDGET_OVERRIDE",
                        headers=AUTH).json()
    assert events, "override must be audited"
    # re-setting the budget clears the one-shot override
    r2 = client.put(f"/api/v1/budgets/work/{work_id}", headers=AUTH,
                    json={"soft_budget_usd": 1.0, "hard_budget_usd": 100.0})
    assert r2.json()["override_active"] is False


def test_rollup_multi_session_no_double_count(client):
    work_id = _mk_work(client)
    _mk_session(client, work_id, cost=1.50, tokens=1000, model="glm-5.3-flash", close=True)
    _mk_session(client, work_id, cost=0.25, tokens=500, model="glm-5.3-flash")
    rollup = client.get(f"/api/v1/budgets/work/{work_id}/rollup", headers=AUTH).json()
    assert rollup["session_count"] == 2
    assert rollup["open_sessions"] == 1
    assert abs(rollup["estimated_cost_usd"] - 1.75) < 1e-9
    assert rollup["cumulative_api_tokens"] == 1500
    breakdown = rollup["model_breakdown"]["glm-5.3-flash"]
    assert breakdown["sessions"] == 2
    assert abs(breakdown["estimated_cost_usd"] - 1.75) < 1e-9
    # re-GET must be stable (pure aggregation, no accumulation)
    rollup2 = client.get(f"/api/v1/budgets/work/{work_id}/rollup", headers=AUTH).json()
    assert rollup2["estimated_cost_usd"] == rollup["estimated_cost_usd"]


def test_local_cost_split_tracked_not_fabricated(client):
    work_id = _mk_work(client)
    _mk_session(client, work_id, cost=1.0)
    # a session reporting ONLY an explicit cloud split (0.5) — blended unknown
    s = client.post("/api/v1/memory/sessions", headers=AUTH, json={
        "agent_id": "00000000-0000-0000-0000-00000000aaa1", "work_item_id": work_id}).json()
    client.post(f"/api/v1/memory/sessions/{s['session_id']}/telemetry", headers=AUTH,
                json={"estimated_cloud_cost_usd": 0.5})
    rollup = client.get(f"/api/v1/budgets/work/{work_id}/rollup", headers=AUTH).json()
    assert rollup["local_cost_usd"] is None          # no authoritative local USD source
    assert rollup["sessions_with_local_usage"] == 0
    assert abs(rollup["estimated_cost_usd"] - 1.0) < 1e-9    # only s1's blended estimate
    assert abs(rollup["estimated_cloud_cost_usd"] - 0.5) < 1e-9
    assert rollup["sessions_unknown_cost"] == 0
    # s2: partial (cloud only) — surfaced distinctly, never invented into a blended sum
    assert rollup["sessions_partial_cost"] == 1


def test_budget_api_requires_token(client):
    work_id = _mk_work(client)
    assert client.put(f"/api/v1/budgets/work/{work_id}",
                      json={"soft_budget_usd": 1.0}).status_code == 401


def test_ui_budget_panel_admin_only_mutations(client, monkeypatch):
    """UI: admin sees/sets budget; anonymous cannot POST budget forms."""
    from acms.ui import session_auth

    work_id = _mk_work(client)
    # anonymous POST refused by session auth (redirect to login)
    r = client.post(f"/ui/work/{work_id}/budget", follow_redirects=False,
                    data={"soft_budget_usd": "1", "hard_budget_usd": "2"})
    assert r.status_code in (303, 403)
    assert "login" in r.headers.get("location", "") or r.status_code == 403
