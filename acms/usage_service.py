"""Usage / economics UI data (window-5 plan §12.2/§12.3).

The one practical question the Usage page answers honestly:
> What did this body of work cost, and what useful result did we get?

Sources: execution_sessions (cost/token telemetry + model_id) + pr_outcomes
(objective PR states). UNKNOWN cost is never converted to zero (§12.2).
Filters (§12.3): work item, agent, model/provider, date range.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .economics_models import PrOutcomeRecord
from .memory_models import ExecutionSessionRecord


async def usage_summary(db: AsyncSession, *, work_item_id: str | None = None,
                        agent_id: str | None = None, model_id: str | None = None,
                        since_days: int | None = None) -> dict[str, Any]:
    since = None
    if since_days:
        since = datetime.now(timezone.utc) - timedelta(days=since_days)

    q = select(ExecutionSessionRecord).order_by(ExecutionSessionRecord.started_at.desc()).limit(2000)
    rows = (await db.scalars(q)).all()

    def keep(r: ExecutionSessionRecord) -> bool:
        if work_item_id and r.work_item_id != work_item_id:
            return False
        if agent_id and r.agent_id != agent_id:
            return False
        if model_id and (r.model_id or "") != model_id:
            return False
        if since and r.started_at and r.started_at < since:
            return False
        return True

    sessions = [r for r in rows if keep(r)]
    total_est = 0.0
    total_cloud = 0.0
    known = 0
    unknown = 0
    tokens = 0
    by_model: dict[str, dict[str, Any]] = {}
    for s in sessions:
        tokens += (s.cumulative_api_tokens or 0)
        cost = s.estimated_cost_usd
        if cost is None:
            unknown += 1
        else:
            known += 1
            total_est += float(cost)
            m = s.model_id or "unknown-model-identity"
            bucket = by_model.setdefault(m, {"sessions": 0, "est_cost_usd": 0.0,
                                             "api_tokens": 0, "unknown_cost": 0})
            bucket["sessions"] += 1
            bucket["est_cost_usd"] += float(cost)
            bucket["api_tokens"] += (s.cumulative_api_tokens or 0)
        if s.estimated_cloud_cost_usd is not None:
            total_cloud += float(s.estimated_cloud_cost_usd)

    # outcomes (what useful result did we get — objective only)
    outcome_q = select(PrOutcomeRecord)
    if work_item_id:
        outcome_q = outcome_q.where(PrOutcomeRecord.work_item_id == work_item_id)
    if agent_id:
        outcome_q = outcome_q.where(PrOutcomeRecord.agent_id == agent_id)
    if model_id:
        outcome_q = outcome_q.where(PrOutcomeRecord.model_id == model_id)
    outcomes = (await db.scalars(outcome_q.order_by(PrOutcomeRecord.outcome_id.desc()).limit(1000))).all()
    outcome_states: dict[str, int] = {}
    for o in outcomes:
        outcome_states[o.outcome_state] = outcome_states.get(o.outcome_state, 0) + 1

    # cost per accepted PR — NULL when zero accepted (never a fabricated zero)
    accepted = outcome_states.get("ACCEPTED", 0)
    cost_per_accepted = round(total_est / accepted, 4) if accepted else None

    return {
        "filters": {"work_item_id": work_item_id, "agent_id": agent_id,
                    "model_id": model_id, "since_days": since_days},
        "sessions": {"count": len(sessions), "cost_known": known, "cost_unknown": unknown,
                     "est_cost_usd": round(total_est, 4) if known else None,
                     "est_cloud_cost_usd": round(total_cloud, 4) if total_cloud else 0.0,
                     "api_tokens": tokens},
        "by_model": dict(sorted(by_model.items(),
                                key=lambda kv: -kv[1]["est_cost_usd"])),
        "outcomes": {"total": len(outcomes), "states": outcome_states,
                     "accepted": accepted,
                     "cost_per_accepted_pr_usd": cost_per_accepted,
                     "note": ("MERGED is never treated as accepted; acceptance is human"
                              if outcome_states.get("MERGED") else None)},
        "unknown_cost_policy": "unknown cost stays UNKNOWN — never converted to zero",
    }