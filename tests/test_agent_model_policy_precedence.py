"""Regression (live-found 2026-10-01): agent-scope model policies were stored
but never read at dispatch — the dispatcher used the work-item-only resolver,
so the ADR-0015 agent layer silently never won. resolve_policy(work, agent)
completes the Work > Agent > Project > System precedence."""
from __future__ import annotations

import pytest

from acms.a2a_models import ModelPolicyRecord
from acms.model_policy import resolve_policy, resolve_policy_for_work_item
from acms.work_models import WorkItemCreate, WorkItemRecord

from .conftest import clean_db  # noqa: F401  (fixture import)


@pytest.fixture()
async def db(clean_db):
    from acms.db import get_session

    async for session in get_session():
        yield session


async def _seed_system(db):
    rec = (await db.scalars(
        __import__("sqlalchemy").select(ModelPolicyRecord).where(
            ModelPolicyRecord.scope == "system"))).first()
    if rec is None:
        from acms import model_policy as mp

        await mp.upsert_policy(db, scope="system", scope_id=None,
                               inference_policy="local-preferred",
                               preferred_model="qwen3.8-flash-next",
                               cloud_fallback=True, updated_by="test")


@pytest.mark.asyncio
async def test_agent_layer_wins_over_system(db):
    from sqlalchemy import select

    from acms import model_policy as mp

    await _seed_system(db)
    work = WorkItemRecord(work_item_id="w-agent-test", title="agent precedence",
                          created_at=WorkItemRecord.now(), updated_at=WorkItemRecord.now())
    db.add(work)
    agent_id = "agent-uuid-1234"
    await mp.upsert_policy(db, scope="agent", scope_id=agent_id,
                           inference_policy="local-preferred",
                           preferred_model="gemma4-26b-a4b",
                           cloud_fallback=False, updated_by="test")

    # work-item-only resolver: system (agent unknown to it — documented gap)
    r_work = await resolve_policy_for_work_item(db, "w-agent-test")
    assert r_work["resolution_reason"] == "system"

    # FULL resolver (dispatcher path): agent wins
    r_full = await resolve_policy(db, "w-agent-test", agent_id)
    assert r_full["resolution_reason"] == "agent"
    assert r_full["preferred_model"] == "gemma4-26b-a4b"
    assert r_full["cloud_fallback"] is False

    # another agent → falls through to system
    r_other = await resolve_policy(db, "w-agent-test", "agent-other-9999")
    assert r_other["resolution_reason"] == "system"
    assert r_other["preferred_model"] == "qwen3.8-flash-next"


@pytest.mark.asyncio
async def test_work_item_override_still_wins_over_agent(db):
    from sqlalchemy import select

    from acms import model_policy as mp

    await _seed_system(db)
    work = WorkItemRecord(work_item_id="w-override-test", title="override precedence",
                          created_at=WorkItemRecord.now(), updated_at=WorkItemRecord.now())
    db.add(work)
    agent_id = "agent-uuid-5678"
    await mp.upsert_policy(db, scope="agent", scope_id=agent_id,
                           inference_policy="local-preferred",
                           preferred_model="gemma4-26b-a4b",
                           cloud_fallback=True, updated_by="test")
    await mp.upsert_policy(db, scope="work_item", scope_id="w-override-test",
                           inference_policy="local-only",
                           preferred_model="qwen3.6-35b-a3b",
                           cloud_fallback=False, updated_by="test")

    r = await resolve_policy(db, "w-override-test", agent_id)
    assert r["resolution_reason"] == "work_override"
    assert r["preferred_model"] == "qwen3.6-35b-a3b"
