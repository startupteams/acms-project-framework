import asyncio
from datetime import datetime, timezone

from sqlalchemy import select

from acms.canonical_handoff import generate_canonical_handoff
from acms.completion_callback import _finalize_jira_workflow
from acms.db import SessionLocal
from acms.telemetry_models import AgentEventRecord
from acms.work_models import AssignmentRecord, ExecutionTaskRecord, WorkItemCreate
from acms import work_service


def test_success_completion_posts_exact_artifact_url_and_moves_to_review(monkeypatch, clean_db):
    async def scenario():
        async with SessionLocal() as db:
            work = await work_service.create_work_item(
                db, WorkItemCreate(title="STNA-88 change version", created_by="test",
                                   scope_markdown="Version #: 0.12.0"))
            work.jira_issue_key = "STNA-88"
            work.jira_url = "https://example.atlassian.net/browse/STNA-88"
            assignment = AssignmentRecord(
                assignment_id="asg-88", assignment_key="ACMS-ASG-000088",
                agent_id="agent-88", work_item_id=work.work_item_id,
                status="ACTIVE", assigned_by="test", assigned_at=datetime.now(timezone.utc),
            )
            task = ExecutionTaskRecord(
                task_id="task-88", work_item_id=work.work_item_id,
                agent_id="agent-88", external_task_id="run-88",
                status="SUCCEEDED", started_at=datetime.now(timezone.utc),
                finished_at=datetime.now(timezone.utc),
            )
            db.add_all([assignment, task])
            await db.commit()
            handoff = await generate_canonical_handoff(
                db, task=task, status="SUCCEEDED", completed_at=datetime.now(timezone.utc))
            assert handoff and handoff["artifact_uid"].startswith("ACMS-ARTIFACT-")

            writes = {"comments": [], "transitions": []}

            class FakeJira:
                status_mutation_enabled = True

                def add_comment(self, key, body):
                    writes["comments"].append((key, body))

                def transition_status(self, key, status):
                    writes["transitions"].append((key, status))
                    return {"result": "transitioned"}

            import acms.jira_client as jc
            monkeypatch.setattr(jc, "JiraClient", FakeJira)
            from acms.settings import get_settings
            monkeypatch.setattr(get_settings(), "callback_base_url", "https://acms.example")

            await _finalize_jira_workflow(
                db, task=task, terminal_status="SUCCEEDED",
                handoff=handoff, error_summary=None)
            await db.commit()
            await db.refresh(work)
            await db.refresh(assignment)
            assert work.status == "in_review"
            assert assignment.status == "COMPLETED" and assignment.closed_at is not None
            assert writes["transitions"] == [("STNA-88", "IN REVIEW")]
            assert len(writes["comments"]) == 1
            assert handoff["artifact_uid"] in writes["comments"][0][1]
            assert f"https://acms.example/ui/artifacts/{handoff['artifact_uid']}" in writes["comments"][0][1]
            events = (await db.scalars(select(AgentEventRecord).where(
                AgentEventRecord.event_type == "JIRA_HANDOFF_POSTED"))).all()
            assert len(events) == 1

            # Duplicate completion signal must not duplicate Jira writes.
            await _finalize_jira_workflow(
                db, task=task, terminal_status="SUCCEEDED",
                handoff=handoff, error_summary=None)
            assert len(writes["comments"]) == 1

    asyncio.run(scenario())
