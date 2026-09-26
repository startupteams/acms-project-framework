"""Agent Detail + Background Routines UI (feature-delivery plan §14; ACMS-REQ-005/008/014/023).

Read-only page: ``/ui/agents/{agent_id}`` shows persistent identity, trust
class, harness, protocol/bridge versions, the declared capability manifest,
assignment history, execution-task history, handoffs on the agent's work
items, and the background-routine/cron inventory (ACMS-REQ-014).

Honesty rules (plan §14):
- ACMS records routine inventory; it does NOT schedule or authorize individual
  routine executions — the page says so.
- Heartbeat/liveness does not exist yet (slice 3): no fabricated status.
- Assignment history is database state only; delivery/acceptance (A2A) is
  slice 4 and the recorded-only distinction carries over from the Work UI.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_session
from ..registry import list_agents
from .. import work_service
from .routes import _base_context
from .session_auth import current_user
from .work_routes import DELIVERY_STATE

router = APIRouter(prefix="/ui/agents", include_in_schema=False)

ROUTINE_NOTE = (
    "Inventory only — agents declare/report these; ACMS does not schedule or "
    "authorize individual routine executions."
)

# Capability flags rendered as a manifest (declared vs not declared).
_CAP_FLAGS = (
    "streaming", "pause", "resume", "interrupt",
    "cancel", "steering", "transcript_export", "background_routine_inventory",
)


def _templates():
    from .routes import templates

    return templates


def _fmt(dt):
    return dt.strftime("%Y-%m-%d %H:%M") if dt else "—"


async def _agent_or_404(db: AsyncSession, agent_id: str):
    agents = {a.agent_id: a for a in await list_agents(db)}
    agent = agents.get(agent_id)
    if agent is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="agent not found")
    return agent


@router.get("/{agent_id}")
async def agent_detail(
    request: Request,
    agent_id: str,
    user=Depends(current_user),
    db: AsyncSession = Depends(get_session),
):
    agent = await _agent_or_404(db, agent_id)

    # --- assignment history (all statuses; newest first) ---
    assignments = await work_service.list_assignments(db, agent_id=agent_id)
    assignments = sorted(assignments, key=lambda a: a.assigned_at, reverse=True)

    # Resolve work-item titles for assignments and handoff scope.
    items = await work_service.list_work_items(db)
    titles = {i.work_item_id: i.title for i in items}
    assigned_item_ids = {a.work_item_id for a in assignments}

    active = next((a for a in assignments if a.status == "ACTIVE"), None)

    # --- execution-task history for this agent ---
    tasks = await work_service.list_execution_tasks(db, agent_id=agent_id)
    tasks = sorted(tasks, key=lambda t: t.started_at, reverse=True)

    # --- handoffs on work items this agent has been assigned to ---
    handoffs = []
    for item_id in assigned_item_ids:
        handoffs.extend(await work_service.list_handoffs(db, work_item_id=item_id))
    handoffs = sorted(handoffs, key=lambda h: h.created_at, reverse=True)

    # --- background routine inventory (ACMS-REQ-014) ---
    routines = await work_service.list_routines(db, agent_id=agent_id)
    routines = sorted(routines, key=lambda r: (not r.enabled, r.name))

    context = _base_context(user) | {
        "agent": {
            "agent_id": agent.agent_id,
            "display_name": agent.display_name,
            "trust_class": agent.trust_class.value,
            "harness": agent.harness,
            "bridge_version": agent.bridge_version,
            "protocol_version": agent.protocol_version,
            "card_url": agent.card_url,
            "capability_hash": agent.capability_hash,
            "created_at": agent.created_at,
            "updated_at": agent.updated_at,
            "created_str": _fmt(agent.created_at),
            "updated_str": _fmt(agent.updated_at),
        },
        "capabilities": [
            {"name": flag.replace("_", " "), "declared": bool(getattr(agent.capabilities, flag, False))}
            for flag in _CAP_FLAGS
        ],
        "extensions": list(agent.capabilities.extensions or []),
        "active_assignment": active,
        "active_item_title": titles.get(active.work_item_id) if active else None,
        "assignments": [
            {
                "assignment_id": a.assignment_id,
                "work_item_id": a.work_item_id,
                "work_item_title": titles.get(a.work_item_id, "(deleted item)"),
                "status": a.status,
                "assigned_by": a.assigned_by or "—",
                "assigned_str": _fmt(a.assigned_at),
                "closed_str": _fmt(a.closed_at),
            }
            for a in assignments
        ],
        "tasks": [
            {
                "task_id": t.task_id,
                "work_item_id": t.work_item_id,
                "work_item_title": titles.get(t.work_item_id, "(deleted item)"),
                "external_task_id": t.external_task_id or "—",
                "status": t.status,
                "started_str": _fmt(t.started_at),
                "finished_str": _fmt(t.finished_at),
            }
            for t in tasks
        ],
        "handoffs": [
            {
                "handoff_id": h.handoff_id,
                "work_item_id": h.work_item_id,
                "work_item_title": titles.get(h.work_item_id, "(deleted item)"),
                "title": h.title,
                "body_markdown": h.body_markdown,
                "author": h.agent_id or "system/human",
                "created_str": _fmt(h.created_at),
            }
            for h in handoffs
        ],
        "routines": [
            {
                "routine_id": r.routine_id,
                "name": r.name,
                "purpose": r.purpose or "—",
                "schedule": r.schedule or "—",
                "enabled": r.enabled,
                "latest_status": r.latest_status or "no report yet",
                "reported_str": _fmt(r.latest_report_at),
            }
            for r in routines
        ],
        "routine_note": ROUTINE_NOTE,
        # Slice 3 has not landed: liveness is not maintained anywhere yet.
        "heartbeat_pending": True,
        "delivery_state": DELIVERY_STATE,
    }
    return _templates().TemplateResponse(request, "agent_detail.html", context)
