"""Memory/Session Offload service + API (flight plan Phase 8; REQ-055..058).

Proof flow this slice implements (plan §13):
  Work → Session 1 + Context Package → handoff/checkpoint → Session 1 closes
  → Session 2 receives a compact reconstructed package → same Work stays active.

Economic rotation is ADVISORY only (no auto-kill): compute_advisory() maps
context utilization to GREEN/MONITOR/CHECKPOINT_RECOMMENDED/ROTATE_RECOMMENDED.
Session 2's reconstructed package is COMPACT: references to the Work state,
latest handoff/checkpoint, and live assignment facts — never full transcripts.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .db import get_session
from .memory_models import (
    HANDOFF_REQUIRED_FIELDS,
    ContextPackageRecord,
    ExecutionSessionRecord,
    SessionCheckpointRecord,
)
from .security import require_admin_token
from .telemetry_service import add_event

router = APIRouter(prefix="/api/v1/memory", dependencies=[Depends(require_admin_token)])


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt) -> str | None:
    return dt.isoformat() if dt else None


# ------------------------------------------------------------- advisory (058/059)
ADVISORY_GREEN = "GREEN"
ADVISORY_MONITOR = "MONITOR"
ADVISORY_CHECKPOINT = "CHECKPOINT_RECOMMENDED"
ADVISORY_ROTATE = "ROTATE_RECOMMENDED"


def compute_advisory(context_pct: float | None, max_context: int | None,
                     estimated_cost: float | None = None,
                     soft_budget_usd: float | None = None) -> str:
    """Advisory from context utilization (context thresholds 70/85/95 are the
    ADR-0010 warning bands; rotation recommends at HIGH/CRITICAL)."""
    if context_pct is None or max_context is None:
        return ADVISORY_GREEN  # no telemetry → no advisory claim
    if context_pct >= 85:
        return ADVISORY_ROTATE
    if context_pct >= 70:
        return ADVISORY_CHECKPOINT
    if context_pct >= 50:
        return ADVISORY_MONITOR
    return ADVISORY_GREEN


# ------------------------------------------------------- completeness (057)
def validate_handoff_completeness(checkpoint_body: dict) -> tuple[bool, dict]:
    """REQ-057: every required field must be present AND non-empty."""
    result = {}
    for f in HANDOFF_REQUIRED_FIELDS:
        v = checkpoint_body.get(f)
        result[f] = bool(v is not None and (not isinstance(v, str) or v.strip() != ""))
    return all(result.values()), result


# ------------------------------------------------------------------ schemas
class SessionCreate(BaseModel):
    agent_id: str
    work_item_id: str | None = None
    assignment_id: str | None = None
    a2a_task_id: str | None = None
    harness_session_id: str | None = None
    model_id: str | None = None
    provider: str | None = None
    context_package_id: str | None = None


class SessionTelemetryUpdate(BaseModel):
    current_context_tokens: int | None = None
    max_context_tokens: int | None = None
    cumulative_api_tokens: int | None = None
    estimated_cost_usd: float | None = None
    estimated_cloud_cost_usd: float | None = None
    estimated_local_cost_usd: float | None = None
    model_id: str | None = None
    provider: str | None = None


class ContextPackageCreate(BaseModel):
    work_item_id: str | None = None
    session_id: str | None = None
    policy_version: str | None = None
    selected: list[dict] = Field(default_factory=list)
    estimated_tokens: int | None = None
    notes: str | None = None


class CheckpointCreate(BaseModel):
    session_id: str
    work_item_id: str | None = None
    reason: str | None = None
    summary: str = Field(min_length=4, max_length=512)
    next_action: str | None = None
    artifacts: list[str] = Field(default_factory=list)
    # REQ-057 validator input: all ten fields
    work_id: str | None = None
    status: str | None = None
    git_state: str | None = None
    tests: str | None = None
    deployment: str | None = None
    decisions: str | None = None
    debt: str | None = None
    remaining_work: str | None = None


class SessionView(BaseModel):
    session_id: str
    agent_id: str
    work_item_id: str | None
    assignment_id: str | None
    harness_session_id: str | None
    status: str
    model_id: str | None
    provider: str | None
    current_context_tokens: int | None
    max_context_tokens: int | None
    context_utilization_percent: float | None
    cumulative_api_tokens: int | None
    estimated_cost_usd: float | None
    estimated_cloud_cost_usd: float | None
    estimated_local_cost_usd: float | None
    rotation_advisory: str | None
    context_package_id: str | None
    last_checkpoint_at: str | None
    started_at: str
    ended_at: str | None


class PackageView(BaseModel):
    context_package_id: str
    work_item_id: str | None
    session_id: str | None
    status: str
    assembled_at: str
    policy_version: str | None
    selected: list | None
    estimated_tokens: int | None
    notes: str | None


class CheckpointView(BaseModel):
    checkpoint_id: str
    session_id: str
    work_item_id: str | None
    created_at: str
    reason: str | None
    summary: str
    complete: bool | None
    completeness: dict | None
    next_action: str | None
    artifacts: list | None


def _sess_view(r: ExecutionSessionRecord) -> SessionView:
    return SessionView(
        session_id=r.session_id, agent_id=r.agent_id, work_item_id=r.work_item_id,
        assignment_id=r.assignment_id, harness_session_id=r.harness_session_id,
        status=r.status, model_id=r.model_id, provider=r.provider,
        current_context_tokens=r.current_context_tokens,
        max_context_tokens=r.max_context_tokens,
        context_utilization_percent=r.context_utilization_percent,
        cumulative_api_tokens=r.cumulative_api_tokens,
        estimated_cost_usd=r.estimated_cost_usd,
        estimated_cloud_cost_usd=r.estimated_cloud_cost_usd,
        estimated_local_cost_usd=r.estimated_local_cost_usd,
        rotation_advisory=r.rotation_advisory,
        context_package_id=r.context_package_id,
        last_checkpoint_at=_iso(r.last_checkpoint_at),
        started_at=_iso(r.started_at), ended_at=_iso(r.ended_at),
    )


# ------------------------------------------------------------------ sessions
@router.post("/sessions", response_model=SessionView, status_code=201)
async def create_session(body: SessionCreate, db: AsyncSession = Depends(get_session)):
    rec = ExecutionSessionRecord(
        session_id=ExecutionSessionRecord.new_id(), agent_id=body.agent_id,
        work_item_id=body.work_item_id, assignment_id=body.assignment_id,
        a2a_task_id=body.a2a_task_id, harness_session_id=body.harness_session_id,
        status="OPEN", model_id=body.model_id, provider=body.provider,
        context_package_id=body.context_package_id, started_at=_now(),
    )
    db.add(rec)
    await add_event(db, event_type="EXECUTION_SESSION_OPENED", actor_source="memory_offload",
                    agent_id=body.agent_id,
                    summary=f"Execution session opened for work {body.work_item_id or '—'}",
                    metadata={"session_id": rec.session_id, "work_item_id": body.work_item_id})
    await db.commit()
    return _sess_view(rec)


@router.get("/sessions/{session_id}", response_model=SessionView)
async def get_session_view(session_id: str, db: AsyncSession = Depends(get_session)):
    rec = await db.get(ExecutionSessionRecord, session_id)
    if rec is None:
        raise HTTPException(status_code=404, detail="session not found")
    return _sess_view(rec)


@router.get("/work/{work_item_id}/sessions", response_model=list[SessionView])
async def list_work_sessions(work_item_id: str, db: AsyncSession = Depends(get_session)):
    rows = (await db.execute(
        select(ExecutionSessionRecord).where(ExecutionSessionRecord.work_item_id == work_item_id)
        .order_by(ExecutionSessionRecord.started_at)
    )).scalars().all()
    return [_sess_view(r) for r in rows]


@router.post("/sessions/{session_id}/telemetry", response_model=SessionView)
async def update_session_telemetry(session_id: str, body: SessionTelemetryUpdate,
                                   db: AsyncSession = Depends(get_session)):
    """Economic telemetry update (REQ-058): context separate from cumulative.
    Utilization is computed ONLY from valid numbers (never fabricated)."""
    rec = await db.get(ExecutionSessionRecord, session_id)
    if rec is None:
        raise HTTPException(status_code=404, detail="session not found")
    if body.model_id is not None:
        rec.model_id = body.model_id
    if body.provider is not None:
        rec.provider = body.provider
    valid_ctx = (
        body.current_context_tokens is not None and body.current_context_tokens >= 0
        and body.max_context_tokens is not None and body.max_context_tokens and body.max_context_tokens > 0
        and body.current_context_tokens <= body.max_context_tokens
    )
    if valid_ctx:
        rec.current_context_tokens = body.current_context_tokens
        rec.max_context_tokens = body.max_context_tokens
        rec.context_utilization_percent = round(
            100.0 * body.current_context_tokens / body.max_context_tokens, 2)
    elif body.current_context_tokens is not None or body.max_context_tokens is not None:
        # invalid telemetry: keep UNKNOWN (context_utilization stays None), record the fact
        rec.current_context_tokens = None
        rec.max_context_tokens = None
        rec.context_utilization_percent = None
    if body.cumulative_api_tokens is not None and body.cumulative_api_tokens >= 0:
        rec.cumulative_api_tokens = body.cumulative_api_tokens
    if body.estimated_cost_usd is not None and body.estimated_cost_usd >= 0:
        rec.estimated_cost_usd = body.estimated_cost_usd
    if body.estimated_cloud_cost_usd is not None and body.estimated_cloud_cost_usd >= 0:
        rec.estimated_cloud_cost_usd = body.estimated_cloud_cost_usd
    if body.estimated_local_cost_usd is not None and body.estimated_local_cost_usd >= 0:
        rec.estimated_local_cost_usd = body.estimated_local_cost_usd
    prev = rec.rotation_advisory
    rec.rotation_advisory = compute_advisory(
        rec.context_utilization_percent, rec.max_context_tokens,
        rec.estimated_cost_usd)
    if rec.rotation_advisory in (ADVISORY_CHECKPOINT, ADVISORY_ROTATE) and prev != rec.rotation_advisory:
        await add_event(db, event_type="ROTATION_ADVISORY", actor_source="memory_offload",
                        agent_id=rec.agent_id,
                        summary=f"Advisory {prev or 'GREEN'} -> {rec.rotation_advisory} at {rec.context_utilization_percent}% context",
                        metadata={"session_id": session_id, "advisory": rec.rotation_advisory})
    # REQ-059 soft/hard evaluation: cost telemetry changed → re-evaluate the
    # Work's budget (emits BUDGET_THRESHOLD_CROSSED once per transition).
    if rec.work_item_id:
        from . import budget_service

        await budget_service.refresh_budget_state(db, rec.work_item_id)
    await db.commit()
    return _sess_view(rec)


@router.post("/sessions/{session_id}/close", response_model=SessionView)
async def close_session(session_id: str, db: AsyncSession = Depends(get_session)):
    """Close a session WITHOUT touching Work (REQ-055 core invariant)."""
    rec = await db.get(ExecutionSessionRecord, session_id)
    if rec is None:
        raise HTTPException(status_code=404, detail="session not found")
    rec.status = "CLOSED"
    rec.ended_at = _now()
    await add_event(db, event_type="EXECUTION_SESSION_CLOSED", actor_source="memory_offload",
                    agent_id=rec.agent_id,
                    summary=f"Execution session closed; Work state preserved",
                    metadata={"session_id": session_id, "work_item_id": rec.work_item_id})
    await db.commit()
    return _sess_view(rec)


# ----------------------------------------------------------- context packages
@router.post("/context-packages", response_model=PackageView, status_code=201)
async def create_context_package(body: ContextPackageCreate, db: AsyncSession = Depends(get_session)):
    rec = ContextPackageRecord(
        context_package_id=ContextPackageRecord.new_id(),
        work_item_id=body.work_item_id, session_id=body.session_id,
        status="OPEN", assembled_at=_now(), policy_version=body.policy_version,
        selected_json=json.dumps(body.selected), estimated_tokens=body.estimated_tokens,
        notes=body.notes,
    )
    db.add(rec)
    await db.commit()
    return _pkg_view(rec)


def _pkg_view(r: ContextPackageRecord) -> PackageView:
    return PackageView(
        context_package_id=r.context_package_id, work_item_id=r.work_item_id,
        session_id=r.session_id, status=r.status, assembled_at=_iso(r.assembled_at),
        policy_version=r.policy_version,
        selected=json.loads(r.selected_json) if r.selected_json else None,
        estimated_tokens=r.estimated_tokens, notes=r.notes,
    )


@router.post("/context-packages/{package_id}/seal", response_model=PackageView)
async def seal_context_package(package_id: str, db: AsyncSession = Depends(get_session)):
    rec = await db.get(ContextPackageRecord, package_id)
    if rec is None:
        raise HTTPException(status_code=404, detail="context package not found")
    rec.status = "SEALED"
    await db.commit()
    return _pkg_view(rec)


@router.get("/context-packages/{package_id}", response_model=PackageView)
async def get_context_package(package_id: str, db: AsyncSession = Depends(get_session)):
    rec = await db.get(ContextPackageRecord, package_id)
    if rec is None:
        raise HTTPException(status_code=404, detail="context package not found")
    return _pkg_view(rec)


# ----------------------------------------------------------------- checkpoints
@router.post("/checkpoints", response_model=CheckpointView, status_code=201)
async def create_checkpoint(body: CheckpointCreate, db: AsyncSession = Depends(get_session)):
    rec = await db.get(ExecutionSessionRecord, body.session_id)
    if rec is None:
        raise HTTPException(status_code=404, detail="session not found")
    complete, detail = validate_handoff_completeness(body.model_dump())
    cp = SessionCheckpointRecord(
        checkpoint_id=SessionCheckpointRecord.new_id(), session_id=body.session_id,
        work_item_id=body.work_item_id or rec.work_item_id, created_at=_now(),
        reason=body.reason, summary=body.summary,
        completeness_json=json.dumps(detail), complete=complete,
        next_action=body.next_action, artifact_refs_json=json.dumps(body.artifacts),
    )
    db.add(cp)
    rec.last_checkpoint_at = cp.created_at
    rec.status = "CHECKPOINTED"
    await add_event(db, event_type="SESSION_CHECKPOINT", actor_source="memory_offload",
                    agent_id=rec.agent_id,
                    summary=f"Checkpoint ({'complete' if complete else 'INCOMPLETE'}) : {body.summary[:180]}",
                    metadata={"session_id": body.session_id, "complete": complete,
                              "missing": [k for k, v in detail.items() if not v]})
    await db.commit()
    return _cp_view(cp)


def _cp_view(c: SessionCheckpointRecord) -> CheckpointView:
    return CheckpointView(
        checkpoint_id=c.checkpoint_id, session_id=c.session_id,
        work_item_id=c.work_item_id, created_at=_iso(c.created_at),
        reason=c.reason, summary=c.summary, complete=c.complete,
        completeness=json.loads(c.completeness_json) if c.completeness_json else None,
        next_action=c.next_action,
        artifacts=json.loads(c.artifact_refs_json) if c.artifact_refs_json else None,
    )


@router.get("/sessions/{session_id}/checkpoints", response_model=list[CheckpointView])
async def list_checkpoints(session_id: str, db: AsyncSession = Depends(get_session)):
    rows = (await db.execute(
        select(SessionCheckpointRecord).where(SessionCheckpointRecord.session_id == session_id)
        .order_by(SessionCheckpointRecord.created_at)
    )).scalars().all()
    return [_cp_view(c) for c in rows]


# ------------------------------------------------ rotation reconstruction (055/056)
@router.post("/work/{work_item_id}/rotate", response_model=PackageView)
async def rotate_session(work_item_id: str, body: dict, db: AsyncSession = Depends(get_session)):
    """Close session 1 (if open), assemble a COMPACT reconstructed package for
    session 2 from ACMS state (Work facts + latest checkpoint/handoff + prior
    session summary). References, not transcripts."""
    from .work_models import AssignmentRecord, WorkItemRecord

    item = await db.get(WorkItemRecord, work_item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="work item not found")
    prior_session_id = body.get("prior_session_id")
    if prior_session_id:
        prior = await db.get(ExecutionSessionRecord, prior_session_id)
        if prior is not None and prior.status in ("OPEN", "CHECKPOINTED"):
            prior.status = "CLOSED"
            prior.ended_at = _now()
    # latest checkpoint for the work
    latest_cp = (await db.execute(
        select(SessionCheckpointRecord)
        .where(SessionCheckpointRecord.work_item_id == work_item_id)
        .order_by(SessionCheckpointRecord.created_at.desc()).limit(1)
    )).scalar_one_or_none()
    # live assignment
    live = (await db.execute(
        select(AssignmentRecord).where(
            AssignmentRecord.work_item_id == work_item_id,
            AssignmentRecord.status == "ACTIVE").limit(1)
    )).scalar_one_or_none()
    selected = [
        {"type": "work_item", "id": work_item_id, "title": item.title, "status": item.status},
        {"type": "work_key", "id": item.work_key} if item.work_key else None,
    ]
    if live is not None:
        selected.append({"type": "assignment", "id": live.assignment_id,
                         "assignment_key": live.assignment_key, "status": live.status})
    if latest_cp is not None:
        selected.append({"type": "checkpoint", "id": latest_cp.checkpoint_id,
                         "complete": latest_cp.complete,
                         "summary": latest_cp.summary[:300],
                         "next_action": latest_cp.next_action})
    selected = [s for s in selected if s]
    rec = ContextPackageRecord(
        context_package_id=ContextPackageRecord.new_id(), work_item_id=work_item_id,
        session_id=body.get("new_session_id"), status="OPEN", assembled_at=_now(),
        policy_version=body.get("policy_version", "rotate-compact-v1"),
        selected_json=json.dumps(selected),
        estimated_tokens=sum(len(json.dumps(s)) // 4 for s in selected),
        notes="Reconstructed rotation package: compact ACMS-state references (no transcripts).",
    )
    db.add(rec)
    await add_event(db, event_type="SESSION_ROTATED", actor_source="memory_offload",
                    agent_id=body.get("agent_id") or "",
                    summary=f"Session rotation for work {item.work_key or work_item_id}: compact package assembled",
                    metadata={"work_item_id": work_item_id,
                              "context_package_id": rec.context_package_id,
                              "prior_session_id": prior_session_id})
    await db.commit()
    return _pkg_view(rec)
