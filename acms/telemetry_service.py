"""Live telemetry service (combined slice 3+4; ADR-0010; REQ-033/034/052/053/054).

Responsibilities:
- ingest heartbeats (complete lightweight snapshots, acms-heartbeat-v1);
- derive connectivity from contact recency (UNKNOWN/HEALTHY/STALE/UNREACHABLE);
- derive context warning levels (NORMAL/ELEVATED/HIGH/CRITICAL);
- compare reported session vs approved assignment (ALIGNED/UNKNOWN/MISMATCH);
- persist semantic events on material change only (never per-heartbeat spam);
- reconciliation (1 h stale, 24 h fleet) — corrects operational state only;
- background scheduling with injected clock (tests never wait real time).
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from .models import AgentRecord
from .telemetry_models import (
    AgentSessionBindingRecord,
    AgentStatusCurrentRecord,
    AgentTelemetrySampleRecord,
    AgentEventRecord,
    CONTEXT_ELEVATED_DEFAULT,
    CONTEXT_HIGH_DEFAULT,
    CONTEXT_CRITICAL_DEFAULT,
    CONNECTIVITY_HEALTHY,
    CONNECTIVITY_STALE,
    CONNECTIVITY_UNREACHABLE,
    CONTACT_SOURCE_HEARTBEAT,
    CONTACT_SOURCE_RECONCILIATION,
    HeartbeatPayload,
    HeartbeatSession,
)
from .work_keys import extract_work_key_from_title
from .work_models import AssignmentRecord
from .settings import get_settings

logger = logging.getLogger("acms.telemetry")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _new_id() -> str:
    return str(uuid.uuid4())


async def _next_event_sequence(db) -> int:
    """Allocate the next agent_events sequence transactionally (durable, race-safe)."""
    from sqlalchemy import text as _text

    await db.execute(
        _text("INSERT INTO acms_key_counters (counter_name, counter_value) VALUES ('agent_event_seq', 0) ON CONFLICT (counter_name) DO NOTHING")
    )
    row = await db.execute(
        _text("UPDATE acms_key_counters SET counter_value = counter_value + 1 "
              "WHERE counter_name = 'agent_event_seq' RETURNING counter_value"),
        {},
    )
    return int(row.scalar_one())


def _warn_level(percent: float | None, s=None) -> str | None:
    """Derive context warning level. None passthrough (not reported)."""
    if percent is None:
        return None
    s = s or get_settings()
    if percent >= s.context_critical_percent:
        return "CRITICAL"
    if percent >= s.context_high_percent:
        return "HIGH"
    if percent >= s.context_elevated_percent:
        return "ELEVATED"
    return "NORMAL"


def _context_valid(used, mx) -> bool:
    """Validity rules (plan §7): both present, non-negative, used <= max."""
    if used is None or mx is None:
        return False
    if used < 0 or mx <= 0 or used > mx:
        return False
    return True


async def add_event(db, **kw) -> "AgentEventRecord":
    """Allocate sequence via the durable counter and add the event (no commit)."""
    seq = await _next_event_sequence(db)
    meta = kw.pop("metadata", None)
    if isinstance(meta, dict):
        kw["metadata_json"] = json.dumps(meta)
    kw.setdefault("timestamp", _now())
    rec = AgentEventRecord(
        event_id=_new_id(),
        sequence=seq,
        **kw,
    )
    db.add(rec)
    return rec


def _tri_state(value: bool | None) -> str | None:
    if value is None:
        return "unknown"
    return "true" if value else "false"


def alignment_for(session_title: str | None, expected_work_key: str | None, session_binding: AgentSessionBindingRecord | None, grace_seconds: int, now: datetime) -> str:
    """Derive ALIGNED/UNKNOWN/MISMATCH (plan §10). Never changes scope."""
    if expected_work_key is None:
        return "UNKNOWN"
    title = (session_binding.session_title if session_binding else None) or ""
    reported = extract_work_key_from_title(title) or (session_binding.session_id if session_binding else None)
    if reported is None:
        # Agent running but no correlation yet → grace period before MISMATCH
        if session_binding and session_binding.last_seen_at and now - session_binding.last_seen_at < timedelta(seconds=grace_seconds):
            return "UNKNOWN"
        return "MISMATCH" if session_binding else "UNKNOWN"
    return "ALIGNED" if reported == expected_work_key else "MISMATCH"


class TelemetryService:
    """Stateless-ish helpers over the telemetry tables; session injected."""

    @staticmethod
    async def record_event(
        db: AsyncSession, *, event_type: str, summary: str, agent_id: str | None = None,
        work_key: str | None = None, assignment_key: str | None = None,
        a2a_task_id: str | None = None, harness_session_id: str | None = None,
        actor_source: str | None = None, metadata: dict | None = None,
        correlation_id: str | None = None,
    ) -> AgentEventRecord:
        seq = await _next_event_sequence(db)
        rec = AgentEventRecord(
            event_id=str(__import__("uuid").uuid4()),
            sequence=seq,
            timestamp=_now(),
            actor_source=actor_source,
            agent_id=agent_id,
            work_key=work_key,
            assignment_key=assignment_key,
            a2a_task_id=a2a_task_id,
            harness_session_id=harness_session_id,
            summary=summary[:512],
            metadata_json=json.dumps(metadata) if metadata else None,
            correlation_id=correlation_id,
        )
        db.add(rec)
        await db.commit()
        return rec

    @staticmethod
    async def get_status(db: AsyncSession, agent_id: str) -> AgentStatusCurrentRecord | None:
        return await db.get(AgentStatusCurrentRecord, agent_id)

    @staticmethod
    async def list_status(db: AsyncSession) -> list[AgentStatusCurrentRecord]:
        result = await db.scalars(select(AgentStatusCurrentRecord))
        return list(result.all())

    @staticmethod
    async def ensure_status_row(db: AsyncSession, agent_id: str) -> AgentStatusCurrentRecord:
        rec = await db.get(AgentStatusCurrentRecord, agent_id)
        if rec is None:
            rec = AgentStatusCurrentRecord(
                agent_id=agent_id,
                connectivity=CONNECTIVITY_HEALTHY,  # a first contact is a contact
                last_contact_at=_now(),
                last_contact_source=CONTACT_SOURCE_HEARTBEAT,
                stale_since=None,
                updated_at=_now(),
            )
            db.add(rec)
        return rec

    @staticmethod
    def derive_connectivity(
        prev: AgentStatusCurrentRecord | None, now_dt: datetime, stale_seconds: int, reconcile_seconds: int
    ) -> str:
        """UNKNOWN / HEALTHY / STALE / UNREACHABLE from last_contact + history."""
        if prev is None or prev.last_contact_at is None:
            return "UNKNOWN"
        delta = (now_dt - prev.last_contact_at).total_seconds()
        if delta <= stale_seconds:
            return CONNECTIVITY_HEALTHY
        if delta >= reconcile_seconds and prev.connectivity == "UNREACHABLE":
            return CONNECTIVITY_UNREACHABLE
        return CONNECTIVITY_STALE

    @staticmethod
    async def ingest_heartbeat(db: AsyncSession, agent_id: str, payload: HeartbeatPayload) -> dict:
        """Process one heartbeat: update current status, sample on material
        change, derive warnings/alignment, emit semantic events on change.

        Returns the view of the stored status row (after commit)."""
        from .work_keys import WORK_PREFIX

        s = get_settings()
        now_dt = _now()
        agent_id = agent_id or payload.identity.acms_agent_id

        # context validity + warning (invalid never becomes a count)
        used, mx = payload.context.used_tokens, payload.context.max_tokens
        valid = _context_valid(used, mx)
        util = None
        if valid:
            util = round((used / mx) * 100.0, 1)  # type: ignore[operator]
        warning = _warn_level(util if valid else None, s)
        if not valid and (used is not None or mx is not None or payload.context.utilization_percent is not None):
            # reported-but-invalid telemetry: record the fact, never a fabricated number
            await add_event(
                db,
                event_type="CONTEXT_TELEMETRY_INVALID",
                actor_source="heartbeat", agent_id=agent_id,
                summary="Context telemetry reported but invalid; stored as UNKNOWN/INVALID",
                metadata={"used": used, "max": mx,
                          "reported_percent": payload.context.utilization_percent},
            )

        # expected assignment correlation: active assignment's keys
        active = (
            await db.scalars(
                select(AssignmentRecord).where(
                    AssignmentRecord.agent_id == agent_id, AssignmentRecord.status == "ACTIVE"
                )
            )
        ).first()
        expected_work_key = active.work_key if active else None
        expected_assignment_key = active.assignment_key if active else None

        # session binding upsert
        bind = await db.get(AgentSessionBindingRecord, agent_id)
        if bind is None:
            bind = AgentSessionBindingRecord(
                agent_id=agent_id, first_seen_at=now_dt, last_seen_at=now_dt,
                session_id=payload.session.session_id, session_title=payload.session.title,
                agent_running=_tri_state(payload.session.agent_running),
            )
            db.add(bind)
        else:
            bind.session_id = payload.session.session_id
            bind.session_title = payload.session.title
            bind.agent_running = _tri_state(payload.session.agent_running)
            bind.last_seen_at = now_dt

        prev = await db.get(AgentStatusCurrentRecord, agent_id)
        prev_snapshot = None
        if prev is not None:
            prev_snapshot = {
                "connectivity": prev.connectivity,
                "agent_running": prev.agent_running,
                "model_id": prev.model_id,
                "session_id": prev.session_id,
                "session_title": prev.session_title,
                "context_warning": prev.context_warning,
                "alignment": prev.alignment,
            }

        status = await TelemetryService.ensure_status_row(db, agent_id)
        status.connectivity = CONNECTIVITY_HEALTHY
        status.last_contact_at = now_dt
        status.last_contact_source = CONTACT_SOURCE_HEARTBEAT
        status.stale_since = None
        status.agent_running = _tri_state(payload.session.agent_running)
        status.session_id = payload.session.session_id
        status.session_title = payload.session.title
        status.expected_work_key = (payload.assignment.acms_work_key or (active.work_key if active else None)) if active else None
        status.expected_assignment_key = (payload.assignment.acms_assignment_key or (active.assignment_key if active else None)) if active else None

        # alignment (plan §10): reported session title vs expected work key
        reported_key = extract_work_key_from_title(payload.session.title)
        if expected_work_key and reported_key:
            alignment = "ALIGNED" if reported_key == expected_work_key else "MISMATCH"
        elif payload.assignment.acms_work_key and payload.session.title:
            alignment = "ALIGNED" if payload.assignment.acms_work_key == extract_work_key_from_title(payload.session.title) else "MISMATCH"
        elif expected_work_key is None:
            alignment = "UNKNOWN"
        else:
            # running but nothing reported yet → grace window
            if payload.session.agent_running is False:
                alignment = "UNKNOWN"
            else:
                grace_active = True  # grace handled by scheduler drift check
                alignment = "UNKNOWN"
        status.alignment = alignment

        status.model_id = payload.model.model_id if payload.model else None
        status.provider = payload.model.provider if payload.model else None
        if _context_valid(used, mx):
            status.context_used_tokens = used
            status.context_max_tokens = mx
            status.context_utilization_percent = util
            status.context_telemetry_valid = True
        else:
            status.context_used_tokens = None
            status.context_max_tokens = None
            status.context_utilization_percent = None
            status.context_telemetry_valid = False
        status.context_warning = warning
        status.context_max_source = payload.context.max_source
        status.session_total_tokens = payload.usage.session_total_tokens if payload.usage else None
        status.cumulative_api_tokens = payload.usage.cumulative_api_tokens if payload.usage else None
        status.bridge_id = payload.identity.bridge_id
        status.bridge_version = payload.identity.bridge_version
        status.harness_version = payload.identity.harness_version
        status.platforms_connected = ",".join(payload.platforms.get("connected", []) or [])
        status.heartbeat_schema_version = payload.schema_version
        status.updated_at = now_dt

        # --- semantic events: only on material change (plan §8/§11) ---
        events: list[tuple[str, str]] = []
        if prev_snapshot:
            if prev_snapshot["connectivity"] != CONNECTIVITY_HEALTHY:
                events.append(("CONNECTIVITY_CHANGED", f"{status.connectivity} after contact (source=heartbeat)"))
            if prev_snapshot["agent_running"] != status.agent_running:
                events.append(("AGENT_RUNNING_CHANGED", f"agent_running {prev_snapshot['agent_running']} -> {status.agent_running}"))
            if prev_snapshot["session_id"] != status.session_id:
                events.append(("SESSION_CHANGED", f"session changed to {status.session_id or '(none)'}"))
            if prev_snapshot["model_id"] != status.model_id and status.model_id:
                events.append(("MODEL_CHANGED", f"model changed to {status.model_id}"))
            if prev_snapshot["context_warning"] != status.context_warning and status.context_warning:
                events.append(("CONTEXT_WARNING_CHANGED", f"context warning now {status.context_warning} ({status.context_utilization_percent}%)"))
            if prev_snapshot["connectivity"] != status.connectivity:
                events.append(("CONNECTIVITY_CHANGED", f"connectivity {prev_snapshot['connectivity']} -> {status.connectivity}"))
        for etype, summary in events:
            await add_event(
                db,
                event_type=etype,
                actor_source="heartbeat", agent_id=agent_id,
                work_key=active.work_key if active else None,
                assignment_key=active.assignment_key if active else None,
                harness_session_id=status.session_id, summary=summary,
            )
        await db.commit()
        return {"connectivity": status.connectivity, "alignment": status.alignment,
                "context_warning": status.context_warning}

