"""Work-scoped Jira UI actions (window-5 plan §13.5).

- "Check Jira now" — real authenticated POST mutation (never a GET); shows the
  durable run id + per-outcome counts; duplicate clicks coalesce server-side.
- Persistent Pause/Stop controls on work detail (local holds survive polls).
- Jira linkage panel on work detail (issue key/url, last observed status,
  assignee, eligibility verdict + reason, last checked).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_session
from ..jira_api import (HoldRequest, LinkRequest, ReconcileRequest,
                        check_jira_now, clear_hold, create_hold, link_issue)
from ..jira_gate import gate_work_item
from ..jira_reconcile import due_for_scheduled_poll
from ..work_models import WorkItemRecord
from .roles import Role
from .session_auth import current_user
from .work_routes import RedirectSeeOther, _require_admin

router = APIRouter(prefix="/ui/jira", include_in_schema=False)


@router.post("/check-now")
async def ui_check_jira_now(
    request: Request,
    user=Depends(current_user),
    db: AsyncSession = Depends(get_session),
):
    """Check Jira now (execution-capable action — stated in the UI).

    Runs the ONE reconciliation service immediately: imports/starts eligible
    work, requests pauses/stops per current Jira state. Returns to the origin
    with the durable run id in the redirect.
    """
    _require_admin(user)
    body = ReconcileRequest(actor=f"ui:{user.username}")
    outcome = await check_jira_now(body, db)
    return RedirectSeeOther(
        f"/ui/work?jira_run={outcome.run_id}&jira_state={outcome.state}"
        + ("&jira_coalesced=1" if outcome.coalesced else "")
    )


@router.post("/work/{work_item_id}/link")
async def ui_link_jira(
    work_item_id: str,
    request: Request,
    issue_key: str = Form(..., min_length=3, max_length=64),
    user=Depends(current_user),
    db: AsyncSession = Depends(get_session),
):
    _require_admin(user)
    body = LinkRequest(issue_key=issue_key.strip(), actor=f"ui:{user.username}")
    try:
        result = await link_issue(work_item_id, body, db)
        err = None
    except Exception as e:  # human-readable failure per plan §14
        result = None
        err = str(getattr(e, "detail", None) or e)[:300]
    target = f"/ui/work/{work_item_id}"
    if err:
        from .work_routes import RedirectWithError

        return RedirectWithError(target, f"Jira link failed: {err}")
    gate = result["gate"]
    return RedirectSeeOther(f"{target}?jira_gate={gate['verdict']}")


@router.post("/work/{work_item_id}/hold")
async def ui_set_hold(
    work_item_id: str,
    request: Request,
    hold_kind: str = Form(..., pattern="^(PAUSE|STOP)$"),
    reason: str = Form("", max_length=512),
    user=Depends(current_user),
    db: AsyncSession = Depends(get_session),
):
    _require_admin(user)
    body = HoldRequest(hold_kind=hold_kind, reason=reason, actor=f"ui:{user.username}")
    await create_hold(work_item_id, body, db)
    return RedirectSeeOther(f"/ui/work/{work_item_id}?hold_set={hold_kind}")


@router.post("/work/{work_item_id}/holds/{hold_id}/clear")
async def ui_clear_hold(
    work_item_id: str,
    hold_id: str,
    request: Request,
    user=Depends(current_user),
    db: AsyncSession = Depends(get_session),
):
    _require_admin(user)
    await clear_hold(work_item_id, hold_id, db)
    return RedirectSeeOther(f"/ui/work/{work_item_id}?hold_cleared=1")


@router.get("/status")
async def ui_jira_status(
    request: Request,
    user=Depends(current_user),
    db: AsyncSession = Depends(get_session),
):
    """Small JSON for the dashboard widget: scheduler freshness + gate summary."""
    due, info = await due_for_scheduled_poll(db)
    return {"due_now": due, **info}