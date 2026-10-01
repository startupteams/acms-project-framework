"""Regression (live-found 2026-10-01): the usage-capture hook wrote
cost_attribution.outcome_id = "task:<task_id>" (41 chars) into a varchar(36)
column. On prod PostgreSQL the INSERT raised StringDataRightTruncation, which
poisoned the callback's session (PendingRollbackError) so the whole completion
callback 500-looped and the task stayed RUNNING forever.

SQLite never raised (it doesn't enforce varchar lengths) — the same silent
prod gap class as the FK lesson. The synthetic outcome id is now the raw
task id (36 chars)."""
from __future__ import annotations

import uuid

import pytest

from acms.completion_callback import CompletionCallbackRequest, process_completion_callback
from acms.economics_models import CostAttributionRecord, PrOutcomeRecord
from acms.work_models import ExecutionTaskRecord

from .conftest import clean_db  # noqa: F401  (fixture import)


@pytest.fixture()
async def db(clean_db):
    from acms.db import get_session

    async for session in get_session():
        yield session


async def _mk_task(db, *, agent_id="agent-A", external="run-123"):
    rec = ExecutionTaskRecord(
        task_id=str(uuid.uuid4()), work_item_id="w-1", agent_id=agent_id,
        external_task_id=external, status="RUNNING", started_at=_now_ts(),
        effective_model="qwen3.8-flash-next",
    )
    db.add(rec)
    await db.commit()
    await db.refresh(rec)
    return rec


def _now_ts():
    from acms.telemetry_service import _now

    return _now()


async def test_usage_capture_outcome_id_fits_varchar36(db):
    task = await _mk_task(db)

    out = await process_completion_callback(
        db, CompletionCallbackRequest(task_id=task.task_id, acms_agent_id="agent-A",
                                      a2a_run_id="run-123", status="SUCCEEDED",
                                      usage_reference={"input_tokens": 13592,
                                                       "output_tokens": 72}))
    assert out["result"] == "closed"

    rows = (await db.execute(
        __import__("sqlalchemy").select(CostAttributionRecord))).scalars().all()
    assert len(rows) == 1
    # The synthetic outcome id IS the raw task id — fits varchar(36) on PG.
    assert rows[0].outcome_id == task.task_id
    assert len(rows[0].outcome_id) <= 36
    assert rows[0].input_tokens == 13592
    assert rows[0].output_tokens == 72
    assert rows[0].provider == "local"

    # FK satisfied: the execution-outcome row exists (OPEN, linked to the task)
    outcome = await db.get(PrOutcomeRecord, task.task_id)
    assert outcome is not None
    assert outcome.outcome_state == "OPEN"
    assert outcome.task_category == "execution"
