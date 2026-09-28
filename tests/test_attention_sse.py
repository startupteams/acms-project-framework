"""SSE transport + Attention UI tests (REV2 Phase D).

Covers the plan §6-D4 matrix:
- SSE connection requires auth (401 without token)
- ordered events (ascending durable sequence)
- reconnect/resume via Last-Event-ID → no duplicate semantic delivery
- Attention severity mapping (hard budget → critical)
- hard-budget event appears on the attention page (UI renders it)
- token telemetry does NOT create Attention noise (never in feed/stream)
"""
from __future__ import annotations

import json
import re

import pytest
from fastapi.testclient import TestClient

AUTH = {"Authorization": "Bearer test-admin-token"}


@pytest.fixture()
def client(monkeypatch, clean_db):
    from acms.main import app
    from acms.settings import get_settings

    monkeypatch.setattr(get_settings(), "admin_token", "test-admin-token")
    return TestClient(app)


def _seed_budget_hard_event(client):
    """Produce a hard-budget crossing (attention-eligible, SSE-eligible)."""
    w = client.post("/api/v1/work/items", headers=AUTH,
                    json={"kind": "task", "title": "sse-hard"}).json()["work_item_id"]
    client.put(f"/api/v1/budgets/work/{w}", headers=AUTH,
               json={"soft_budget_usd": 1.0, "hard_budget_usd": 2.0})
    s = client.post("/api/v1/memory/sessions", headers=AUTH, json={
        "agent_id": "00000000-0000-0000-0000-00000000ccc1",
        "work_item_id": w}).json()
    client.post(f"/api/v1/memory/sessions/{s['session_id']}/telemetry", headers=AUTH,
                json={"estimated_cost_usd": 9.0})
    client.post(f"/api/v1/memory/sessions/{s['session_id']}/close", headers=AUTH)
    return w


def _parse_sse(text: str):
    """Parse an SSE body into [(id, event, data_dict)]."""
    out = []
    for block in text.split("\n\n"):
        if not block.strip() or block.startswith(":"):
            continue
        mid = me = None
        data_lines = []
        for line in block.split("\n"):
            if line.startswith("id: "):
                mid = int(line[4:])
            elif line.startswith("event: "):
                me = line[7:]
            elif line.startswith("data: "):
                data_lines.append(line[6:])
        if mid is not None and me:
            out.append((mid, me, json.loads(data_lines[0]) if data_lines else {}))
    return out


def _stream(client, extra_headers=None, **kw):
    params = kw.pop("params", {})
    params.setdefault("max_events", 50)  # bound the infinite stream for tests
    headers = {**AUTH, **(extra_headers or {})}
    with client.stream("GET", "/api/v1/events/stream", headers=headers,
                       params=params, **kw) as r:
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/event-stream")
        body = "".join(chunk for chunk in r.iter_text())
    return body


def test_sse_requires_auth(client):
    r = client.get("/api/v1/events/stream")
    assert r.status_code == 401


def test_sse_ordered_events_no_token_noise(client):
    _seed_budget_hard_event(client)
    body = _stream(client)
    events = _parse_sse(body)
    assert events, "expected at least one semantic event on the stream"
    seqs = [e[0] for e in events]
    assert seqs == sorted(seqs), "events must be in ascending sequence order"
    assert all("BUDGET_THRESHOLD_CROSSED" == e[1] or e[1] != "TOKEN_TELEMETRY"
               for e in events)


def test_sse_reconnect_resume_no_duplicates(client):
    _seed_budget_hard_event(client)
    first = _parse_sse(_stream(client))
    assert first
    last_id = first[-1][0]
    # reconnect with Last-Event-ID: must NOT re-deliver anything ≤ last_id
    second = _parse_sse(_stream(client, extra_headers={"Last-Event-ID": str(last_id)}))
    ids = [e[0] for e in second]
    assert all(i > last_id for i in ids), f"replay after resume leaked ids {ids}"


def test_attention_severity_mapping_hard_budget(client):
    _seed_budget_hard_event(client)
    feed = client.get("/api/v1/attention", headers=AUTH).json()
    hard = [i for i in feed if i["event_type"] == "BUDGET_THRESHOLD_CROSSED"]
    assert hard, "hard crossing must appear in the feed"
    assert all(i["severity"] == "critical" for i in hard)


def test_attention_ui_renders_hard_budget(client, monkeypatch):
    _seed_budget_hard_event(client)
    from acms.ui.session_auth import issue_token
    from acms.settings import get_settings

    s = get_settings()
    monkeypatch.setattr(s, "session_secret", "ui-test-secret-0123456789abcdef")
    monkeypatch.setattr(s, "session_cookie_secure", False)
    token = issue_token("jordatech", "Administrator")
    c2 = TestClient(client.app)
    c2.cookies.set("acms_session", token)
    r = c2.get("/ui/attention")
    assert r.status_code == 200, r.text
    assert "BUDGET_THRESHOLD_CROSSED" in r.text
    assert "sev-critical" in r.text
    # filter works
    r2 = c2.get("/ui/attention", params={"severity": "critical"})
    assert r2.status_code == 200
    assert "BUDGET_THRESHOLD_CROSSED" in r2.text


def test_token_telemetry_never_attention(client):
    """Heartbeat/token telemetry events never qualify (they aren't in the
    allow-list; the feed only derives from ATTENTION_EVENT_TYPES)."""
    from acms.attention_api import ATTENTION_EVENT_TYPES

    for t in ATTENTION_EVENT_TYPES:
        assert "TOKEN" not in t and "HEARTBEAT" not in t
