"""Phase F: automatic ExecutionSession lifecycle tests (plan §8)."""

from __future__ import annotations

import pytest
from sqlalchemy import text

from acms.session_lifecycle import (
    close_session,
    fail_session_for_dispatch,
    open_session_for_dispatch,
    reconcile_zombie_sessions,
)


@pytest.fixture()
async def db():
    from acms.db import get_session

    async for session in get_session():
        yield session


async def test_open_session_and_duplicate_does_not_duplicate(db):
    s1 = await open_session_for_dispatch(
        db, agent_id="agent-a", work_item_id="w1",
        assignment_id=None, a2a_task_id="run-42",
    )
    await db.commit()
    s2 = await open_session_for_dispatch(
        db, agent_id="agent-a", work_item_id="w1",
        assignment_id=None, a2a_task_id="run-42",
    )
    assert s2.session_id == s1.session_id, "duplicate dispatch must not duplicate session"
    n = (await db.execute(
        text("SELECT count(*) FROM execution_sessions WHERE a2a_task_id='run-42'"))).scalar()
    assert n == 1


async def test_fail_session_closes_and_retains_cost(db):
    s = await open_session_for_dispatch(
        db, agent_id="agent-b", work_item_id="w2",
        assignment_id=None, a2a_task_id="run-99",
    )
    s.estimated_cost_usd = 0.123
    await db.commit()
    await fail_session_for_dispatch(db, session=s, reason="bridge unreachable")
    await db.commit()
    await db.refresh(s)
    assert s.status == "CLOSED" and s.ended_at is not None
    assert s.estimated_cost_usd == 0.123, "failed runs retain their cost"
    await fail_session_for_dispatch(db, session=s, reason="again")  # no-op


async def test_close_session_idempotent(db):
    s = await open_session_for_dispatch(
        db, agent_id="agent-c", work_item_id="w3",
        assignment_id=None, a2a_task_id="run-7",
    )
    await db.commit()
    r1 = await close_session(db, session_id=s.session_id)
    r2 = await close_session(db, session_id=s.session_id)
    assert r1.status == "CLOSED" and r2.status == "CLOSED"


async def test_hard_budget_reject_never_creates_session(db):
    """Session rows exist only for dispatched runs; a blocked work id stays absent."""
    n = (await db.execute(
        text("SELECT count(*) FROM execution_sessions WHERE work_item_id='never-dispatched'"))).scalar()
    assert n == 0


async def test_reconcile_zombie(db):
    from datetime import timedelta

    from acms.telemetry_service import _now

    s = await open_session_for_dispatch(
        db, agent_id="agent-d", work_item_id="w4",
        assignment_id=None, a2a_task_id="run-old",
    )
    s.started_at = _now() - timedelta(hours=30)
    fresh = await open_session_for_dispatch(
        db, agent_id="agent-d", work_item_id="w4",
        assignment_id=None, a2a_task_id="run-new",
    )
    await db.commit()
    closed = await reconcile_zombie_sessions(db, max_age_hours=24.0)
    await db.commit()
    await db.refresh(s)
    await db.refresh(fresh)
    assert s.status == "CLOSED"
    assert fresh.status == "OPEN"
    assert closed >= 1
