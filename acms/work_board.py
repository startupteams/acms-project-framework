"""Work board UI data — SUPERSEDED by acms/ui/operator_data.kanban_data
(STEA-004 Phase C §12: six columns incl. dispatching/running/attention).

``work_prs`` (the §6.5 PR/review visibility rows) stays here and remains the
single source used by the Work detail page.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .economics_models import PrOutcomeRecord


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