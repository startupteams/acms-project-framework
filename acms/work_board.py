"""Work board UI data (window-5 plan §6) — operator-first board over existing
ACMS concepts; the backend domain model is NOT replaced.

Board columns (§6.3): PLANNED / ACTIVE / IN REVIEW / COMPLETED / BLOCKED.
Cards show real, acted-on facts only (§6.2 anti-vanity rule):
  Work ID · title · parent · kind · Jira linkage+eligibility badge ·
  assignment state (active worker name or none) · PR count by outcome state ·
  budget state · last update.

PR/review visibility (§6.5) uses the economics pr_outcomes rows — MERGED is
never called accepted; the outcome_state vocabulary renders verbatim
(CI_VERIFIED / MERGED / ACCEPTED / OPEN / FAILED...).
"""
from __future__ import annotations

from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from .budget_service import get_budget
from .economics_models import PrOutcomeRecord
from .work_models import AssignmentRecord, WorkItemRecord

BOARD_COLUMNS = [
    ("planned", "Planned"),
    ("active", "Active"),
    ("in_review", "In Review"),
    ("blocked", "Blocked"),
    ("completed", "Completed"),
]


async def board_data(db: AsyncSession) -> dict:
    items = (await db.scalars(
        select(WorkItemRecord).order_by(WorkItemRecord.updated_at.desc()).limit(200)
    )).all()

    # assignments (active worker per item)
    assignments = (await db.scalars(
        select(AssignmentRecord).where(AssignmentRecord.status == "ACTIVE")
    )).all()
    active_worker: dict[str, str] = {}
    for a in assignments:
        active_worker.setdefault(a.work_item_id, a.agent_id[:8])

    # PR outcomes per work item (economics data — objective states only)
    pr_rows = (await db.scalars(
        select(PrOutcomeRecord.work_item_id, PrOutcomeRecord.outcome_state,
               PrOutcomeRecord.pr_url, PrOutcomeRecord.pr_number)
        .where(PrOutcomeRecord.work_item_id.isnot(None))
    )).all()
    pr_by_item: dict[str, list[dict]] = {}
    for wid, state, url, num in pr_rows:
        pr_by_item.setdefault(wid, []).append(
            {"state": state, "url": url, "pr_number": num})

    # budget states
    budget_state: dict[str, str] = {}
    for it in items:
        b = await get_budget(db, it.work_item_id)
        if b is not None:
            budget_state[it.work_item_id] = b.budget_state

    columns: dict[str, list[dict]] = {c[0]: [] for c in BOARD_COLUMNS}
    for it in items:
        wid = it.work_item_id
        prs = pr_by_item.get(wid, [])
        columns.setdefault(it.status, []).append({
            "work_item_id": wid,
            "work_key": it.work_key,
            "title": it.title,
            "kind": it.kind,
            "worker": active_worker.get(wid),
            "pr_count": len(prs),
            "pr_accepted": sum(1 for p in prs if p["state"] == "ACCEPTED"),
            "pr_merged": sum(1 for p in prs if p["state"] == "MERGED"),
            "budget_state": budget_state.get(wid),
            "jira_issue_key": it.jira_issue_key,
            "jira_url": it.jira_url,
            "jira_eligibility": it.jira_eligibility,
            "updated_at": it.updated_at,
        })

    return {
        "columns": [
            {"status": key, "label": label, "cards": columns.get(key, [])}
            for key, label in BOARD_COLUMNS
        ],
        "total": len(items),
    }


async def work_prs(db: AsyncSession, work_item_id: str) -> list[dict]:
    """PR/review visibility rows for one work item (§6.5)."""
    rows = (await db.scalars(
        select(PrOutcomeRecord)
        .where(PrOutcomeRecord.work_item_id == work_item_id)
        .order_by(PrOutcomeRecord.outcome_id.desc())
    )).all()
    return [
        {"pr_number": r.pr_number, "url": r.pr_url, "repository": r.repository,
         "branch": r.branch, "outcome_state": r.outcome_state,
         "accepted_at": r.accepted_at, "accepted_by": r.accepted_by,
         "model_id": r.model_id}
        for r in rows
    ]