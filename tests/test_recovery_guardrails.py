"""Phase C4/C5 recovery-guardrail tests (2026-09-29 human direction).

C4: agent RUNNING without an active primary assignment → informational
    AGENT_RUNNING_WITHOUT_ASSIGNMENT event, emitted ONCE per state entry, no
    auto-remediation (runtime untouched).
C5: Executive reassignment guardrail — max 3 reassignments per work item;
    identical retry (changed_dimension=none) refused; 4th attempt refused with
    durable REASSIGNMENT_LIMIT_REACHED (Attention-eligible, high).
"""
from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

AUTH = {"Authorization": "Bearer test-admin-token"}


def _events(client, event_type: str | None = None):
    from acms.db import engine
    from acms.telemetry_models import AgentEventRecord

    async def _pull():
        from sqlalchemy.ext.asyncio import AsyncSession
        from sqlalchemy.orm import sessionmaker

        maker = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        async with maker() as s:
            stmt = select(AgentEventRecord)
            if event_type:
                stmt = stmt.where(AgentEventRecord.event_type == event_type)
            rows = (await s.execute(stmt)).scalars().all()
            return [(e.event_type, e.agent_id, e.summary, e.metadata_json) for e in rows]

    return asyncio.run(_pull())


# ---------------------------------------------------------------------- C4

@pytest.fixture()
def client(monkeypatch, clean_db):
    from acms.main import app
    from acms.settings import get_settings

    monkeypatch.setattr(get_settings(), "admin_token", "test-admin-token")
    return TestClient(app)


def _mk_agent(client, ext: str) -> str:
    r = client.post("/api/v1/agents/register", headers=AUTH, json={
        "external_registration_id": ext, "display_name": "C4 Agent",
        "trust_class": "internal", "harness": "hermes", "bridge_version": "0.1",
        "protocol_version": "a2a", "capabilities": {}})
    return r.json()["agent_id"]


def _running_status(client, agent_id: str):
    """Push a heartbeat-style status: agent running."""
    from acms.telemetry_models import HeartbeatPayload, HeartbeatIdentity, HeartbeatSession

    payload = HeartbeatPayload(
        schema_version="acms-heartbeat-v1",
        identity=HeartbeatIdentity(acms_agent_id=agent_id, bridge_id="b", harness="hermes"),
        session=HeartbeatSession(session_id="sess-1", title=None, agent_running=True),
        platforms={"connected": ["api_server"]},
    )
    from acms.telemetry_service import TelemetryService

    async def _push():
        from acms.db import engine

        from sqlalchemy.ext.asyncio import AsyncSession
        from sqlalchemy.orm import sessionmaker

        maker = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        async with maker() as db:
            await TelemetryService.ingest_heartbeat(db, agent_id, payload)
            await db.commit()

    asyncio.run(_push())


def test_running_without_assignment_emits_once(client, monkeypatch):
    from acms.telemetry_scheduler import TelemetryScheduler
    from acms.settings import get_settings

    agent_id = _mk_agent(client, "c4-agent-ext")
    _running_status(client, agent_id)

    now = __import__("datetime").datetime(2026, 9, 29, 12, 0, 0,
                                          tzinfo=__import__("datetime").timezone.utc)
    sched = TelemetryScheduler(now_fn=lambda: now)
    s = get_settings()

    async def _tick():
        async with sched._session_factory() as db:
            statuses = await __import__("acms.telemetry_service", fromlist=["TelemetryService"]) \
                .TelemetryService.list_status(db)
            for status in statuses:
                await sched._evaluate_agent(db, s, status, now)

    asyncio.run(_tick())
    asyncio.run(_tick())  # second tick: must NOT re-emit
    evs = _events(client, "AGENT_RUNNING_WITHOUT_ASSIGNMENT")
    assert len(evs) == 1, f"expected exactly 1 state-entry event, got {len(evs)}"
    # and no remediation happened: the status row is untouched/agent still running
    # (the event is informational only)


def test_running_with_assignment_is_silent(client):
    from acms.telemetry_scheduler import TelemetryScheduler
    from acms.settings import get_settings

    agent_id = _mk_agent(client, "c4-assigned-ext")
    _running_status(client, agent_id)
    w = client.post("/api/v1/work/items", headers=AUTH,
                    json={"kind": "task", "title": "c4 assigned"}).json()["work_item_id"]
    r = client.post("/api/v1/work/assignments", headers=AUTH,
                    json={"work_item_id": w, "agent_id": agent_id})
    assert r.status_code in (200, 201), r.text

    now = __import__("datetime").datetime(2026, 9, 29, 12, 0, 0,
                                          tzinfo=__import__("datetime").timezone.utc)
    sched = TelemetryScheduler(now_fn=lambda: now)
    s = get_settings()

    async def _tick():
        async with sched._session_factory() as db:
            statuses = await __import__("acms.telemetry_service", fromlist=["TelemetryService"]) \
                .TelemetryService.list_status(db)
            for status in statuses:
                await sched._evaluate_agent(db, s, status, now)

    asyncio.run(_tick())
    assert _events(client, "AGENT_RUNNING_WITHOUT_ASSIGNMENT") == []


# ---------------------------------------------------------------------- C5

def test_reassignment_guard_limit_and_refusals(client):
    from acms.reassignment_guard import record_reassignment

    agent_a = _mk_agent(client, "c5-a")
    agent_b = _mk_agent(client, "c5-b")
    w = client.post("/api/v1/work/items", headers=AUTH,
                    json={"kind": "task", "title": "c5 guard"}).json()["work_item_id"]

    async def _run():
        from acms.db import engine

        from sqlalchemy.ext.asyncio import AsyncSession
        from sqlalchemy.orm import sessionmaker

        maker = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        async with maker() as db:
            out = []
            # identical retry refused (changed_dimension=none)
            out.append(await record_reassignment(
                db, work_item_id=w, work_key=None, from_agent_id=agent_a,
                to_agent_id=agent_b, reason="same thing again",
                changed_dimension="none"))
            # 3 allowed reassignments with real changes
            for i, dim in enumerate(("different_agent", "revised_instructions",
                                     "additional_context")):
                out.append(await record_reassignment(
                    db, work_item_id=w, work_key=None,
                    from_agent_id=agent_a if i % 2 == 0 else agent_b,
                    to_agent_id=agent_b if i % 2 == 0 else agent_a,
                    reason=f"attempt {i + 1}", changed_dimension=dim))
            # 4th real change refused — limit reached
            out.append(await record_reassignment(
                db, work_item_id=w, work_key=None, from_agent_id=agent_b,
                to_agent_id=agent_a, reason="one more",
                changed_dimension="different_strategy"))
            return out

    results = asyncio.run(_run())
    assert results[0]["allowed"] is False          # identical retry
    assert [r["allowed"] for r in results[1:4]] == [True, True, True]
    assert results[4]["allowed"] is False
    assert results[4]["limit_event_emitted"] is True

    recs = [e for e in _events(client, "REASSIGNMENT_RECORDED")]
    assert len(recs) == 3
    limits = _events(client, "REASSIGNMENT_LIMIT_REACHED")
    assert len(limits) == 1
    # every recorded reassignment carries its changed dimension
    import json as _json
    dims = [_json.loads(m[3])["changed_dimension"] for m in recs]
    assert dims == ["different_agent", "revised_instructions", "additional_context"]


def test_reassignment_limit_event_is_attention_eligible(client):
    """REASSIGNMENT_LIMIT_REACHED must appear on the derived Attention feed (high)."""
    from acms.reassignment_guard import record_reassignment
    from acms.attention_api import attention_items, SEVERITY

    assert SEVERITY.get("REASSIGNMENT_LIMIT_REACHED") == "high"
    assert SEVERITY.get("AGENT_RUNNING_WITHOUT_ASSIGNMENT") == "info"

    agent_a = _mk_agent(client, "c5-attn-a")
    agent_b = _mk_agent(client, "c5-attn-b")
    w = client.post("/api/v1/work/items", headers=AUTH,
                    json={"kind": "task", "title": "c5 attention"}).json()["work_item_id"]

    async def _run():
        from acms.db import engine

        from sqlalchemy.ext.asyncio import AsyncSession
        from sqlalchemy.orm import sessionmaker

        maker = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        async with maker() as db:
            for i, dim in enumerate(("different_agent", "revised_instructions",
                                     "additional_context")):
                await record_reassignment(
                    db, work_item_id=w, work_key=None, from_agent_id=agent_a,
                    to_agent_id=agent_b, reason=f"r{i}", changed_dimension=dim)
            await record_reassignment(
                db, work_item_id=w, work_key=None, from_agent_id=agent_b,
                to_agent_id=agent_a, reason="beyond limit",
                changed_dimension="different_strategy")
            return await attention_items(db)

    items = asyncio.run(_run())
    types = [i["event_type"] for i in items]
    assert "REASSIGNMENT_LIMIT_REACHED" in types
