"""Live telemetry API (combined slice 3+4; ADR-0010; REQ-033/052/053/054).

Bearer-gated like every machine API (REQ-039): the bridge/harness posts
acms-heartbeat-v1 snapshots here every 60 s. The human UI never receives the
bearer token (plan §18) — it reads the same data through server-rendered pages.
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from .db import get_session
from .security import require_admin_token
from .telemetry_models import AgentEventRecord, AgentStatusCurrentRecord, HeartbeatPayload
from .telemetry_service import TelemetryService

router = APIRouter(prefix="/api/v1/fleet", dependencies=[Depends(require_admin_token)])


def _iso(dt) -> str | None:
    return dt.isoformat() if dt else None


class HeartbeatAck(BaseModel):
    received_at: str
    schema_version: str
    connectivity: str
    alignment: str | None
    context_warning: str | None


@router.post("/agents/{agent_id}/heartbeat", response_model=HeartbeatAck)
async def post_heartbeat(
    agent_id: str,
    payload: HeartbeatPayload,
    _: None = Depends(require_admin_token),
    db: AsyncSession = Depends(get_session),
) -> HeartbeatAck:
    from .models import AgentRecord

    agent = await db.get(AgentRecord, agent_id)
    if agent is None:
        raise _not_found()
    payload.identity.acms_agent_id = agent_id  # path is authoritative
    result = await TelemetryService.ingest_heartbeat(db, agent_id, payload)
    return HeartbeatAck(
        received_at=datetime.now(payload.observed_at.tzinfo).isoformat() if payload.observed_at else datetime.now().isoformat(),
        schema_version=result.get("schema_version", payload.schema_version),
        connectivity=result["connectivity"],
        alignment=result["alignment"],
        context_warning=result["context_warning"],
    )


class StatusView(BaseModel):
    agent_id: str
    connectivity: str
    last_contact_at: str | None = None
    last_contact_source: str | None = None
    stale_since: str | None = None
    agent_running: str | None = None
    session_id: str | None = None
    session_title: str | None = None
    expected_work_key: str | None = None
    expected_assignment_key: str | None = None
    alignment: str | None = None
    model_id: str | None = None
    provider: str | None = None
    context_used_tokens: int | None = None
    context_max_tokens: int | None = None
    context_utilization_percent: float | None = None
    context_warning: str | None = None
    context_telemetry_valid: bool | None = None
    context_max_source: str | None = None
    session_total_tokens: int | None = None
    cumulative_api_tokens: int | None = None
    platforms_connected: str | None = None
    bridge_version: str | None = None
    harness_version: str | None = None
    heartbeat_schema_version: str | None = None
    updated_at: str | None = None


def _view(rec: AgentStatusCurrentRecord) -> StatusView:
    return StatusView(
        agent_id=rec.agent_id,
        connectivity=rec.connectivity,
        last_contact_at=_iso(rec.last_contact_at),
        last_contact_source=rec.last_contact_source,
        stale_since=_iso(rec.stale_since),
        agent_running=rec.agent_running,
        session_id=rec.session_id,
        session_title=rec.session_title,
        expected_work_key=rec.expected_work_key,
        expected_assignment_key=rec.expected_assignment_key,
        alignment=rec.alignment,
        model_id=rec.model_id,
        provider=rec.provider,
        context_used_tokens=rec.context_used_tokens,
        context_max_tokens=rec.context_max_tokens,
        context_utilization_percent=rec.context_utilization_percent,
        context_warning=rec.context_warning,
        context_telemetry_valid=rec.context_telemetry_valid,
        context_max_source=rec.context_max_source,
        session_total_tokens=rec.session_total_tokens,
        cumulative_api_tokens=rec.cumulative_api_tokens,
        platforms_connected=rec.platforms_connected,
        bridge_version=rec.bridge_version,
        harness_version=rec.harness_version,
        heartbeat_schema_version=rec.heartbeat_schema_version,
        updated_at=_iso(rec.updated_at),
    )


@router.get("/status", response_model=list[StatusView])
async def get_fleet_status(
    _: None = Depends(require_admin_token),
    db: AsyncSession = Depends(get_session),
):
    rows = await TelemetryService.list_status(db)
    return [_view(r) for r in rows]


@router.get("/agents/{agent_id}/status", response_model=StatusView)
async def get_agent_status(
    agent_id: str,
    _: None = Depends(require_admin_token),
    db: AsyncSession = Depends(get_session),
):
    rec = await TelemetryService.get_status(db, agent_id)
    if rec is None:
        raise _not_found()
    return _view(rec)


class AgentEventView(BaseModel):
    event_id: str
    sequence: int
    timestamp: str
    event_type: str
    actor_source: str | None
    agent_id: str | None
    work_key: str | None
    assignment_key: str | None
    harness_session_id: str | None
    summary: str


@router.get("/events", response_model=list[AgentEventView])
async def get_events(
    agent_id: str | None = None,
    event_type: str | None = None,
    limit: int = 100,
    _: None = Depends(require_admin_token),
    db: AsyncSession = Depends(get_session),
):
    from sqlalchemy import select

    stmt = select(AgentEventRecord).order_by(AgentEventRecord.sequence.desc()).limit(min(limit, 500))
    if agent_id:
        stmt = stmt.where(AgentEventRecord.agent_id == agent_id)
    if event_type:
        stmt = stmt.where(AgentEventRecord.event_type == event_type)
    rows = (await db.scalars(stmt)).all()
    return [
        AgentEventView(
            event_id=r.event_id, sequence=r.sequence, timestamp=_iso(r.timestamp),
            event_type=r.event_type, actor_source=r.actor_source, agent_id=r.agent_id,
            work_key=r.work_key, assignment_key=r.assignment_key,
            harness_session_id=r.harness_session_id, summary=r.summary,
        )
        for r in rows
    ]


def _not_found():
    from fastapi import HTTPException, status as http_status

    return HTTPException(status_code=http_status.HTTP_404_NOT_FOUND, detail="agent not found")
