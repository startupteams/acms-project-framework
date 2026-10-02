"""Agent-scoped usage rollups for the fleet table (STEA-004 §19 columns
'Tokens today' / 'Cost today').

Source of truth: execution_sessions (agent_id + cumulative_api_tokens +
estimated cost) — the same telemetry the Usage page uses. Cloud cost today
per agent joins cost_attribution via session_id. Unknown cost stays None —
never fabricated.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession


async def agent_usage_today(db: AsyncSession) -> dict[str, dict]:
    """{agent_id: {tokens: int|None, cost: float}} for sessions started today."""
    from ..economics_models import CostAttributionRecord
    from ..memory_models import ExecutionSessionRecord

    day_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    rows = (await db.execute(
        select(ExecutionSessionRecord.agent_id,
               func.sum(func.coalesce(ExecutionSessionRecord.cumulative_api_tokens, 0)))
        .where(ExecutionSessionRecord.started_at >= day_start)
        .group_by(ExecutionSessionRecord.agent_id))).all()
    tokens = {r[0]: int(r[1] or 0) for r in rows}
    # cloud cost per agent via session linkage (only rows with a session id)
    cost_rows = (await db.execute(
        select(ExecutionSessionRecord.agent_id,
               func.sum(func.coalesce(CostAttributionRecord.api_cost_usd, 0.0)))
        .join(CostAttributionRecord, CostAttributionRecord.session_id == ExecutionSessionRecord.session_id)
        .where(ExecutionSessionRecord.started_at >= day_start)
        .group_by(ExecutionSessionRecord.agent_id))).all()
    costs = {r[0]: round(float(r[1] or 0.0), 6) for r in cost_rows}
    out: dict[str, dict] = {}
    for agent_id, tok in tokens.items():
        out[agent_id] = {"tokens": tok, "cost": costs.get(agent_id)}
    for agent_id, cost in costs.items():
        out.setdefault(agent_id, {"tokens": None, "cost": cost})
    return out