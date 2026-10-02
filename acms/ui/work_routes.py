"""Work Management UI (feature-delivery plan §13; ACMS-REQ-007/008/013/014/023).

Read pages are open to every mapped role (Administrator, Worker, Observer).
All mutations are Administrator-only for this slice (plan §13: workers get
read-permitted work first; privileged actions arrive with the audit backend).

Assignment honesty (plan §13): until A2A delivery exists (slice 4), every
assignment is shown as RECORDED only — the UI never implies that a database
assignment reached the agent.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_session
from ..models import AgentResponse
from ..registry import list_agents
from ..work_models import (
    AssignmentCreate,
    HandoffCreate,
    WorkItemCreate,
    WorkItemKind,
    WorkItemStatus,
    WorkItemUpdate,
)
from .. import budget_service
from .. import work_service
from .roles import Role
from .session_auth import current_user

router = APIRouter(prefix="/ui/work", include_in_schema=False)

# Plan §13: recorded ≠ delivered ≠ accepted. A2A delivery is slice 4.
DELIVERY_STATE = "Recorded assignment only — delivery to the agent (A2A) is not implemented yet"

_STATUSES = [s.value for s in WorkItemStatus]
_KINDS = [k.value for k in WorkItemKind]
_CLOSE_STATUSES = ("COMPLETED", "RELEASED", "SUSPENDED")

# Canonical-handoff artifact types (§9 + legacy "handoff" rows honored)
HANDOFF_TYPES = ("work_handoff", "handoff")


def _require_admin(user) -> None:
    """Mutations are Administrator-only in this slice (plan §13)."""
    try:
        role = Role(user.role)
    except ValueError:
        role = None
    if role is not Role.ADMINISTRATOR:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Administrator role required")


def _badge(kind: str) -> str:
    return f"badge badge-{kind}"


async def _work_prs(db, work_item_id: str) -> list[dict]:
    """Linked PRs (economics pr_outcomes) — MERGED never shown as accepted."""
    from ..work_board import work_prs

    return await work_prs(db, work_item_id)


async def _active_holds(db, work_item_id: str) -> list:
    """Un-cleared local holds (window-5 §13.3) for the detail page."""
    from sqlalchemy import select

    from ..jira_models import WorkRuntimeHoldRecord

    return list((await db.scalars(
        select(WorkRuntimeHoldRecord)
        .where(WorkRuntimeHoldRecord.work_item_id == work_item_id)
        .where(WorkRuntimeHoldRecord.cleared_at.is_(None))
        .order_by(WorkRuntimeHoldRecord.created_at.desc())
    )).all())


def _view_item(record, parent_title: str | None = None) -> dict:
    return {
        "work_item_id": record.work_item_id,
        # STEA-004 §6: human UID as primary link text, short key alongside
        "work_uid": getattr(record, "work_uid", None),
        "work_key": getattr(record, "work_key", None),
        "parent_id": record.parent_id,
        "parent_title": parent_title,
        "kind": record.kind,
        "title": record.title,
        "created_by": record.created_by or "—",
        "status": record.status,
        "scope_markdown": record.scope_markdown,
        "created_at": record.created_at,
        "updated_at": record.updated_at,
        # window-5 §7: Jira kickoff linkage + eligibility observation
        "jira_issue_key": getattr(record, "jira_issue_key", None),
        "jira_issue_id": getattr(record, "jira_issue_id", None),
        "jira_url": getattr(record, "jira_url", None),
        "jira_last_status": getattr(record, "jira_last_status", None),
        "jira_last_assignee_account_id": getattr(record, "jira_last_assignee_account_id", None),
        "jira_last_checked_at": getattr(record, "jira_last_checked_at", None),
        "jira_eligibility": getattr(record, "jira_eligibility", None),
        "jira_eligibility_reason": getattr(record, "jira_eligibility_reason", None),
    }


async def _parent_map(db: AsyncSession) -> dict[str, str]:
    items = await work_service.list_work_items(db)
    return {i.work_item_id: i.title for i in items}


@router.get("")
async def work_list(
    request: Request,
    status_filter: str | None = None,
    kind_filter: str | None = None,
    user=Depends(current_user),
    db: AsyncSession = Depends(get_session),
):
    items = await work_service.list_work_items(db)
    parents = {i.work_item_id: i.title for i in items}
    if status_filter in _STATUSES:
        items = [i for i in items if i.status == status_filter]
    if kind_filter in _KINDS:
        items = [i for i in items if i.kind == kind_filter]
    # Newest first for operator ergonomics.
    items = sorted(items, key=lambda i: i.updated_at, reverse=True)
    context = {
        "user": user,
        "items": [_view_item(i, parents.get(i.parent_id)) for i in items],
        "statuses": _STATUSES,
        "kinds": _KINDS,
        "status_filter": status_filter or "",
        "kind_filter": kind_filter or "",
        "error": request.query_params.get("error"),
    }
    return _templates().TemplateResponse(request, "work_list.html", context)


@router.get("/board")
async def work_board(
    request: Request,
    user=Depends(current_user),
    db: AsyncSession = Depends(get_session),
):
    """Operator kanban (STEA-004 Phase C §12) — six columns incl. dispatching/
    running/attention; UID as primary link text; Jira state separate."""
    from .operator_data import kanban_data

    data = await kanban_data(db)
    context = {
        "user": user,
        "columns": data["columns"],
        "total": data["total"],
        "error": request.query_params.get("error"),
    }
    return _templates().TemplateResponse(request, "work_board.html", context)


@router.get("/new")
async def work_new_form(
    request: Request,
    user=Depends(current_user),
    db: AsyncSession = Depends(get_session),
):
    _require_admin(user)
    items = await work_service.list_work_items(db)
    context = {
        "user": user,
        "item": None,
        "parents": [{"work_item_id": i.work_item_id, "title": i.title} for i in items],
        "kinds": _KINDS,
        "statuses": _STATUSES,
        "action": "/ui/work/new",
        "error": request.query_params.get("error"),
    }
    return _templates().TemplateResponse(request, "work_form.html", context)


@router.post("/new")
async def work_create(
    request: Request,
    kind: str = Form(...),
    parent_id: str = Form(""),
    title: str = Form(...),
    scope_markdown: str = Form(""),
    user=Depends(current_user),
    db: AsyncSession = Depends(get_session),
):
    _require_admin(user)
    if kind not in _KINDS or not title.strip():
        return RedirectWithError("/ui/work/new", "invalid kind or empty title")
    payload = WorkItemCreate(
        kind=WorkItemKind(kind),
        parent_id=parent_id or None,
        title=title.strip(),
        # Provenance (ACMS-REQ-010/011): the acting human is recorded.
        created_by=user.username,
        scope_markdown=scope_markdown,
    )
    try:
        record = await work_service.create_work_item(db, payload)
    except ValueError as exc:
        return RedirectWithError("/ui/work/new", str(exc))
    return RedirectSeeOther(f"/ui/work/{record.work_item_id}")


@router.get("/{work_item_id}")
async def work_detail(
    request: Request,
    work_item_id: str,
    user=Depends(current_user),
    db: AsyncSession = Depends(get_session),
):
    record = await work_service.get_work_item(db, work_item_id)
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="work item not found")
    parents = await _parent_map(db)
    parent_title = parents.get(record.parent_id) if record.parent_id else None

    assignments = await work_service.list_assignments(db, work_item_id=work_item_id)
    # Resolve display names for every assignment's agent (not just the active one).
    agent_ids = {a.agent_id for a in assignments}
    agents_by_id = {}
    if agent_ids:
        agents = await list_agents(db)
        agents_by_id = {a.agent_id: a for a in agents}
    active_assignment = next((a for a in assignments if a.status == "ACTIVE"), None)
    assigned_agent = agents_by_id.get(active_assignment.agent_id) if active_assignment else None
    routines = []
    if active_assignment is not None:
        routines = await work_service.list_routines(db, active_assignment.agent_id)

    children = await work_service.list_children(db, work_item_id)
    tasks = await work_service.list_execution_tasks(db, work_item_id)
    handoffs = await work_service.list_handoffs(db, work_item_id)

    # Canonical handoff (STEA-004 §9): exactly ONE work_handoff artifact per
    # terminal item — surfaced prominently on this page (§9 UI requirement).
    # Legacy rows typed "handoff" (pre-§9 naming) are honored as canonical too.
    canonical_handoff = None
    from ..a2a_models import ArtifactRecord

    from sqlalchemy import select as _sel

    ch = (await db.scalars(
        _sel(ArtifactRecord)
        .where(ArtifactRecord.work_item_id == work_item_id)
        .where(ArtifactRecord.artifact_type.in_(HANDOFF_TYPES))
        .order_by(ArtifactRecord.created_at.desc()).limit(1))).first()
    if ch is not None:
        canonical_handoff = {
            "artifact_id": ch.artifact_id,
            "artifact_uid": ch.artifact_uid,
            "title": ch.title,
            "bluf": ch.bluf,
            "created_str": ch.created_at.strftime("%Y-%m-%d %H:%M") if ch.created_at else "—",
            "content": ch.content,
        }

    # Budget panel (REQ-059): rollup + budget row (may be unset — show honestly).
    budget = await budget_service.get_budget(db, work_item_id)
    rollup = await budget_service.work_budget_rollup(db, work_item_id)

    # Assignable agents: any registered agent with no ACTIVE assignment.
    all_agents = await list_agents(db)
    busy_ids = {
        a.agent_id
        for a in await work_service.list_assignments(db, status="ACTIVE")
    }
    available_agents = [a for a in all_agents if a.agent_id not in busy_ids]

    context = {
        "user": user,
        "item": _view_item(record, parent_title),
        "children": [_view_item(c) for c in children],
        "assignments": [
            {
                "assignment_id": a.assignment_id,
                "agent_id": a.agent_id,
                "agent_name": getattr(agents_by_id.get(a.agent_id), "display_name", None) or a.agent_id[:8],
                "status": a.status,
                "assigned_by": a.assigned_by or "—",
                "assigned_at": a.assigned_at,
                "closed_at": a.closed_at,
            }
            for a in assignments
        ],
        "active_assignment": active_assignment,
        "assigned_agent": assigned_agent,
        "delivery_state": DELIVERY_STATE,
        "tasks": tasks,
        "handoffs": handoffs,
        "routines": routines,
        "available_agents": available_agents,
        "statuses_default": _STATUSES,
        "close_statuses": _CLOSE_STATUSES,
        "budget": budget,
        "rollup": rollup,
        # window-5 §13.3: persistent local holds (pause/stop) for this item
        "holds": (await _active_holds(db, work_item_id)),
        # window-5 §6.5: linked PRs with objective outcome states
        "prs": (await _work_prs(db, work_item_id)),
        # STEA-004 §9: canonical Markdown handoff surfaced prominently
        "canonical_handoff": canonical_handoff,
        "error": request.query_params.get("error"),
    }
    return _templates().TemplateResponse(request, "work_detail.html", context)


@router.post("/{work_item_id}/edit")
async def work_edit(
    request: Request,
    work_item_id: str,
    title: str = Form(...),
    status_value: str = Form("status"),
    scope_markdown: str = Form(""),
    user=Depends(current_user),
    db: AsyncSession = Depends(get_session),
):
    _require_admin(user)
    if status_value not in _STATUSES:
        return RedirectWithError(f"/ui/work/{work_item_id}", f"invalid status: {status_value}")
    payload = WorkItemUpdate(
        status=WorkItemStatus(status_value),
        title=title.strip() or None,
        scope_markdown=scope_markdown,
    )
    record = await work_service.update_work_item(db, work_item_id, payload)
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="work item not found")
    return RedirectSeeOther(f"/ui/work/{work_item_id}")


@router.post("/{work_item_id}/assign")
async def work_assign(
    request: Request,
    work_item_id: str,
    agent_id: str = Form(...),
    user=Depends(current_user),
    db: AsyncSession = Depends(get_session),
):
    _require_admin(user)
    record, error = await work_service.assign_primary(
        db, AssignmentCreate(agent_id=agent_id, work_item_id=work_item_id), assigned_by=user.username
    )
    if record is None:
        code = error or "assignment-failed"
        return RedirectWithError(f"/ui/work/{work_item_id}", f"assignment refused: {code}")
    return RedirectSeeOther(f"/ui/work/{work_item_id}")


@router.post("/assignments/{assignment_id}/close")
async def work_close_assignment(
    request: Request,
    assignment_id: str,
    new_status: str = Form(...),
    work_item_id: str = Form(...),
    user=Depends(current_user),
    db: AsyncSession = Depends(get_session),
):
    _require_admin(user)
    try:
        record = await work_service.close_assignment(db, assignment_id, new_status)
    except ValueError as exc:
        return RedirectWithError(f"/ui/work/{work_item_id}", str(exc))
    if record is None:
        return RedirectWithError(f"/ui/work/{work_item_id}", "active assignment not found")
    return RedirectSeeOther(f"/ui/work/{work_item_id}")


@router.post("/{work_item_id}/handoffs")
async def work_create_handoff(
    request: Request,
    work_item_id: str,
    title: str = Form(...),
    body_markdown: str = Form(...),
    user=Depends(current_user),
    db: AsyncSession = Depends(get_session),
):
    _require_admin(user)
    record = await work_service.create_handoff(
        db,
        HandoffCreate(work_item_id=work_item_id, title=title.strip(), body_markdown=body_markdown),
    )
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="work item not found")
    return RedirectSeeOther(f"/ui/work/{work_item_id}")


@router.post("/{work_item_id}/budget")
async def work_set_budget(
    request: Request,
    work_item_id: str,
    soft_budget_usd: str = Form(""),
    hard_budget_usd: str = Form(""),
    user=Depends(current_user),
    db: AsyncSession = Depends(get_session),
):
    """Set/update the Work budget (REQ-059). Administrator-only."""
    _require_admin(user)

    def _parse(raw: str) -> float | None:
        raw = (raw or "").strip()
        if raw == "":
            return None
        val = float(raw)
        if val < 0:
            raise ValueError("negative budget")
        return val

    try:
        soft = _parse(soft_budget_usd)
        hard = _parse(hard_budget_usd)
        if soft is not None and hard is not None and soft > hard:
            raise ValueError("soft exceeds hard")
        await budget_service.set_budget(db, work_item_id, soft_budget_usd=soft,
                                        hard_budget_usd=hard, actor=user.username)
    except ValueError as exc:
        return RedirectWithError(f"/ui/work/{work_item_id}", f"invalid budget: {exc}")
    return RedirectSeeOther(f"/ui/work/{work_item_id}")


@router.post("/{work_item_id}/budget/override")
async def work_override_budget(
    request: Request,
    work_item_id: str,
    override_reason: str = Form(...),
    user=Depends(current_user),
    db: AsyncSession = Depends(get_session),
):
    """Audited one-shot hard-limit override (REQ-059). Administrator-only."""
    _require_admin(user)
    reason = override_reason.strip()
    if len(reason) < 4:
        return RedirectWithError(f"/ui/work/{work_item_id}", "override reason required")
    try:
        await budget_service.override_hard_limit(db, work_item_id,
                                                 override_by=user.username, reason=reason)
    except ValueError as exc:
        return RedirectWithError(f"/ui/work/{work_item_id}", str(exc))
    return RedirectSeeOther(f"/ui/work/{work_item_id}")


# ---------------------------------------------------------------- helpers


def RedirectSeeOther(url: str):
    from fastapi.responses import RedirectResponse

    return RedirectResponse(url=url, status_code=status.HTTP_303_SEE_OTHER)


def RedirectWithError(url: str, message: str):
    from urllib.parse import quote

    return RedirectSeeOther(f"{url}?error={quote(message)}")


def _templates():
    from .routes import templates

    return templates