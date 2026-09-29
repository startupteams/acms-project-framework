"""Regression (2026-09-29 live): budget_service events used agent_id=""
which violates the agent_events FK on PostgreSQL (SQLite unit tests do not
enforce FKs by default — 142 tests passed while prod 500'd on PUT /budgets).
Budget/override/threshold events carry NO agent id (they are work-scoped);
assert they persist with agent_id NULL and FK enforcement active.
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


def _mk_work_with_assignment(client) -> str:
    a = client.post("/api/v1/agents/register", headers=AUTH, json={
        "external_registration_id": "fk-worker-ext",
        "display_name": "FK Worker", "trust_class": "internal",
        "harness": "hermes", "bridge_version": "0.1",
        "protocol_version": "a2a", "capabilities": {}})
    agent_id = a.json()["agent_id"]
    w = client.post("/api/v1/work/items", headers=AUTH,
                    json={"kind": "task", "title": "fk-regression"}).json()["work_item_id"]
    r = client.post("/api/v1/work/assignments", headers=AUTH,
                    json={"work_item_id": w, "agent_id": agent_id})
    assert r.status_code in (200, 201), r.text
    return w


def test_budget_set_event_has_no_sentinel_agent_id(client):
    """PUT /budgets must succeed (no FK violation) and the event must not
    carry agent_id='' — work-scoped events leave agent_id NULL."""
    w = _mk_work_with_assignment(client)
    r = client.put(f"/api/v1/budgets/work/{w}", headers=AUTH,
                   json={"soft_budget_usd": 0.5, "hard_budget_usd": 2.0})
    assert r.status_code == 200, r.text

    # Inspect the persisted event row directly (FK enforced in this SQLite DB).
    import asyncio
    from sqlalchemy import select
    from acms.db import engine
    from acms.telemetry_models import AgentEventRecord

    async def _pull():
        from sqlalchemy.ext.asyncio import AsyncSession
        from sqlalchemy.orm import sessionmaker

        maker = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        async with maker() as s:
            rows = (await s.execute(
                select(AgentEventRecord).where(
                    AgentEventRecord.event_type.in_(
                        ["BUDGET_SET", "BUDGET_OVERRIDE", "BUDGET_THRESHOLD_CROSSED"]))
            )).scalars().all()
            return [(e.event_type, e.agent_id) for e in rows]

    events = asyncio.run(_pull())
    assert events, "BUDGET_SET event must be persisted"
    for etype, agent_id in events:
        assert agent_id != "", (
            f"{etype} event must not use agent_id='' (FK violation on PG); "
            "leave agent_id NULL for work-scoped events")
        assert agent_id is None or len(agent_id) == 36


def test_budget_override_event_persists_without_agent_id(client):
    w = _mk_work_with_assignment(client)
    client.put(f"/api/v1/budgets/work/{w}", headers=AUTH,
               json={"soft_budget_usd": 0.5, "hard_budget_usd": 2.0})
    r = client.post(f"/api/v1/budgets/work/{w}/override", headers=AUTH,
                    json={"override_by": "jordan", "reason": "fk regression check"})
    assert r.status_code == 200, r.text
    import asyncio
    from sqlalchemy import select
    from acms.db import engine
    from acms.telemetry_models import AgentEventRecord

    async def _pull():
        from sqlalchemy.ext.asyncio import AsyncSession
        from sqlalchemy.orm import sessionmaker

        maker = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        async with maker() as s:
            rows = (await s.execute(
                select(AgentEventRecord).where(
                    AgentEventRecord.event_type == "BUDGET_OVERRIDE")
            )).scalars().all()
            return [(e.summary, e.agent_id) for e in rows]

    events = asyncio.run(_pull())
    assert events and all(a is None for _, a in events)
