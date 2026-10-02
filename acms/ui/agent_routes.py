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
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_session
from ..registry import list_agents
from .. import work_service
from .routes import _base_context
from .session_auth import current_user
from .work_routes import DELIVERY_STATE
from ..telemetry_models import (
    CONNECTIVITY_UNKNOWN, CONNECTIVITY_HEALTHY, CONNECTIVITY_STALE, CONNECTIVITY_UNREACHABLE,
)

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
            "legacy_name": agent.legacy_name,
            "worker_uid": agent.worker_uid,
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
        "delivery_state": DELIVERY_STATE,
    }

    # --- live telemetry (combined slice 3+4; ADR-0010) ---
    from ..telemetry_service import TelemetryService

    status_rec = await TelemetryService.get_status(db, agent_id)
    live = {
        "has_status": status_rec is not None,
        "connectivity": status_rec.connectivity if status_rec else CONNECTIVITY_UNKNOWN,
        "last_contact_str": _fmt(status_rec.last_contact_at) if status_rec else "—",
        "last_contact_source": (status_rec.last_contact_source or "—") if status_rec else "—",
        "agent_running": (status_rec.agent_running or "unknown") if status_rec else "unknown",
        "session_id": status_rec.session_id if status_rec else None,
        "session_title": status_rec.session_title if status_rec else None,
        "alignment": status_rec.alignment if status_rec else None,
        "model_id": status_rec.model_id if status_rec else None,
        "provider": status_rec.provider if status_rec else None,
        "context_used": status_rec.context_used_tokens if status_rec else None,
        "context_max": status_rec.context_max_tokens if status_rec else None,
        "context_pct": status_rec.context_utilization_percent if status_rec else None,
        "context_warning": status_rec.context_warning if status_rec else None,
        "context_valid": status_rec.context_telemetry_valid if status_rec else None,
        "platforms": (status_rec.platforms_connected or "") if status_rec else "",
    }
    # Plan §17: no fabricated context numbers — if telemetry invalid/absent, show that.
    if not live["context_valid"]:
        live["context_display"] = "UNKNOWN/INVALID"
        live["context_pct_display"] = "—"
    elif status_rec is not None and status_rec.context_max_tokens:
        live["context_display"] = f"{status_rec.context_used_tokens:,} / {status_rec.context_max_tokens:,} tokens"
        live["context_pct_display"] = f"{status_rec.context_utilization_percent}%"
    else:
        live["context_display"] = "UNKNOWN"
        live["context_pct_display"] = "—"

    # Recent semantic events for this agent (plan §17 Agent Detail).
    from sqlalchemy import select as _select
    from ..telemetry_models import AgentEventRecord

    ev_rows = (
        await db.scalars(
            select(AgentEventRecord)
            .where(AgentEventRecord.agent_id == agent_id)
            .order_by(AgentEventRecord.sequence.desc())
            .limit(10)
        )
    ).all()
    live["events"] = [
        {"event_type": e.event_type, "summary": e.summary, "when": _fmt(e.timestamp)}
        for e in ev_rows
    ]

    # Controls: only what the harness mapping proved (plan §13/§17).
    supported = ["status", "send_work", "steer", "interrupt", "cancel", "request_handoff", "set_session_title"]
    live["supported_controls"] = supported
    live["unsupported_controls"] = ["pause", "resume"]

    # --- runtime lifecycle (Phase 3; REV4 §12/§13) — Server Manager view ---
    runtime_view = None
    runtime_error = None
    runtime_controls_enabled = False
    from ..settings import get_settings as _gs

    _s = _gs()
    if _s.server_manager_base_url and _s.server_manager_token:
        runtime_controls_enabled = True
        from ..server_manager_client import ServerManagerClient, ServerManagerError

        try:
            client = ServerManagerClient(_s.server_manager_base_url, _s.server_manager_token)
            rts = client.list_runtimes(acms_agent_id=agent_id)
            if rts:
                rt = rts[0]
                runtime_view = {
                    "runtime_id": rt.runtime_id,
                    "desired_state": rt.raw.get("desired_state"),
                    "actual_state": rt.raw.get("actual_state"),
                    "recovery_count": rt.raw.get("recovery_count"),
                    "last_error": rt.raw.get("last_error"),
                    "last_reconcile_str": _fmt_str(rt.raw.get("last_reconcile_at")),
                    "node": rt.node, "vmid": rt.vmid,
                    "state_sync_health": rt.raw.get("state_sync_health"),
                    "hermes_state_branch": rt.raw.get("hermes_state_branch"),
                    "last_state_commit_sha": (rt.raw.get("last_state_commit_sha") or "")[:8] or None,
                }
        except ServerManagerError as e:
            runtime_error = f"Server Manager unreachable: {e.__class__.__name__}"

    context |= {
        "live": live,
        "heartbeat_pending": False,
        "runtime": runtime_view,
        "runtime_error": runtime_error,
        "runtime_controls_enabled": runtime_controls_enabled,
        # ---- W2: MCP capability card (plan §23) — best-effort, honest ----
        "mcp_card": _mcp_capability_card(agent),
        "mcp_gateway_configured": _mcp_gateway_configured(),
    }
    return _templates().TemplateResponse(request, "agent_detail.html", context)


def _mcp_gateway_configured() -> bool:
    from ..settings import get_settings as _gs

    _s = _gs()
    return bool(_s.mcp_gateway_base_url and _s.mcp_gateway_internal_token)


def _mcp_capability_card(agent) -> dict | None:
    """Fetch the agent's MCP capability card from the gateway (plan §23).

    Best-effort: gateway down / unconfigured / unknown agent ⇒ None and the
    UI renders the honest 'gateway unavailable' state. Token values are never
    part of the card (gateway exports metadata only)."""
    if not _mcp_gateway_configured():
        return None
    try:
        from ..mcp_gateway_client import McpGatewayClient

        card = McpGatewayClient().capability_card(agent.display_name)
    except Exception:  # noqa: BLE001 — card must never break the page
        return None
    if card and isinstance(card.get("last_mcp_activity"), dict):
        la = card["last_mcp_activity"]
        card["last_mcp_activity"]["ts_str"] = _fmt_str(la.get("ts"))
    return card


def _fmt_str(iso: str | None) -> str:
    if not iso:
        return "—"
    try:
        from datetime import datetime as _dt

        return _dt.fromisoformat(iso.replace("Z", "+00:00")).strftime("%Y-%m-%d %H:%M")
    except (ValueError, TypeError):
        return iso or "—"


@router.post("/{agent_id}/runtime/{action}")
async def runtime_action(
    request: Request,
    agent_id: str,
    action: str,
    user=Depends(current_user),
    db: AsyncSession = Depends(get_session),
):
    """Administrator runtime lifecycle controls (Phase 3; REV4 §12).

    Start/Stop/Restart/Reconcile the agent's runtime via Server Manager.
    Distinct from A2A interrupt/cancel/steer (plan §9). Requires the admin
    role; work state is preserved on lifecycle actions.
    """
    from .roles import Role

    try:
        role = Role(user.role)
    except ValueError:
        role = None
    if role is not Role.ADMINISTRATOR:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Administrator role required")
    if action not in ("start", "stop", "restart", "reconcile"):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="unknown runtime action")
    agent = await _agent_or_404(db, agent_id)

    from ..settings import get_settings as _gs

    _s = _gs()
    if not _s.server_manager_base_url or not _s.server_manager_token:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                            detail="Server Manager integration not configured")
    from ..server_manager_client import ServerManagerClient, ServerManagerError

    client = ServerManagerClient(_s.server_manager_base_url, _s.server_manager_token)
    try:
        rts = client.list_runtimes(acms_agent_id=agent_id)
        if not rts:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                                detail="no runtime provisioned for this agent")
        runtime_id = rts[0].runtime_id
        reason = f"UI runtime {action} by {getattr(user, 'username', 'admin')}"
        if action == "start":
            client.set_desired_state(runtime_id, "DESIRED_RUNNING", reason)
            rec = client.reconcile(runtime_id)
        elif action == "stop":
            client.set_desired_state(runtime_id, "DESIRED_STOPPED", reason)
            rec = client.reconcile(runtime_id)
        elif action == "restart":
            client.set_desired_state(runtime_id, "DESIRED_STOPPED", reason)
            client.reconcile(runtime_id)
            client.set_desired_state(runtime_id, "DESIRED_RUNNING", reason)
            rec = client.reconcile(runtime_id)
        else:  # reconcile
            rec = client.reconcile(runtime_id)
    except ServerManagerError as e:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY,
                            detail=f"Server Manager error: {e}")

    # semantic event (best-effort; never blocks the action result)
    try:
        from ..telemetry_service import add_event

        await add_event(db, event_type=f"RUNTIME_UI_{action.upper()}", actor_source="ui",
                        agent_id=agent_id,
                        summary=f"UI runtime {action}: {rec.get('before')} -> {rec.get('after')} ({rec.get('action')})",
                        metadata={"runtime_id": runtime_id, "reconcile": rec})
        await db.commit()
    except Exception:  # noqa: BLE001 — audit best-effort
        pass
    return RedirectResponse(url=f"/ui/agents/{agent_id}", status_code=status.HTTP_303_SEE_OTHER)
