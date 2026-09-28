"""Phase 10 slice-6 foundation tests — derived Attention feed (REV2 §15).

Proves: budget hard-crossing lands in the Attention feed as critical;
SESSION_ROTATION_REQUIRED appears; the feed is a pure derivation from the
audit log (no separate mutable state); severity filtering works.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

AUTH = {"Authorization": "Bearer test-admin-token"}


@pytest.fixture()
def client(monkeypatch, clean_db):
    from acms.main import app
    from acms.settings import get_settings

    monkeypatch.setattr(get_settings(), "admin_token", "test-admin-token")
    return TestClient(app)


def test_budget_hard_crossing_appears_as_attention(client):
    w = client.post("/api/v1/work/items", headers=AUTH,
                    json={"kind": "task", "title": "attn-budget"}).json()["work_item_id"]
    client.put(f"/api/v1/budgets/work/{w}", headers=AUTH,
               json={"soft_budget_usd": 1.0, "hard_budget_usd": 2.0})
    s = client.post("/api/v1/memory/sessions", headers=AUTH,
                    json={"agent_id": "00000000-0000-0000-0000-00000000ddd1",
                          "work_item_id": w}).json()
    client.post(f"/api/v1/memory/sessions/{s['session_id']}/telemetry", headers=AUTH,
                json={"estimated_cost_usd": 5.0})
    feed = client.get("/api/v1/attention", headers=AUTH).json()
    hard = [i for i in feed if i["event_type"] == "BUDGET_THRESHOLD_CROSSED"
            and "HARD_EXCEEDED" in i["summary"]]
    assert hard and hard[0]["severity"] == "critical"


def test_rotation_required_appears_as_attention(client):
    s = client.post("/api/v1/memory/sessions", headers=AUTH,
                    json={"agent_id": "00000000-0000-0000-0000-00000000ddd1"}).json()
    client.post(f"/api/v1/memory/sessions/{s['session_id']}/rotation-required",
                headers=AUTH, json={"reason": "test"})
    feed = client.get("/api/v1/attention", headers=AUTH).json()
    assert any(i["event_type"] == "SESSION_ROTATION_REQUIRED" for i in feed)


def test_severity_filter(client):
    s = client.post("/api/v1/memory/sessions", headers=AUTH,
                    json={"agent_id": "00000000-0000-0000-0000-00000000ddd1"}).json()
    client.post(f"/api/v1/memory/sessions/{s['session_id']}/rotation-required",
                headers=AUTH, json={"reason": "sev-filter-test"})
    crit = client.get("/api/v1/attention", headers=AUTH, params={"severity": "critical"}).json()
    assert all(i["severity"] == "critical" for i in crit)
    assert not any(i["event_type"] == "SESSION_ROTATION_REQUIRED" for i in crit)


def test_attention_requires_token(client):
    assert client.get("/api/v1/attention").status_code == 401


def test_normal_events_do_not_appear(client):
    """Plain telemetry/advisory events are NOT attention candidates (semantic
    state changes only — never token-stream noise)."""
    s = client.post("/api/v1/memory/sessions", headers=AUTH,
                    json={"agent_id": "00000000-0000-0000-0000-00000000ddd1"}).json()
    client.post(f"/api/v1/memory/sessions/{s['session_id']}/telemetry", headers=AUTH,
                json={"current_context_tokens": 1000, "max_context_tokens": 70000})
    feed = client.get("/api/v1/attention", headers=AUTH).json()
    assert not any(i["event_type"] in ("EXECUTION_SESSION_OPENED", "ROTATION_ADVISORY")
                   for i in feed)
