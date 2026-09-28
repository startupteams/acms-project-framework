"""Runtime lifecycle views for ACMS (REV4 §12/§13; flight plan Phase 3).

ACMS mirrors ARM's runtime lifecycle state (desired / actual / recovery) for
its agents so Administrators can Start/Stop/Restart/Reconcile a runtime from
ACMS, and see runtime events in the semantic event history.

Boundaries (plan §9, ADR-0010):
- Runtime lifecycle ≠ A2A Interrupt/Cancel/Steer/task/session controls. The
  A2A layer owns conversation control; this module owns VM-level lifecycle.
- ACMS never talks to Proxmox (§13): every action is a Server Manager call.
- Active Work is preserved on lifecycle actions; if a runtime goes down while
  Work remains assigned, the Work state is untouched and the dependency is
  surfaced via semantic events (RUNTIME_STOPPED_WITH_ACTIVE_WORK etc.).
- No casual persistent-worker deletion: destroy is not exposed here.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .db import get_session
from .models import AgentRecord
from .security import require_admin_token
from .server_manager_client import (
    RuntimeHandle,
    ServerManagerError,
)
from .settings import get_settings
from .telemetry_service import add_event

router = APIRouter(prefix="/api/v1/fleet", dependencies=[Depends(require_admin_token)])


def _client():
    s = get_settings()
    if not s.server_manager_base_url or not s.server_manager_token:
        raise HTTPException(status_code=503, detail="Server Manager integration not configured")
    from .server_manager_client import ServerManagerClient

    return ServerManagerClient(s.server_manager_base_url, s.server_manager_token)


def _iso(dt) -> str | None:
    return dt.isoformat() if dt else None


class RuntimeView(BaseModel):
    agent_id: str
    runtime_id: str | None
    desired_state: str | None
    actual_state: str | None
    recovery_count: int | None
    last_error: str | None
    last_reconcile_at: str | None
    node: str | None
    vmid: int | None
    state_sync_health: str | None
    hermes_state_branch: str | None
    last_state_commit_sha: str | None
    provisioning_request_id: str | None


class RuntimeAction(BaseModel):
    reason: str


class RuntimeActionOut(BaseModel):
    agent_id: str
    runtime_id: str | None
    action: str
    desired_state: str | None
    actual_state: str | None
    detail: dict = {}


async def _runtime_for_agent(db: AsyncSession, agent_id: str, client) -> RuntimeHandle:
    """Resolve the ARM runtime correlated to this agent (latest row wins)."""
    try:
        runtimes = client.list_runtimes(acms_agent_id=agent_id)
    except ServerManagerError as e:
        raise HTTPException(status_code=502, detail=f"Server Manager error: {e}")
    if not runtimes:
        raise HTTPException(status_code=404, detail="no runtime provisioned for this agent")
    return runtimes[0]


async def _agent_exists(db: AsyncSession, agent_id: str) -> None:
    agent = await db.get(AgentRecord, agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail="agent not found")


async def _event(db: AsyncSession, agent_id: str, event_type: str, summary: str, meta: dict | None = None):
    await add_event(db, event_type=event_type, actor_source="runtime_lifecycle",
                    agent_id=agent_id, summary=summary, metadata=meta or {})


@router.get("/agents/{agent_id}/runtime", response_model=RuntimeView)
async def get_runtime(agent_id: str, db: AsyncSession = Depends(get_session)) -> RuntimeView:
    await _agent_exists(db, agent_id)
    client = _client()
    rt = await _runtime_for_agent(db, agent_id, client)
    return RuntimeView(
        agent_id=agent_id, runtime_id=rt.runtime_id,
        desired_state=rt.raw.get("desired_state"), actual_state=rt.raw.get("actual_state"),
        recovery_count=rt.raw.get("recovery_count"), last_error=rt.raw.get("last_error"),
        last_reconcile_at=rt.raw.get("last_reconcile_at"),
        node=rt.node, vmid=rt.vmid,
        state_sync_health=rt.raw.get("state_sync_health"),
        hermes_state_branch=rt.raw.get("hermes_state_branch"),
        last_state_commit_sha=rt.raw.get("last_state_commit_sha"),
        provisioning_request_id=rt.raw.get("provisioning_request_id"),
    )


async def _apply_desired(db: AsyncSession, agent_id: str, client, runtime_id: str,
                         desired: str, action: str, reason: str) -> RuntimeActionOut:
    try:
        rt = client.set_desired_state(runtime_id, desired, reason=reason)
    except ServerManagerError as e:
        raise HTTPException(status_code=502, detail=f"Server Manager error: {e}")
    # execute desired state now (ARM reconciler §10B)
    detail: dict = {"desired_state": desired}
    try:
        rec = client.reconcile(runtime_id)
        detail["reconcile"] = rec
    except ServerManagerError as e:
        detail["reconcile_error"] = str(e)[:300]
    await _event(db, agent_id, f"RUNTIME_{action}",
                 summary=f"Runtime {action} (desired={desired}): {reason}",
                 meta={"runtime_id": runtime_id, **detail})
    await db.commit()
    return RuntimeActionOut(agent_id=agent_id, runtime_id=runtime_id, action=action,
                            desired_state=rt.raw.get("desired_state"),
                            actual_state=rt.raw.get("actual_state"), detail=detail)


@router.post("/agents/{agent_id}/runtime/start", response_model=RuntimeActionOut)
async def start_runtime(agent_id: str, body: RuntimeAction, db: AsyncSession = Depends(get_session)):
    await _agent_exists(db, agent_id)
    client = _client()
    rt = await _runtime_for_agent(db, agent_id, client)
    return await _apply_desired(db, agent_id, client, rt.runtime_id, "DESIRED_RUNNING", "START", body.reason)


@router.post("/agents/{agent_id}/runtime/stop", response_model=RuntimeActionOut)
async def stop_runtime(agent_id: str, body: RuntimeAction, db: AsyncSession = Depends(get_session)):
    """Stop = graceful VM shutdown; Work assignments are PRESERVED (plan §9)."""
    await _agent_exists(db, agent_id)
    # surface the external runtime dependency if Work remains assigned
    from .work_models import AssignmentRecord

    active = (await db.execute(
        select(AssignmentRecord).where(
            AssignmentRecord.agent_id == agent_id,
            AssignmentRecord.status == "ACTIVE",
        ).limit(1)
    )).scalar_one_or_none()
    client = _client()
    rt = await _runtime_for_agent(db, agent_id, client)
    out = await _apply_desired(db, agent_id, client, rt.runtime_id, "DESIRED_STOPPED", "STOP", body.reason)
    if active is not None:
        await _event(db, agent_id, "RUNTIME_STOPPED_WITH_ACTIVE_WORK",
                     summary=f"Runtime stopped while Work {active.work_item_id} remains assigned — Work state preserved; runtime dependency active",
                     meta={"assignment_id": active.assignment_id,
                           "assignment_key": active.assignment_key,
                           "work_item_id": active.work_item_id})
        await db.commit()
    return out


@router.post("/agents/{agent_id}/runtime/restart", response_model=RuntimeActionOut)
async def restart_runtime(agent_id: str, body: RuntimeAction, db: AsyncSession = Depends(get_session)):
    await _agent_exists(db, agent_id)
    client = _client()
    rt = await _runtime_for_agent(db, agent_id, client)
    await _apply_desired(db, agent_id, client, rt.runtime_id, "DESIRED_STOPPED", "RESTART_STOP", body.reason)
    return await _apply_desired(db, agent_id, client, rt.runtime_id, "DESIRED_RUNNING", "RESTART_START", body.reason)


@router.post("/agents/{agent_id}/runtime/reconcile", response_model=RuntimeActionOut)
async def reconcile_runtime(agent_id: str, body: RuntimeAction, db: AsyncSession = Depends(get_session)):
    await _agent_exists(db, agent_id)
    client = _client()
    rt = await _runtime_for_agent(db, agent_id, client)
    try:
        rec = client.reconcile(rt.runtime_id)
    except ServerManagerError as e:
        raise HTTPException(status_code=502, detail=f"Server Manager error: {e}")
    await _event(db, agent_id, "RUNTIME_RECONCILED",
                 summary=f"Reconcile now: {rec.get('action')} ({rec.get('before')} -> {rec.get('after')})",
                 meta={"runtime_id": rt.runtime_id, "reconcile": rec})
    await db.commit()
    return RuntimeActionOut(agent_id=agent_id, runtime_id=rt.runtime_id, action="reconcile",
                            desired_state=rec.get("desired"), actual_state=rec.get("after"), detail=rec)
