import asyncio
from datetime import datetime, timezone

from sqlalchemy import func, select

from acms.a2a_models import ProjectRecord
from acms.db import SessionLocal
from acms.jira_client import JiraIssue
from acms.jira_models import JiraIssueLinkRecord, JiraReconciliationRunRecord
from acms.jira_reconcile import ReconcileCounts, _intake_and_start
from acms.work_models import WorkItemRecord


def test_ready_jira_issue_is_imported_once_with_project_and_started(monkeypatch, clean_db):
    async def scenario():
        async with SessionLocal() as db:
            now = datetime.now(timezone.utc)
            project = ProjectRecord(
                project_id=ProjectRecord.new_id(), name="ACMS", slug="acms",
                jira_project_key="STNA", repos="startupteams/acms-project-framework",
                created_at=now, updated_at=now,
            )
            run = JiraReconciliationRunRecord(
                run_id="run-jira-intake", trigger="manual_check",
                requested_by="test", state="running",
            )
            db.add_all([project, run])
            await db.commit()

            async def fake_assign_dispatch(db_, run_, issue_, work_, link_, counts_):
                counts_.started += 1
                return {"outcome": "started", "reason": "test dispatch ACK",
                        "work_item_id": work_.work_item_id,
                        "agent_id": "agent-1", "task_id": "task-1"}

            import acms.jira_reconcile as jr
            monkeypatch.setattr(jr, "_assign_and_dispatch", fake_assign_dispatch)
            issue = JiraIssue(
                key="STNA-88", issue_id="10088", summary="Change ACMS version",
                description="Version #: 0.12.0", status="TO START",
                assignee="Startup Teams", assignee_account_id="acct-ai",
                priority="Medium", labels=[], url="https://example.atlassian.net/browse/STNA-88",
                project_key="STNA", issue_type="Task",
            )
            counts = ReconcileCounts()
            result = await _intake_and_start(db, run, issue, counts)
            assert result["outcome"] == "started"
            assert counts.added == 1
            assert counts.started == 1

            work = (await db.scalars(
                select(WorkItemRecord).where(WorkItemRecord.jira_issue_key == "STNA-88")
            )).one()
            assert work.project_id == project.project_id
            assert work.work_uid and work.work_uid.startswith("ACMS-WORK-")
            assert "Version #: 0.12.0" in work.scope_markdown
            link = (await db.scalars(
                select(JiraIssueLinkRecord).where(JiraIssueLinkRecord.jira_issue_id == "10088")
            )).one()
            assert link.work_item_id == work.work_item_id

            duplicate = await _intake_and_start(db, run, issue, ReconcileCounts())
            assert duplicate["outcome"] == "already_linked"
            assert (await db.scalar(select(func.count()).select_from(WorkItemRecord))) == 1
            assert (await db.scalar(select(func.count()).select_from(JiraIssueLinkRecord))) == 1

    asyncio.run(scenario())
