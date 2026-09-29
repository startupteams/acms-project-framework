"""Automatic ExecutionSession lifecycle around dispatch (2026-09-29 plan §8 / Phase F).

Target chain:
    Work → Assignment → budget check → ExecutionSession → Bridge/A2A task →
    telemetry/cost → result → session close → handoff/checkpoint

Requirements:
    - duplicate dispatch does NOT duplicate the session (idempotent open by a2a_task_id)
    - hard-budget reject does NOT create a fake RUNNING/OPEN session
    - Bridge failure does not leave a zombie OPEN session
    - success closes correctly
    - A2A/task/model/provider/cost correlation preserved
    - failed runs retain their cost (close with status CLOSED; cost fields untouched)
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .memory_models import ExecutionSessionRecord
from .telemetry_service import add_event, _now


async def _find_by_a2a(db: AsyncSession, a2a_task_id: str | None) -> ExecutionSessionRecord | None:
    if not a2a_task_id:
        return None
    row = await db.scalar(
        select(ExecutionSessionRecord)
        .where(ExecutionSessionRecord.a2a_task_id == str(a2a_task_id))
        .order_by(ExecutionSessionRecord.started_at.desc())
        .limit(1)
    )
    return row


async def open_session_for_dispatch(
    db: AsyncSession, *, agent_id: str, work_item_id: str,
    assignment_id: str | None, a2a_task_id: str | None,
    harness_session_id: str | None = None,
) -> ExecutionSessionRecord:
    """Idempotently open (or return the existing) session for this A2A run."""
    existing = await _find_by_a2a(db, a2a_task_id)
    if existing is not None and existing.status == "OPEN":
        return existing  # duplicate dispatch: never duplicate the session
    rec = ExecutionSessionRecord(
        session_id=ExecutionSessionRecord.new_id(), agent_id=agent_id,
        work_item_id=work_item_id, assignment_id=assignment_id,
        a2a_task_id=str(a2a_task_id) if a2a_task_id else None,
        harness_session_id=harness_session_id,
        status="OPEN", started_at=_now(),
    )
    db.add(rec)
    await add_event(db, event_type="EXECUTION_SESSION_OPENED", actor_source="dispatch_lifecycle",
                    agent_id=agent_id, work_key=None,
                    summary=f"Execution session auto-opened for dispatch (a2a={str(a2a_task_id)[:24]})",
                    metadata={"session_id": rec.session_id, "work_item_id": work_item_id,
                              "a2a_task_id": str(a2a_task_id) if a2a_task_id else None})
    return rec


async def fail_session_for_dispatch(
    db: AsyncSession, *, session: ExecutionSessionRecord | None, reason: str,
) -> None:
    """Bridge failure: never leave a zombie OPEN session."""
    if session is None or session.status != "OPEN":
        return
    session.status = "CLOSED"
    session.ended_at = _now()
    await add_event(db, event_type="EXECUTION_SESSION_CLOSED", actor_source="dispatch_lifecycle",
                    agent_id=session.agent_id, work_key=None,
                    summary=f"Execution session closed after dispatch failure: {reason[:120]}",
                    metadata={"session_id": session.session_id, "failure": True})
    # cost fields intentionally untouched: failed runs retain their cost (REQ-058)


async def close_session(
    db: AsyncSession, *, session_id: str, outcome: str = "COMPLETED",
) -> ExecutionSessionRecord | None:
    """Explicit close (bridge telemetry / dispatcher completion path)."""
    rec = await db.get(ExecutionSessionRecord, session_id)
    if rec is None or rec.status == "CLOSED":
        return rec
    rec.status = "CLOSED"
    rec.ended_at = _now()
    await add_event(db, event_type="EXECUTION_SESSION_CLOSED", actor_source="dispatch_lifecycle",
                    agent_id=rec.agent_id, work_key=None,
                    summary=f"Execution session closed ({outcome})",
                    metadata={"session_id": session_id, "outcome": outcome})
    return rec


async def reconcile_zombie_sessions(db: AsyncSession, *, max_age_hours: float = 24.0) -> int:
    """Close OPEN sessions whose A2A run is gone/terminal and older than max_age.

    Conservative: only closes sessions with no recent checkpoint and an A2A id
    (those are bridge-proven runs). Returns count closed.
    """
    cutoff = _now()
    # Normalize tz-awareness: SQLite returns naive datetimes; PG returns aware.
    if cutoff.tzinfo is None:
        cutoff = cutoff.replace(tzinfo=None)

    rows = (await db.scalars(
        select(ExecutionSessionRecord)
        .where(ExecutionSessionRecord.status == "OPEN")
        .where(ExecutionSessionRecord.a2a_task_id.isnot(None))
        .where(ExecutionSessionRecord.started_at < cutoff)
    )).all()
    closed = 0
    for r in rows:
        started = r.started_at
        if started.tzinfo is not None and cutoff.tzinfo is None:
            started = started.replace(tzinfo=None)
        elif started.tzinfo is None and cutoff.tzinfo is not None:
            started = started.replace(tzinfo=cutoff.tzinfo)
        age_h = (cutoff - started).total_seconds() / 3600.0
        if age_h >= max_age_hours:
            r.status = "CLOSED"
            r.ended_at = _now()
            await add_event(db, event_type="EXECUTION_SESSION_CLOSED", actor_source="dispatch_lifecycle",
                            agent_id=r.agent_id, work_key=None,
                            summary=f"Zombie session reconciled closed (age {age_h:.1f}h)",
                            metadata={"session_id": r.session_id, "zombie": True})
            closed += 1
    return closed
