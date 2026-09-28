"""Per-Work budget service (ACMS-REQ-059; REV2 plan §4).

Budget = execution economics on a Work Item: soft threshold produces a
semantic event + advisory (checkpoint/review/reroute recommendation); hard
threshold BLOCKS NEW cloud execution but never interrupts in-flight atomic
work. Overrides are explicit and audited.

Cost honesty (REV2 §4.1): only sessions with authoritative cost telemetry
contribute to rollups. Sessions with UNKNOWN cost (null estimated_cost_usd)
are counted separately — never coerced to zero, never fabricated.

Local inference: usage may be recorded (tokens/seconds), but an authoritative
local USD-equivalent does not yet exist (FUTURE_WORK FW-LLM-LOCAL-COST).
``estimated_local_cost_usd`` stays nullable and is excluded from budget math
until a source exists.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .memory_models import (
    BUDGET_HARD_EXCEEDED,
    BUDGET_OK,
    BUDGET_SOFT_EXCEEDED,
    ExecutionSessionRecord,
    WorkBudgetRecord,
)
from .telemetry_service import add_event

BUDGET_STATES = (BUDGET_OK, BUDGET_SOFT_EXCEEDED, BUDGET_HARD_EXCEEDED)


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class BudgetRollup:
    """Aggregated session telemetry for one Work Item (REV2 §4.2).

    ``sessions_unknown_cost`` counts sessions with no authoritative cost
    figure — they are excluded from cost sums, not treated as zero.
    """

    work_item_id: str
    session_count: int = 0
    open_sessions: int = 0
    cumulative_api_tokens: int = 0
    estimated_cost_usd: float = 0.0
    estimated_cloud_cost_usd: float = 0.0
    # local usage is tracked, but USD-equivalent stays unknown
    sessions_with_local_usage: int = 0
    local_cost_usd: float | None = None
    # sessions with NO cost fields at all (fully unknown)
    sessions_unknown_cost: int = 0
    # sessions with SOME cost fields but no blended total (e.g. cloud only) —
    # their true total is unknown ≥ what was reported; surfaced, never summed in
    sessions_partial_cost: int = 0
    model_breakdown: dict[str, dict] | None = None
    budget_state: str = BUDGET_OK

    def to_dict(self) -> dict:
        return {
            "work_item_id": self.work_item_id,
            "session_count": self.session_count,
            "open_sessions": self.open_sessions,
            "cumulative_api_tokens": self.cumulative_api_tokens,
            "estimated_cost_usd": self.estimated_cost_usd,
            "estimated_cloud_cost_usd": self.estimated_cloud_cost_usd,
            "sessions_with_local_usage": self.sessions_with_local_usage,
            "local_cost_usd": self.local_cost_usd,
            "sessions_unknown_cost": self.sessions_unknown_cost,
            "sessions_partial_cost": self.sessions_partial_cost,
            "model_breakdown": self.model_breakdown,
            "budget_state": self.budget_state,
        }


def evaluate_state(actual_cost: float | None, soft: float | None, hard: float | None) -> str:
    """State from authoritative actual spend vs thresholds.

    UNKNOWN actual (None) → OK (never fabricates a threshold crossing).
    """
    if actual_cost is None:
        return BUDGET_OK
    if hard is not None and actual_cost > hard:
        return BUDGET_HARD_EXCEEDED
    if soft is not None and actual_cost > soft:
        return BUDGET_SOFT_EXCEEDED
    return BUDGET_OK


def execution_blocked(budget: WorkBudgetRecord, actual_cost: float | None) -> bool:
    """Hard-threshold gate for NEW cloud execution (REQ-059).

    True only when: hard threshold set AND actual cost exceeds it AND no
    active audited override. Never blocks on unknown cost, never applies to
    in-flight atomic work (callers enforce that).
    """
    if budget.hard_budget_usd is None:
        return False
    if actual_cost is None:
        return False  # unknown cost never triggers a block
    if actual_cost <= budget.hard_budget_usd:
        return False
    return not bool(budget.override_active)


def evaluate_budget_state(budget: WorkBudgetRecord, rollup: BudgetRollup) -> str:
    """Budget state considers override: with an active audited override the
    hard crossing is recorded but execution is permitted (state stays
    HARD_EXCEEDED — the fact is not erased by the override)."""
    return evaluate_state(rollup.estimated_cost_usd, budget.soft_budget_usd,
                          budget.hard_budget_usd)


async def get_budget(db: AsyncSession, work_item_id: str) -> WorkBudgetRecord | None:
    return await db.get(WorkBudgetRecord, work_item_id)


async def set_budget(db: AsyncSession, work_item_id: str, *,
                     soft_budget_usd: float | None,
                     hard_budget_usd: float | None,
                     actor: str = "api") -> tuple[WorkBudgetRecord, bool]:
    """Create/update the budget row for a Work Item. Setting thresholds clears
    any active override (documented one-shot semantics)."""
    from .work_models import WorkItemRecord

    item = await db.get(WorkItemRecord, work_item_id)
    if item is None:
        raise ValueError("work item not found")

    budget = await db.get(WorkBudgetRecord, work_item_id)
    created = budget is None
    if budget is None:
        budget = WorkBudgetRecord(work_item_id=work_item_id)
        db.add(budget)
    budget.soft_budget_usd = soft_budget_usd
    budget.hard_budget_usd = hard_budget_usd
    budget.override_active = False
    budget.override_by = None
    budget.override_reason = None
    budget.overridden_at = None

    rollup = await work_budget_rollup(db, work_item_id)
    budget.budget_state = evaluate_budget_state(budget, rollup)
    await add_event(
        db, event_type="BUDGET_SET", actor_source="budget_service", agent_id="",
        summary=f"Budget {'set' if created else 'updated'} for {item.work_key or work_item_id}: "
                f"soft={soft_budget_usd} hard={hard_budget_usd} (override cleared)",
        metadata={"work_item_id": work_item_id,
                  "soft_budget_usd": soft_budget_usd, "hard_budget_usd": hard_budget_usd,
                  "budget_state": budget.budget_state},
    )
    await db.commit()
    return budget, created


async def override_hard_limit(db: AsyncSession, work_item_id: str, *,
                              override_by: str, reason: str) -> WorkBudgetRecord:
    """Audited one-shot override of a hard block (Administrator-only at API layer)."""
    from .work_models import WorkItemRecord

    budget = await db.get(WorkBudgetRecord, work_item_id)
    if budget is None:
        raise ValueError("budget not set for work item")
    item = await db.get(WorkItemRecord, work_item_id)
    budget.override_active = True
    budget.override_by = override_by
    budget.override_reason = reason
    budget.overridden_at = _now()
    await add_event(
        db, event_type="BUDGET_OVERRIDE", actor_source="budget_service", agent_id="",
        summary=f"Hard-budget override by {override_by} for {item.work_key or work_item_id}: {reason[:200]}",
        metadata={"work_item_id": work_item_id, "override_by": override_by,
                  "reason": reason, "budget_state": budget.budget_state},
    )
    await db.commit()
    return budget


async def check_new_execution_allowed(db: AsyncSession, work_item_id: str) -> dict:
    """Gate for NEW cloud model execution on this Work (REV2 §4.3).

    Returns an honest verdict dict: allowed/blocked + the facts used.
    Never blocks on unknown cost; never fabricates.
    """
    budget = await db.get(WorkBudgetRecord, work_item_id)
    if budget is None:
        return {"allowed": True, "reason": "no_budget_set", "budget_state": None,
                "rollup": None}
    rollup = await work_budget_rollup(db, work_item_id)
    blocked = execution_blocked(budget, rollup.estimated_cost_usd)
    if blocked:
        return {"allowed": False, "reason": "hard_budget_exceeded",
                "budget_state": budget.budget_state,
                "rollup": rollup.to_dict(),
                "override": {"active": bool(budget.override_active),
                             "by": budget.override_by,
                             "reason": budget.override_reason,
                             "at": budget.overridden_at.isoformat() if budget.overridden_at else None}}
    return {"allowed": True, "reason": "within_budget", "budget_state": budget.budget_state,
            "rollup": rollup.to_dict()}


async def work_budget_rollup(db: AsyncSession, work_item_id: str) -> BudgetRollup:
    """Aggregate session telemetry for a Work Item without double counting.

    One session contributes exactly once (its own cumulative fields are the
    authority). Sessions without cost telemetry increment
    ``sessions_unknown_cost`` and are excluded from cost sums.
    """
    rows = (await db.execute(
        select(ExecutionSessionRecord).where(
            ExecutionSessionRecord.work_item_id == work_item_id)
        .order_by(ExecutionSessionRecord.started_at)
    )).scalars().all()

    rollup = BudgetRollup(work_item_id=work_item_id)
    breakdown: dict[str, dict] = {}
    for s in rows:
        rollup.session_count += 1
        if s.status != "CLOSED":
            rollup.open_sessions += 1
        if s.cumulative_api_tokens:
            rollup.cumulative_api_tokens += s.cumulative_api_tokens
        # authoritative cloud cost: blended estimate (legacy field) or explicit split
        cost = s.estimated_cost_usd
        cloud = s.estimated_cloud_cost_usd
        if cost is not None:
            rollup.estimated_cost_usd += cost
        if cloud is not None:
            rollup.estimated_cloud_cost_usd += cloud
        if cost is None and cloud is None:
            rollup.sessions_unknown_cost += 1
        elif cost is None:
            # some cost fields present but no blended total: true total unknown
            rollup.sessions_partial_cost += 1
        if s.estimated_local_cost_usd is not None:
            rollup.sessions_with_local_usage += 1
        if s.model_id:
            entry = breakdown.setdefault(
                s.model_id, {"sessions": 0, "cumulative_api_tokens": 0,
                             "estimated_cost_usd": 0.0, "sessions_unknown_cost": 0})
            entry["sessions"] += 1
            if s.cumulative_api_tokens:
                entry["cumulative_api_tokens"] += s.cumulative_api_tokens
            if cost is not None:
                entry["estimated_cost_usd"] += cost
            else:
                entry["sessions_unknown_cost"] += 1
    rollup.model_breakdown = breakdown or None
    budget = await db.get(WorkBudgetRecord, work_item_id)
    if budget is not None:
        rollup.budget_state = evaluate_budget_state(budget, rollup)
    return rollup


async def refresh_budget_state(db: AsyncSession, work_item_id: str) -> WorkBudgetRecord | None:
    """Re-evaluate + persist budget_state from current telemetry. Emits the
    REQ-059 soft semantic event exactly once per transition."""
    budget = await db.get(WorkBudgetRecord, work_item_id)
    if budget is None:
        return None
    rollup = await work_budget_rollup(db, work_item_id)
    prev = budget.budget_state
    new_state = evaluate_budget_state(budget, rollup)
    if new_state != prev:
        budget.budget_state = new_state
        if new_state in (BUDGET_SOFT_EXCEEDED, BUDGET_HARD_EXCEEDED):
            from .work_models import WorkItemRecord

            item = await db.get(WorkItemRecord, work_item_id)
            await add_event(
                db, event_type="BUDGET_THRESHOLD_CROSSED",
                actor_source="budget_service", agent_id="",
                summary=f"Budget {new_state} for {item.work_key or work_item_id}: "
                        f"spend ${rollup.estimated_cost_usd:.2f} vs soft "
                        f"{budget.soft_budget_usd} hard {budget.hard_budget_usd}",
                metadata={"work_item_id": work_item_id, "prior_state": prev,
                          "new_state": new_state,
                          "actual_cost_usd": rollup.estimated_cost_usd,
                          "soft_budget_usd": budget.soft_budget_usd,
                          "hard_budget_usd": budget.hard_budget_usd},
            )
        await db.commit()
    return budget
