"""Attention/audit service — Phase 10 slice 6 foundation (REV2 plan §15).

Semantic Attention candidates derived from the durable event log (never token
noise). SSE transport ships separately; this slice ships the derived Attention
feed + API so the UI and any SSE layer have one authority to subscribe to.

Attention rules (REV2 §15 priority list, mapped to durable events):
  UNREACHABLE / recovery exhausted → AgentEvent UNREACHABLE / RECONCILE_* failures
  failed/rejected execution        → execution task FAILED + A2A rejects
  handoff requesting human input   → handoff event with human_input flag
  mandatory approval               → APPROVAL_REQUESTED events
  Hermes state-backup failure      → STATE_SYNC_FAILED
  hard budget reached              → BUDGET_THRESHOLD_CROSSED (HARD)
  budget override required         → BUDGET_OVERRIDE (already an audit fact)
  session rotation required        → SESSION_ROTATION_REQUIRED
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from .db import get_session
from .security import require_admin_token
from .telemetry_models import AgentEventRecord

router = APIRouter(prefix="/api/v1/attention", dependencies=[Depends(require_admin_token)])

# Attention-eligible event types (semantic state changes only)
ATTENTION_EVENT_TYPES = (
    "UNREACHABLE",
    "RECONCILE_FAILED",
    "RUNTIME_RECOVERY_EXHAUSTED",
    "EXECUTION_FAILED",
    "EXECUTION_REJECTED",
    "HANDOFF_HUMAN_INPUT",
    "APPROVAL_REQUESTED",
    "STATE_SYNC_FAILED",
    "BUDGET_THRESHOLD_CROSSED",
    "BUDGET_OVERRIDE",
    "SESSION_ROTATION_REQUIRED",
    "RUNTIME_STOPPED_WITH_ACTIVE_WORK",
)

# Severity mapping (REV2 §15 priority order: highest first)
SEVERITY = {
    "UNREACHABLE": "critical",
    "RUNTIME_RECOVERY_EXHAUSTED": "critical",
    "EXECUTION_REJECTED": "high",
    "EXECUTION_FAILED": "high",
    "APPROVAL_REQUESTED": "high",
    "HANDOFF_HUMAN_INPUT": "high",
    "STATE_SYNC_FAILED": "high",
    "BUDGET_THRESHOLD_CROSSED": "high",
    "BUDGET_OVERRIDE": "medium",
    "SESSION_ROTATION_REQUIRED": "medium",
    "RECONCILE_FAILED": "medium",
    "RUNTIME_STOPPED_WITH_ACTIVE_WORK": "medium",
}

DEFAULT_LOOKBACK_HOURS = 72


class AttentionItem(BaseModel):
    event_id: str
    sequence: int
    timestamp: str | None
    event_type: str
    severity: str
    agent_id: str | None
    summary: str
    acknowledged: bool = False


@router.get("", response_model=list[AttentionItem])
async def attention_feed(
    db: AsyncSession = Depends(get_session),
    hours: int = Query(default=DEFAULT_LOOKBACK_HOURS, ge=1, le=24 * 30),
    severity: str | None = Query(default=None),
    include_acknowledged: bool = False,
):
    """Derived Attention feed: recent semantic events that need human/Executive
    eyes. Deterministic derivation from the audit log — no separate state to
    drift out of sync. BUDGET_THRESHOLD_CROSSED items whose metadata says HARD
    rank high; the summary already carries the state."""
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    stmt = (
        select(AgentEventRecord)
        .where(AgentEventRecord.event_type.in_(ATTENTION_EVENT_TYPES))
        .where(AgentEventRecord.timestamp >= since)
        .order_by(AgentEventRecord.sequence.desc())
        .limit(200)
    )
    rows = (await db.execute(stmt)).scalars().all()
    items = []
    for r in rows:
        sev = SEVERITY.get(r.event_type, "medium")
        if r.event_type == "BUDGET_THRESHOLD_CROSSED" and "HARD_EXCEEDED" in (r.summary or ""):
            sev = "critical"
        if severity and sev != severity:
            continue
        items.append(AttentionItem(
            event_id=r.event_id, sequence=r.sequence,
            timestamp=r.timestamp.isoformat() if r.timestamp else None,
            event_type=r.event_type, severity=sev, agent_id=r.agent_id,
            summary=r.summary))
    return items
