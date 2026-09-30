"""Jira reconciliation API (window-5 plan §13.5) + kickoff-linkage endpoints.

- POST /api/v1/jira/reconcile        — "Check Jira now" (execution-capable;
  a page refresh/GET never starts work). Returns a durable run id.
- GET  /api/v1/jira/reconcile/runs   — run ledger (counts, states, errors).
- GET  /api/v1/jira/reconcile/runs/{run_id}
- GET  /api/v1/jira/schedule         — scheduler freshness (last attempt /
  last success / next due / interval; §13.6 display facts).
- POST /api/v1/work/{id}/jira/link   — link an existing Jira issue (admin).
- GET  /api/v1/work/{id}/jira        — linkage + gate verdict for one work item
  (read-only observation — safe for UI detail views).
- POST /api/v1/work/{id}/holds       — persistent local pause/stop (§13.3).
- DELETE /api/v1/work/{id}/holds/{hold_id} — explicit operator clear.

Machine routes are bearer-gated (require_admin_token) like every other
admin-capable mutation; UI actions go through the UI layer with CSRF.
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .db import get_session
from .jira_client import JiraClient, JiraError
from .jira_gate import gate_work_item
from .jira_models import (JiraIssueLinkRecord, JiraIssueObservationRecord,
                          WorkRuntimeHoldRecord, new_id)
from .jira_reconcile import (ReconcileOutcome, due_for_scheduled_poll,
                             start_reconciliation_run)
from .security import require_admin_token
from .telemetry_service import add_event
from .work_models import WorkItemRecord

router = APIRouter(prefix="/api/v1/jira")


class ReconcileRequest(BaseModel):
    actor: str | None = Field(default=None, max_length=128)


class ReconcileAccepted(BaseModel):
    run_id: str
    state: str
    coalesced: bool
    note: str = "reconciliation is execution-capable: eligible work may start/pause/stop as a result"


class LinkRequest(BaseModel):
    issue_key: str = Field(min_length=3, max_length=64)
    actor: str | None = Field(default=None, max_length=128)


class HoldRequest(BaseModel):
    hold_kind: str = Field(pattern="^(PAUSE|STOP)$")
    reason: str = Field(default="", max_length=512)
    actor: str | None = Field(default=None, max_length=128)


def _now() -> datetime:
    return datetime.now(timezone.utc)


@router.post("/reconcile", response_model=ReconcileAccepted,
             dependencies=[Depends(require_admin_token)])
async def check_jira_now(body: ReconcileRequest | None = None,
                         db: AsyncSession = Depends(get_session)) -> ReconcileAccepted:
    """Manual 'Check Jira now' — real mutation action (never a GET)."""
    actor = (body.actor if body else None) or "api"
    outcome: ReconcileOutcome = await start_reconciliation_run(
        db, trigger="manual_check", requested_by=actor)
    return ReconcileAccepted(run_id=outcome.run_id, state=outcome.state,
                             coalesced=outcome.coalesced)


@router.get("/reconcile/runs", dependencies=[Depends(require_admin_token)])
async def list_runs(limit: int = 25, db: AsyncSession = Depends(get_session)):
    from .jira_models import JiraReconciliationRunRecord as R

    rows = (await db.scalars(
        select(R).order_by(R.created_at.desc()).limit(min(max(limit, 1), 100))
    )).all()
    return {"runs": [
        {"run_id": r.run_id, "trigger": r.trigger, "requested_by": r.requested_by,
         "state": r.state, "started_at": _iso(r.started_at), "finished_at": _iso(r.finished_at),
         "scan_completeness": r.scan_completeness,
         "counts": {k: getattr(r, f"{k}_count") for k in (
             "examined", "added", "started", "resumed", "paused",
             "paused_acknowledged", "stopped", "stopped_acknowledged",
             "unchanged", "failed")},
         "error_summary": r.error_summary}
        for r in rows
    ]}


@router.get("/reconcile/runs/{run_id}", dependencies=[Depends(require_admin_token)])
async def get_run(run_id: str, db: AsyncSession = Depends(get_session)):
    from .jira_models import JiraReconciliationRunRecord as R

    r = await db.get(R, run_id)
    if r is None:
        raise HTTPException(status_code=404, detail="reconciliation run not found")
    return {"run_id": r.run_id, "trigger": r.trigger, "requested_by": r.requested_by,
            "state": r.state, "started_at": _iso(r.started_at), "finished_at": _iso(r.finished_at),
            "scan_completeness": r.scan_completeness,
            "counts": {k: getattr(r, f"{k}_count") for k in (
                "examined", "added", "started", "resumed", "paused",
                "paused_acknowledged", "stopped", "stopped_acknowledged",
                "unchanged", "failed")},
            "error_summary": r.error_summary}


@router.get("/schedule", dependencies=[Depends(require_admin_token)])
async def schedule_status(db: AsyncSession = Depends(get_session)):
    """Scheduler freshness display (§13.6) — operator-visible, honest."""
    due, info = await due_for_scheduled_poll(db)
    return {"due_now": due, **info}


# ------------------------------------------------------------------ linkage

@router.post("/work/{work_item_id}/link", dependencies=[Depends(require_admin_token)])
async def link_issue(work_item_id: str, body: LinkRequest,
                     db: AsyncSession = Depends(get_session)):
    """Link an EXISTING Jira issue to a work item (§7.2 'Link existing').

    Creating/linking does NOT authorize execution — the human must set the
    ready state + AI-account assignment in Jira; the gate observes it.
    """
    work = await db.get(WorkItemRecord, work_item_id)
    if work is None:
        raise HTTPException(status_code=404, detail="work item not found")
    client = JiraClient()
    try:
        issue = client.get_issue(body.issue_key)
    except JiraError as e:
        raise HTTPException(status_code=502, detail=f"Jira read failed: {str(e)[:200]}") from None

    existing = (await db.scalars(
        select(JiraIssueLinkRecord).where(JiraIssueLinkRecord.jira_issue_id == issue.issue_id)
    )).first()
    if existing is not None and existing.work_item_id != work_item_id:
        raise HTTPException(status_code=409, detail={
            "code": "issue-already-linked",
            "linked_work_item_id": existing.work_item_id,
            "note": "one intake issue may authorize multiple descendants via hierarchy; "
                    "link the DESCENDANT to the parent-linked item or link the parent",
        })
    if existing is None:
        db.add(JiraIssueLinkRecord(
            link_id=new_id(), jira_site_id="default",
            jira_issue_id=issue.issue_id or "", jira_issue_key=issue.key,
            jira_url=issue.url, work_item_id=work_item_id,
            created_by=body.actor or "api",
        ))
    # persist linkage on the item itself (display + descendant inheritance)
    work.jira_site_id = "default"
    work.jira_issue_id = issue.issue_id
    work.jira_issue_key = issue.key
    work.jira_url = issue.url
    await add_event(db, event_type="JIRA_LINKED", actor_source="api",
                    work_key=work.work_key,
                    summary=f"Jira {issue.key} linked to {work.work_key or work_item_id[:8]} "
                            f"(status={issue.status}; linkage != execution authorization)",
                    metadata={"issue_key": issue.key, "issue_id": issue.issue_id,
                              "observed_status": issue.status})
    await db.commit()
    gate = await gate_work_item(db, work_item_id, live_check=True)
    return {"linked": True, "issue_key": issue.key, "issue_url": issue.url,
            "gate": gate.to_dict()}


@router.get("/work/{work_item_id}/jira", dependencies=[Depends(require_admin_token)])
async def work_jira_view(work_item_id: str, db: AsyncSession = Depends(get_session)):
    work = await db.get(WorkItemRecord, work_item_id)
    if work is None:
        raise HTTPException(status_code=404, detail="work item not found")
    gate = await gate_work_item(db, work_item_id, live_check=False)
    holds = (await db.scalars(
        select(WorkRuntimeHoldRecord)
        .where(WorkRuntimeHoldRecord.work_item_id == work_item_id)
        .where(WorkRuntimeHoldRecord.cleared_at.is_(None))
    )).all()
    return {
        "work_item_id": work_item_id,
        "linkage": {"issue_key": work.jira_issue_key, "issue_id": work.jira_issue_id,
                    "url": work.jira_url, "site_id": work.jira_site_id},
        "last_observation": {"status": work.jira_last_status,
                             "assignee_account_id": work.jira_last_assignee_account_id,
                             "checked_at": _iso(work.jira_last_checked_at)},
        "gate": gate.to_dict(),
        "holds": [{"hold_id": h.hold_id, "kind": h.hold_kind, "reason": h.reason,
                   "created_by": h.created_by, "created_at": _iso(h.created_at),
                   "runtime_acknowledged": h.runtime_acknowledged} for h in holds],
    }


# ------------------------------------------------------------------ holds

@router.post("/work/{work_item_id}/holds", dependencies=[Depends(require_admin_token)])
async def create_hold(work_item_id: str, body: HoldRequest,
                      db: AsyncSession = Depends(get_session)):
    """Persistent local PAUSE/STOP (§13.3) — survives restarts and polls."""
    work = await db.get(WorkItemRecord, work_item_id)
    if work is None:
        raise HTTPException(status_code=404, detail="work item not found")
    hold = WorkRuntimeHoldRecord(
        hold_id=new_id(), work_item_id=work_item_id, hold_kind=body.hold_kind,
        reason=body.reason[:512], created_by=body.actor or "api", source="api",
    )
    db.add(hold)
    await add_event(db, event_type=f"LOCAL_{body.hold_kind}_HOLD_SET", actor_source="api",
                    work_key=work.work_key,
                    summary=f"local {body.hold_kind} hold set on {work.work_key or work_item_id[:8]}: "
                            f"{body.reason[:200]}",
                    metadata={"hold_id": hold.hold_id, "hold_kind": body.hold_kind})
    await db.commit()
    return {"hold_id": hold.hold_id, "hold_kind": hold.hold_kind,
            "note": "polls cannot override this hold; an operator must clear it explicitly"}


@router.delete("/work/{work_item_id}/holds/{hold_id}",
               dependencies=[Depends(require_admin_token)])
async def clear_hold(work_item_id: str, hold_id: str,
                     db: AsyncSession = Depends(get_session)):
    hold = await db.get(WorkRuntimeHoldRecord, hold_id)
    if hold is None or hold.work_item_id != work_item_id:
        raise HTTPException(status_code=404, detail="hold not found")
    hold.cleared_at = _now()
    hold.cleared_by = "api"
    await add_event(db, event_type="LOCAL_HOLD_CLEARED", actor_source="api",
                    summary=f"hold {hold.hold_kind} cleared on {work_item_id[:8]} — "
                            f"normal Jira resume gate applies",
                    metadata={"hold_id": hold_id, "hold_kind": hold.hold_kind})
    await db.commit()
    return {"cleared": True, "hold_id": hold_id}


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None