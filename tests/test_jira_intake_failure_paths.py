"""Section-10 verification matrix for ACMS-REQ-063 (STNA-88 construction plan §10).

Covers the failure paths the intake/write-back PR left without direct tests:
  case 3 — unmapped Jira project → no Work Item + durable outcome
  case 4 — no idle worker → queued_no_worker, no corrupt assignment state
  case 5 — dispatch failure → primary assignment lock RELEASED durably
  case 8 — Jira write-back failure → execution success preserved + JIRA_OUTBOUND_FAILED
"""
import asyncio
from datetime import datetime, timezone

import pytest
from sqlalchemy import func, select

from acms.a2a_models import ProjectRecord
from acms.db import SessionLocal
from acms.jira_client import JiraIssue
from acms.jira_models import JiraIssueLinkRecord, JiraReconciliationRunRecord
from acms.jira_reconcile import ReconcileCounts, _intake_and_start
from acms.telemetry_models import AgentEventRecord
from acms.work_models import AssignmentRecord, WorkItemRecord

_AI_ACCOUNT = "712020:520fb263-ef0f-425c-a0be-14e9d258917e"


@pytest.fixture()
def gate_env(monkeypatch):
    """Settings via env vars + cache_clear (the proven lru_cache-safe pattern)."""
    from acms.settings import get_settings

    monkeypatch.setenv("ACMS_JIRA_AI_ACCOUNT_ID", _AI_ACCOUNT)
    monkeypatch.setenv("ACMS_JIRA_READY_STATUSES", "TO START")
    monkeypatch.delenv("ACMS_JIRA_BASE_URL", raising=False)
    monkeypatch.delenv("ACMS_JIRA_EMAIL", raising=False)
    monkeypatch.delenv("ACMS_JIRA_API_TOKEN", raising=False)
    monkeypatch.setenv("ACMS_JIRA_RECONCILE_INTERVAL_HOURS", "24")
    monkeypatch.setenv("ACMS_JIRA_RECONCILE_ENABLED", "false")
    monkeypatch.setenv("ACMS_ADMIN_TOKEN", "test-token")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture()
async def db(clean_db):
    from acms.db import get_session

    async for session in get_session():
        yield session


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _issue(key="STNA-88", issue_id="10088", status="TO START",
           project_key="STNA", account=_AI_ACCOUNT):
    return JiraIssue(
        key=key, issue_id=issue_id, summary="Section 10 case",
        description="Version #: 0.12.0", status=status,
        assignee="Startup Teams", assignee_account_id=account,
        priority="Medium", labels=[],
        url=f"https://example.atlassian.net/browse/{key}",
        project_key=project_key, issue_type="Task",
    )


def _run(run_id="run-s10"):
    return JiraReconciliationRunRecord(
        run_id=run_id, trigger="manual_check", requested_by="test", state="running",
    )


def _project(key="STNA"):
    now = _now()
    return ProjectRecord(
        project_id=ProjectRecord.new_id(), name=f"Proj {key}", slug=key.lower(),
        jira_project_key=key, repos="startupteams/acms-project-framework",
        created_at=now, updated_at=now,
    )


# --------------------------------------------------------------- case 3

def test_unmapped_project_creates_no_work_and_records_durable_outcome(db, monkeypatch, gate_env):
    async def scenario():
        async with SessionLocal() as s:
            run = _run()
            s.add(run)
            await s.commit()
            counts = ReconcileCounts()

            # No ProjectRecord → the fail-closed mapping check inside
            # _intake_and_start must fire. _process_observation persists the
            # durable outcome event per examined issue.
            seen = set()
            from acms.jira_reconcile import _process_observation
            await _process_observation(s, run, _issue(), None, counts, seen)
            assert counts.added == 0 and counts.started == 0
            assert counts.unchanged == 0
            assert (await s.scalar(select(func.count()).select_from(WorkItemRecord))) == 0
            assert (await s.scalar(
                select(func.count()).select_from(JiraIssueLinkRecord))) == 0
            outcomes = (await s.scalars(select(AgentEventRecord).where(
                AgentEventRecord.event_type == "JIRA_RECONCILIATION_OUTCOME"))).all()
            assert len(outcomes) == 1
            meta = __import__("json").loads(outcomes[0].metadata_json)
            assert meta["outcome"] == "project_unmapped"

    asyncio.run(scenario())


# --------------------------------------------------------------- case 4

def test_no_idle_worker_queues_without_assignment(db, gate_env):
    async def scenario():
        async with SessionLocal() as s:
            s.add(_project())
            run = _run()
            s.add(run)
            await s.commit()
            counts = ReconcileCounts()
            result = await _intake_and_start(s, run, _issue(), counts)
            # No agents exist at all → no idle worker → queued, work created.
            assert result["outcome"] == "queued_no_worker"
            assert result["work_item_id"]
            assert counts.added == 1 and counts.started == 0
            # Work item exists, but NO assignment row was created.
            assert (await s.scalar(
                select(func.count()).select_from(AssignmentRecord))) == 0
            # Exactly one work item and one link (no corruption).
            assert (await s.scalar(select(func.count()).select_from(WorkItemRecord))) == 1
            assert (await s.scalar(
                select(func.count()).select_from(JiraIssueLinkRecord))) == 1

    asyncio.run(scenario())


# --------------------------------------------------------------- case 5

def test_dispatch_exception_releases_primary_assignment_lock(db, monkeypatch, gate_env):
    async def scenario():
        async with SessionLocal() as s:
            s.add(_project())
            run = _run()
            s.add(run)
            await s.commit()

            from acms.models import AgentRecord

            agent = AgentRecord(
                agent_id="agent-locked", display_name="worker-x",
                external_registration_id="arm-worker-x", harness="hermes",
                worker_uid="001", trust_class="internal",
                bridge_version="0.19.0", protocol_version="1",
                created_at=_now(), updated_at=_now(),
            )
            s.add(agent)
            await s.commit()

            from acms.telemetry_models import AgentStatusCurrentRecord

            s.add(AgentStatusCurrentRecord(
                agent_id="agent-locked", connectivity="HEALTHY",
                agent_running="running", last_contact_at=_now(),
                updated_at=_now(),
            ))
            await s.commit()
            def fake_bridge(agent_id):
                return object()  # bridge resolvable → worker selectable

            import acms.bridge as bridge_mod
            monkeypatch.setattr(bridge_mod, "get_bridge_for_agent", fake_bridge)

            from acms.dispatch_service import DispatchError
            import acms.dispatch_service as ds

            async def boom_dispatch(db_, **kw):
                raise DispatchError("bridge unreachable")

            monkeypatch.setattr(ds, "dispatch_work", boom_dispatch)

            counts = ReconcileCounts()
            result = await _intake_and_start(s, run, _issue(), counts)

            # dispatch raised → lock released, durably recorded.
            assert result["outcome"] == "queued_dispatch_error"
            assignments = (await s.scalars(select(AssignmentRecord))).all()
            assert len(assignments) == 1
            assert assignments[0].status == "RELEASED"
            assert assignments[0].closed_at is not None

    asyncio.run(scenario())


# --------------------------------------------------------------- case 8

def test_jira_start_writeback_failure_does_not_break_started_dispatch(db, monkeypatch, gate_env):
    async def scenario():
        async with SessionLocal() as s:
            s.add(_project())
            run = _run()
            s.add(run)
            await s.commit()

            from acms.models import AgentRecord
            s.add(AgentRecord(
                agent_id="agent-jira-fail", display_name="worker-y",
                external_registration_id="arm-worker-y", harness="hermes",
                worker_uid="002", trust_class="internal",
                bridge_version="0.19.0", protocol_version="1",
                created_at=_now(), updated_at=_now(),
            ))
            await s.commit()
            from acms.telemetry_models import AgentStatusCurrentRecord
            s.add(AgentStatusCurrentRecord(
                agent_id="agent-jira-fail", connectivity="HEALTHY",
                agent_running="running", last_contact_at=_now(),
                updated_at=_now(),
            ))
            await s.commit()

            import acms.bridge as bridge_mod
            monkeypatch.setattr(bridge_mod, "get_bridge_for_agent", lambda aid: object())

            captured = {}

            async def fake_dispatch(db_, *, work_item_id, instruction, agent_id,
                                    idempotency_key, actor):
                captured["work_item_id"] = work_item_id
                captured["idempotency_key"] = idempotency_key
                return {"dispatched": True, "task_id": "task-9",
                        "external_task_id": "run-9"}

            import acms.dispatch_service as ds
            monkeypatch.setattr(ds, "dispatch_work", fake_dispatch)

            class BoomJira:
                status_mutation_enabled = False

                def add_comment(self, key, body):
                    raise RuntimeError("jira down")

                def transition_status(self, key, status):
                    raise RuntimeError("jira down")

            import acms.jira_client as jc
            import acms.jira_reconcile as jr
            monkeypatch.setattr(jc, "JiraClient", BoomJira)
            monkeypatch.setattr(jr, "JiraClient", BoomJira)

            counts = ReconcileCounts()
            result = await _intake_and_start(s, run, _issue(), counts)
            assert result["outcome"] == "started"
            assert counts.started == 1
            # dispatch ACK still durable: assignment bound to the run.
            assignment = (await s.scalars(select(AssignmentRecord))).one()
            assert assignment.status == "ACTIVE"
            assert assignment.bound_session_id == "run-9"
            assert assignment.dispatched_at is not None
            # outbound failure is durable
            fails = (await s.scalars(select(AgentEventRecord).where(
                AgentEventRecord.event_type == "JIRA_OUTBOUND_FAILED"))).all()
            assert len(fails) == 1
            assert fails[0].metadata_json and "jira down" in fails[0].metadata_json
            # generation recorded so re-polled identical ready snapshots don't restart
            link = (await s.scalars(select(JiraIssueLinkRecord))).one()
            assert link.generation_count == 1
            assert link.last_generation_started_at is not None

    asyncio.run(scenario())
