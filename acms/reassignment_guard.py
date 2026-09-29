"""Work reassignment guardrail (Phase C5 — human direction 2026-09-29).

Policy (recorded here; enforced by ``record_reassignment``):
- The Executive Agent may reassign a Work Item at most
  ``ACMS_REASSIGNMENT_LIMIT`` (default **3**) consecutive times after failures.
- Beyond the limit: automatic reassignment STOPS; a durable
  ``REASSIGNMENT_LIMIT_REACHED`` event is written (Attention-eligible, high) and
  the decision returns to a human.
- Every reassignment attempt records WHY it is different — ``reason`` plus the
  ``changed_dimension`` (different_agent | revised_instructions | additional_context
  | different_strategy | human_clarification). A retry whose changed_dimension is
  ``none`` is refused (an identical retry is not a reassignment).
- The old subjective "<60% instruction match" rule is NOT automated. Failure is
  eventually judged against the Work Item's explicit acceptance criteria
  (REQ-013 handoffs + review-loop policy; see docs/architecture/
  MEASURABLE_GOAL_WORK_LIFECYCLE.md §4).

Data model stays in the durable audit log (agent_events) — no second mutable
history system (human preference; same pattern as ARM runtime_events).
Counting queries REASSIGNMENT_RECORDED events for the work item.
"""
from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .telemetry_service import add_event

CHANGED_DIMENSIONS = (
    "different_agent",
    "revised_instructions",
    "additional_context",
    "different_strategy",
    "human_clarification",
    "none",  # refused
)

DEFAULT_LIMIT = 3


async def _count_reassignments(db: AsyncSession, work_item_id: str) -> int:
    from .telemetry_models import AgentEventRecord

    n = (await db.execute(
        select(func.count()).select_from(AgentEventRecord).where(
            AgentEventRecord.event_type == "REASSIGNMENT_RECORDED",
            AgentEventRecord.work_key == f"work:{work_item_id}",
        )
    )).scalar() or 0
    return int(n)


async def record_reassignment(db: AsyncSession, *, work_item_id: str, work_key: str | None,
                              from_agent_id: str | None, to_agent_id: str | None,
                              reason: str, changed_dimension: str,
                              limit: int = DEFAULT_LIMIT) -> dict:
    """Record one Executive-Agent reassignment attempt and enforce the limit.

    Returns {"allowed": bool, "count": n, "limit": limit, "limit_event_emitted": bool}.
    Commits on both allowed and refused paths (the refusal itself is a durable fact).
    """
    if changed_dimension not in CHANGED_DIMENSIONS:
        raise ValueError(f"changed_dimension must be one of {CHANGED_DIMENSIONS}")
    if changed_dimension == "none":
        # Distinct event type: identical retries are NOT reassignments and must
        # not consume the limit budget.
        await add_event(
            db, event_type="REASSIGNMENT_REFUSED", actor_source="reassignment_guard",
            agent_id=to_agent_id or from_agent_id, work_key=f"work:{work_item_id}",
            summary=f"Reassignment refused: identical retry is not a reassignment "
                    f"(changed_dimension=none) on {work_key or work_item_id[:8]}",
            metadata={"work_item_id": work_item_id, "reason": reason[:300],
                      "changed_dimension": "none"},
        )
        await db.commit()
        return {"allowed": False, "count": await _count_reassignments(db, work_item_id),
                "limit": limit, "limit_event_emitted": False}

    count = await _count_reassignments(db, work_item_id)
    if count >= limit:
        # Limit reached: do NOT count this as a new reassignment; emit the
        # durable stop event (Attention high) once per crossing.
        already = (await db.execute(
            select(func.count()).select_from(_event_model()).where(
                _event_model().event_type == "REASSIGNMENT_LIMIT_REACHED",
                _event_model().work_key == f"work:{work_item_id}",
            )
        )).scalar() or 0
        if not already:
            await add_event(
                db, event_type="REASSIGNMENT_LIMIT_REACHED", actor_source="reassignment_guard",
                agent_id=to_agent_id or from_agent_id, work_key=f"work:{work_item_id}",
                summary=f"Reassignment limit ({limit}) reached on {work_key or work_item_id[:8]} "
                        f"— automatic reassignment stopped; human decision required",
                metadata={"work_item_id": work_item_id, "count": count, "limit": limit,
                          "attempted_reason": reason[:300],
                          "attempted_dimension": changed_dimension},
            )
        await db.commit()
        return {"allowed": False, "count": count, "limit": limit, "limit_event_emitted": True}

    await add_event(
        db, event_type="REASSIGNMENT_RECORDED", actor_source="reassignment_guard",
        agent_id=to_agent_id or from_agent_id, work_key=f"work:{work_item_id}",
        summary=f"Reassignment #{count + 1} on {work_key or work_item_id[:8]}: "
                f"{changed_dimension} ({reason[:140]})",
        metadata={"work_item_id": work_item_id, "count": count + 1, "limit": limit,
                  "reason": reason[:300], "changed_dimension": changed_dimension,
                  "from_agent_id": from_agent_id, "to_agent_id": to_agent_id},
    )
    await db.commit()
    new_count = count + 1
    emitted = False
    if new_count >= limit:
        await add_event(
            db, event_type="REASSIGNMENT_LIMIT_REACHED", actor_source="reassignment_guard",
            agent_id=to_agent_id or from_agent_id, work_key=f"work:{work_item_id}",
            summary=f"Reassignment limit ({limit}) reached on {work_key or work_item_id[:8]} "
                    f"— automatic reassignment stopped; human decision required",
            metadata={"work_item_id": work_item_id, "count": new_count, "limit": limit},
        )
        await db.commit()
        emitted = True
    return {"allowed": True, "count": new_count, "limit": limit, "limit_event_emitted": emitted}


def _event_model():
    from .telemetry_models import AgentEventRecord

    return AgentEventRecord
