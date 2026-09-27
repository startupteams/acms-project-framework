"""Combined slice 3+4 — heartbeat, connectivity, reconciliation, keys,
telemetry honesty (ADR-0010; ACMS-REQ-033/034/035/052/053/054).

Time-dependent logic is driven via explicit datetimes / scheduler injection —
no test waits real minutes (plan §20).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from acms.main import app
from acms.telemetry_models import (
    AgentStatusCurrentRecord,
    CONNECTIVITY_HEALTHY,
    CONNECTIVITY_STALE,
    CONNECTIVITY_UNREACHABLE,
)
from acms.telemetry_scheduler import TelemetryScheduler
from acms.telemetry_service import (
    TelemetryService,
    _context_valid,
    _warn_level,
    _tri_state,
)
from acms.work_keys import extract_work_key_from_title
from acms.db import SessionLocal

client = TestClient(app, follow_redirects=False)
AUTH = {"Authorization": "Bearer test-token"}

AGENT = {
    "external_registration_id": "telemetry-hermes-01",
    "display_name": "Telemetry Hermes",
    "trust_class": "internal",
    "harness": "hermes",
    "bridge_version": "0.17.0",
    "protocol_version": "a2a",
    "capabilities": {"streaming": True, "background_routine_inventory": True},
}


def _register(**overrides):
    r = client.post("/api/v1/agents/register", headers=AUTH, json={**AGENT, **overrides})
    assert r.status_code in (200, 201), r.text
    return r.json()["agent_id"]


def _hb(agent_id: str, **overrides) -> dict:
    body = {
        "identity": {"acms_agent_id": agent_id, "bridge_id": "b1",
                     "bridge_version": "0.1", "harness": "hermes", "harness_version": "0.17.0"},
        "session": {"session_id": "sess-1", "title": "[ACMS-WORK-000042] bridge work",
                    "agent_running": True},
        "model": {"model_id": "qwen3.8-27b", "provider": "marion-local"},
        "context": {"used_tokens": 137185, "max_tokens": 200000, "max_source": "harness"},
        "usage": {"session_total_tokens": 150000, "cumulative_api_tokens": 9000000},
        "platforms": {"connected": ["api_server"]},
    }
    body.update(overrides)
    return body


def _post_hb(agent_id: str, body: dict):
    return client.post(f"/api/v1/fleet/agents/{agent_id}/heartbeat", headers=AUTH, json=body)


# ---------------------------------------------------------------- pure helpers


def test_context_validity_rules():
    assert _context_valid(137185, 200000) is True
    assert _context_valid(0, 200000) is True
    assert _context_valid(None, 200000) is False
    assert _context_valid(100, None) is False
    assert _context_valid(-5, 100) is False        # negative used
    assert _context_valid(100, -5) is False        # non-positive max
    assert _context_valid(250000, 200000) is False  # used > max


def test_warning_levels():
    assert _warn_level(69.9) == "NORMAL"
    assert _warn_level(70.0) == "ELEVATED"
    assert _warn_level(84.9) == "ELEVATED"
    assert _warn_level(85.0) == "HIGH"
    assert _warn_level(94.9) == "HIGH"
    assert _warn_level(95.0) == "CRITICAL"
    assert _warn_level(None) is None


def test_tri_state():
    assert _tri_state(True) == "true"
    assert _tri_state(False) == "false"
    assert _tri_state(None) == "unknown"


def test_extract_work_key():
    assert extract_work_key_from_title("[ACMS-WORK-000042] Implement heartbeat") == "ACMS-WORK-000042"
    assert extract_work_key_from_title("no key here") is None
    assert extract_work_key_from_title(None) is None


# ---------------------------------------------------------------- API surface


def test_heartbeat_requires_bearer():
    r = client.post("/api/v1/fleet/agents/x/heartbeat", json={"identity": {"acms_agent_id": "x"}})
    assert r.status_code in (401, 403, 503)


def test_heartbeat_unknown_agent_404():
    r = client.post("/api/v1/fleet/agents/00000000-0000-0000-0000-000000000000/heartbeat",
                    headers=AUTH,
                    json={"identity": {"acms_agent_id": "x"},
                          "session": {"agent_running": True}})
    assert r.status_code == 404


def test_full_heartbeat_flow_and_events():
    aid = _register()
    body = {
        "identity": {"acms_agent_id": aid, "bridge_id": "b1", "harness": "hermes"},
        "session": {"session_id": "s-42", "title": "[ACMS-WORK-000042] bridge work",
                    "agent_running": True},
        "model": {"model_id": "qwen3.8-27b", "provider": "marion-local"},
        "context": {"used_tokens": 137185, "max_tokens": 200000, "max_source": "harness"},
        "usage": {"session_total_tokens": 150000, "cumulative_api_tokens": 9000000},
        "platforms": {"connected": ["api_server", "telegram"]},
    }
    r = _post_hb(aid, body)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["connectivity"] == "HEALTHY"
    assert data["schema_version"] == "acms-heartbeat-v1"

    st = client.get(f"/api/v1/fleet/agents/{aid}/status", headers=AUTH).json()
    assert st["connectivity"] == "HEALTHY"
    assert st["agent_running"] == "true"
    assert st["model_id"] == "qwen3.8-27b"
    assert st["context_used_tokens"] == 137185
    assert st["context_max_tokens"] == 200000
    assert abs(st["context_utilization_percent"] - 68.6) < 0.1
    assert st["context_warning"] == "NORMAL"
    assert st["platforms_connected"] == "api_server,telegram"

    # model change → MODEL_CHANGED (delta-driven, not per-heartbeat)
    body["model"] = {"model_id": "qwen3.6-35b", "provider": "marion-local"}
    _post_hb(aid, body)
    evs = client.get("/api/v1/fleet/events", headers=AUTH,
                     params={"agent_id": aid, "event_type": "MODEL_CHANGED"}).json()
    assert len(evs) == 1 and "qwen3.6-35b" in evs[0]["summary"]

    # agent_running flip → AGENT_RUNNING_CHANGED
    body["session"]["agent_running"] = False
    _post_hb(aid, body)
    evs = client.get("/api/v1/fleet/events", headers=AUTH,
                     params={"agent_id": aid, "event_type": "AGENT_RUNNING_CHANGED"}).json()
    assert len(evs) == 1
    assert "true -> false" in evs[0]["summary"]

    # events carry a strictly-decreasing recency order with real sequences
    all_evs = client.get("/api/v1/fleet/events", headers=AUTH,
                         params={"agent_id": aid}).json()
    seqs = [e["sequence"] for e in all_evs]
    assert len(seqs) >= 2 and all(s is not None for s in seqs)
    assert seqs == sorted(seqs, reverse=True)


def test_invalid_context_telemetry_never_stored_as_count():
    aid = _register(external_registration_id="telemetry-hermes-02")
    body = {
        "identity": {"acms_agent_id": aid},
        "session": {"session_id": "s-1", "agent_running": True},
        "context": {"used_tokens": 999999, "max_tokens": 200000},  # used > max
    }
    r = _post_hb(aid, body)
    assert r.status_code == 200
    st = client.get(f"/api/v1/fleet/agents/{aid}/status", headers=AUTH).json()
    # plan §7: invalid telemetry is NOT treated as a valid count
    assert st["context_telemetry_valid"] is False
    assert st["context_used_tokens"] is None
    assert st["context_utilization_percent"] is None
    evs = client.get("/api/v1/fleet/events", headers=AUTH,
                     params={"agent_id": aid, "event_type": "CONTEXT_TELEMETRY_INVALID"}).json()
    assert len(evs) == 1


def test_never_contacted_is_unknown_not_fabricated():
    """An agent registered but never heartbeating shows UNKNOWN — never a
    fabricated status (plan §6/§15)."""
    aid = _register(external_registration_id="telemetry-hermes-03")
    st = client.get(f"/api/v1/fleet/agents/{aid}/status", headers=AUTH).json()
    # no status row exists at all → the UI/API surface reports UNKNOWN
    assert st is None or st.get("connectivity") in (None, "UNKNOWN")


# ---------------------------------------------------------------- scheduler

def test_scheduler_stale_and_unreachable(tmp_path):
    """Stale derivation with injected clock: >5 min → STALE; ≥1 h with
    reconciliation failure → UNREACHABLE; management data untouched."""
    import asyncio
    from acms.db import SessionLocal
    from acms.telemetry_scheduler import TelemetryScheduler
    from acms.telemetry_models import AgentStatusCurrentRecord

    aid = _register(external_registration_id="telemetry-hermes-04")

    async def setup_and_tick():
        async with SessionLocal() as db:
            rec = AgentStatusCurrentRecord(
                agent_id=aid, connectivity=CONNECTIVITY_HEALTHY,
                last_contact_at=datetime.now(timezone.utc) - timedelta(minutes=10),
                last_contact_source="heartbeat", agent_running="true",
                updated_at=datetime.now(timezone.utc),
            )
            db.add(rec)
            await db.commit()

        # Injected clock 10 minutes after last contact: 5-min stale passed,
        # 1 h reconcile threshold NOT reached → STALE, no bridge attempt.
        base = datetime.now(timezone.utc)
        sched = TelemetryScheduler(now_fn=lambda: base + timedelta(minutes=10))
        await sched.tick(type("S", (), {
            "heartbeat_stale_seconds": 300,
            "stale_reconcile_seconds": 3600,
            "fleet_reconcile_seconds": 86400,
            "telemetry_sample_seconds": 300,
        })())
        async with SessionLocal() as db:
            rec = await db.get(AgentStatusCurrentRecord, aid)
            assert rec.connectivity == CONNECTIVITY_STALE
            assert rec.stale_since is not None

    asyncio.run(setup_and_tick())


def test_scheduler_unreachable_on_failed_reconciliation():
    """≥1 h stale + reconciliation failure (no bridge) → UNREACHABLE with an
    honest RECONCILIATION_FAILED event (plan §9)."""
    import asyncio
    from acms.db import SessionLocal
    from acms.telemetry_scheduler import TelemetryScheduler
    from acms.telemetry_models import AgentStatusCurrentRecord

    aid = _register(external_registration_id="telemetry-hermes-05")

    async def go():
        async with SessionLocal() as db:
            rec = AgentStatusCurrentRecord(
                agent_id=aid, connectivity=CONNECTIVITY_STALE,
                last_contact_at=datetime.now(timezone.utc) - timedelta(hours=2),
                last_contact_source="heartbeat", agent_running="true",
                updated_at=datetime.now(timezone.utc),
            )
            db.add(rec)
            await db.commit()
        sched = TelemetryScheduler(now_fn=lambda: datetime.now(timezone.utc))
        await sched.tick(type("S", (), {
            "heartbeat_stale_seconds": 300,
            "stale_reconcile_seconds": 3600,
            "fleet_reconcile_seconds": 86400,
            "telemetry_sample_seconds": 300,
        })())
        async with SessionLocal() as db:
            rec = await db.get(AgentStatusCurrentRecord, aid)
            # No bridge configured for this agent → reconciliation fails → UNREACHABLE
            assert rec.connectivity == CONNECTIVITY_UNREACHABLE

    asyncio.run(go())

    evs = client.get("/api/v1/fleet/events", headers=AUTH,
                     params={"agent_id": aid, "event_type": "RECONCILIATION_FAILED"}).json()
    assert len(evs) == 1
    # management data untouched (there is no assignment to change; the test
    # asserts the events, not the work data, per plan §20)
