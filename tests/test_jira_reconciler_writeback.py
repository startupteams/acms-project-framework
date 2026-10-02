import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from acms.telemetry_models import AgentEventRecord
from acms.work_models import WorkItemRecord


@pytest.fixture()
async def db(clean_db):
    from acms.db import get_session

    async for session in get_session():
        yield session


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def test_reconciler_sweep_posts_jira_handoff_exactly_once(
    db, monkeypatch, clean_db,
):
    """ADR-0012 fallback sweep must deliver the same Jira lifecycle write-back
    as the callback path (ACMS-REQ-063): on reconciled SUCCEEDED the Work Item
    moves to in_review, and exactly one JIRA_HANDOFF_POSTED event exists even
    when the sweep runs twice."""
    from acms.telemetry_scheduler import TelemetryScheduler
    import acms.bridge as bridge_mod

    work = WorkItemRecord(
        work_item_id=str(uuid.uuid4()), title="STNA-88 sweep case",
        status="active", created_by="test",
        created_at=_now(), updated_at=_now(),
        jira_issue_key="STNA-88",
        jira_url="https://example.atlassian.net/browse/STNA-88",
    )
    db.add(work)
    await db.commit()

    writes = {"comments": [], "transitions": []}

    class FakeJira:
        status_mutation_enabled = False

        def add_comment(self, key, body):
            writes["comments"].append((key, body))

        def transition_status(self, key, status):
            writes["transitions"].append((key, status))

    monkeypatch.setattr("acms.jira_client.JiraClient", FakeJira)
    # canonical_handoff imports JiraClient lazily inside its module too; the
    # finalize path resolves acms.jira_client.JiraClient at call time.
    monkeypatch.setattr("acms.canonical_handoff.JiraClient", FakeJira, raising=False)

    class _FakeBridge:
        def __init__(self, target):
            pass

        def _get(self, path):
            return {"status": "succeeded"}

    class _S:
        callback_reconcile_seconds = 60

    task_id = str(uuid.uuid4())
    from acms.work_models import ExecutionTaskRecord

    task = ExecutionTaskRecord(
        task_id=task_id, work_item_id=work.work_item_id, agent_id="agent-A",
        external_task_id="run-recon-jira", status="RUNNING", started_at=_now(),
    )
    db.add(task)
    await db.commit()

    monkeypatch.setattr(bridge_mod, "get_bridge_for_agent", lambda aid: _FakeBridge(None))

    sched = TelemetryScheduler(session_factory=None)
    sched._last_result_reconcile = None
    await sched._reconcile_execution_results(db, _S(), _now())

    await db.refresh(task)
    assert task.status == "SUCCEEDED"
    await db.refresh(work)
    assert work.status == "in_review"
    assert len(writes["comments"]) == 1
    # Status mutation disabled → no transition (fail-closed default).
    assert writes["transitions"] == []

    events = (await db.scalars(select(AgentEventRecord).where(
        AgentEventRecord.event_type == "JIRA_HANDOFF_POSTED"))).all()
    assert len(events) == 1
    meta = __import__("json").loads(events[0].metadata_json)
    assert meta["issue_key"] == "STNA-88"

    # A second sweep (e.g. a duplicate/later sweep on an already-terminal
    # task skips work; simulate the finalize re-entry guard instead) must not
    # duplicate the Jira write.
    from acms.completion_callback import _finalize_jira_workflow

    await _finalize_jira_workflow(
        db, task=task, terminal_status="SUCCEEDED",
        handoff=None, error_summary=None)
    assert len(writes["comments"]) == 1
