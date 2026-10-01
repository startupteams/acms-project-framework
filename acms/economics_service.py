"""Engineering-economics service (REV2 plan §9B).

Canonical metrics (failed/retried cost included, never divided away):
  cost_per_accepted_pr        = total cost / PRs with outcome_state=ACCEPTED
  cost_per_accepted_requirement = total cost / distinct accepted requirement refs

Honesty rules carried over from REQ-054/058:
  - ACCEPTED counts only explicit acceptance (accepted_at set); MERGED alone
    never becomes accepted.
  - Unknown human time stays unknown (never 0).
  - Unknown cost stays unknown: entries with null api_cost_usd are counted
    separately, not coerced to zero.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .economics_models import (
    OUTCOME_ACCEPTED,
    CostAttributionRecord,
    PrOutcomeRecord,
    RequirementLinkRecord,
)
from .telemetry_service import add_event


def _now() -> datetime:
    return datetime.now(timezone.utc)


class EconomicsReport:
    """Model/provider-level rollup of the canonical metrics."""

    def __init__(self) -> None:
        self.total_cost_usd = 0.0
        self.entries_with_unknown_cost = 0
        self.failed_run_cost_usd = 0.0
        self.total_tokens = 0
        self.outcomes_total = 0
        self.outcomes_accepted = 0
        self.outcomes_merged = 0
        self.outcomes_reverted = 0
        self.outcomes_rework = 0
        self.accepted_requirements: set[str] = set()
        self.linked_requirements: set[str] = set()
        self.human_review_minutes = 0.0
        self.human_repair_minutes = 0.0
        self.human_time_unknown_count = 0
        self.first_pass_count = 0
        self.first_pass_known = 0

    # ------------------------------------------------------------- math
    @property
    def cost_per_accepted_pr(self) -> float | None:
        if self.outcomes_accepted == 0:
            return None
        return self.total_cost_usd / self.outcomes_accepted

    @property
    def cost_per_accepted_requirement(self) -> float | None:
        if not self.accepted_requirements:
            return None
        return self.total_cost_usd / len(self.accepted_requirements)

    @property
    def first_pass_acceptance_rate(self) -> float | None:
        if self.first_pass_known == 0:
            return None
        return self.first_pass_count / self.first_pass_known

    def to_dict(self, **filters: Any) -> dict:
        return {
            "filters": filters or None,
            "total_cost_usd": round(self.total_cost_usd, 6),
            "entries_with_unknown_cost": self.entries_with_unknown_cost,
            "failed_run_cost_usd": round(self.failed_run_cost_usd, 6),
            "total_tokens": self.total_tokens,
            "outcomes": {
                "total": self.outcomes_total,
                "accepted": self.outcomes_accepted,
                "merged": self.outcomes_merged,
                "reverted": self.outcomes_reverted,
                "rework_required": self.outcomes_rework,
            },
            "requirements": {
                "linked_distinct": len(self.linked_requirements),
                "accepted_distinct": len(self.accepted_requirements),
            },
            "cost_per_accepted_pr": (round(self.cost_per_accepted_pr, 6)
                                     if self.cost_per_accepted_pr is not None else None),
            "cost_per_accepted_requirement": (
                round(self.cost_per_accepted_requirement, 6)
                if self.cost_per_accepted_requirement is not None else None),
            "first_pass_acceptance_rate": (round(self.first_pass_acceptance_rate, 4)
                                           if self.first_pass_acceptance_rate is not None else None),
            "human_review_minutes": (self.human_review_minutes or None),
            "human_repair_minutes": (self.human_repair_minutes or None),
            "human_time_unknown_count": self.human_time_unknown_count,
        }


async def build_report(db: AsyncSession, *, model_id: str | None = None,
                       provider: str | None = None, repository: str | None = None,
                       task_category: str | None = None,
                       work_item_id: str | None = None,
                       date_from: date | None = None,
                       date_to: date | None = None) -> EconomicsReport:
    """Aggregate the canonical metrics with optional filters (REV2 §9B.6)."""
    outcomes = (await db.execute(select(PrOutcomeRecord))).scalars().all()
    costs = (await db.execute(select(CostAttributionRecord))).scalars().all()
    links = (await db.execute(select(RequirementLinkRecord))).scalars().all()

    by_outcome: dict[str, list[CostAttributionRecord]] = {}
    for c in costs:
        by_outcome.setdefault(c.outcome_id, []).append(c)

    def _match(o: PrOutcomeRecord) -> bool:
        if model_id and o.model_id != model_id:
            return False
        if provider and o.provider != provider:
            return False
        if repository and o.repository != repository:
            return False
        if task_category and o.task_category != task_category:
            return False
        if work_item_id and o.work_item_id != work_item_id:
            return False
        if date_from and o.created_at and o.created_at.date() < date_from:
            return False
        if date_to and o.created_at and o.created_at.date() > date_to:
            return False
        return True

    matched = {o.outcome_id: o for o in outcomes if _match(o)}
    links_by_outcome: dict[str, list[RequirementLinkRecord]] = {}
    for l in links:
        links_by_outcome.setdefault(l.outcome_id, []).append(l)

    rep = EconomicsReport()
    for oid, o in matched.items():
        rep.outcomes_total += 1
        if o.outcome_state == OUTCOME_ACCEPTED:
            rep.outcomes_accepted += 1
            for l in links_by_outcome.get(oid, []):
                rep.accepted_requirements.add(l.requirement_ref)
        elif o.outcome_state == "MERGED":
            rep.outcomes_merged += 1
        elif o.outcome_state == "REVERTED":
            rep.outcomes_reverted += 1
        elif o.outcome_state == "REWORK_REQUIRED":
            rep.outcomes_rework += 1
        for l in links_by_outcome.get(oid, []):
            rep.linked_requirements.add(l.requirement_ref)
        if o.human_review_minutes is None and o.human_repair_minutes is None:
            rep.human_time_unknown_count += 1
        else:
            rep.human_review_minutes += o.human_review_minutes or 0.0
            rep.human_repair_minutes += o.human_repair_minutes or 0.0
        if o.first_pass is not None:
            rep.first_pass_known += 1
            rep.first_pass_count += 1 if o.first_pass else 0
        for c in by_outcome.get(oid, []):
            if c.api_cost_usd is None:
                rep.entries_with_unknown_cost += 1
            else:
                rep.total_cost_usd += c.api_cost_usd
                if c.failed_run:
                    rep.failed_run_cost_usd += c.api_cost_usd
            toks = (c.input_tokens or 0) + (c.output_tokens or 0)
            rep.total_tokens += toks

    return rep


async def link_requirements(db: AsyncSession, outcome_id: str,
                            requirement_refs: list[str]) -> list[str]:
    """Idempotent many-to-many link of one PR outcome to requirement refs."""
    rows = (await db.execute(
        select(RequirementLinkRecord).where(RequirementLinkRecord.outcome_id == outcome_id)
    )).scalars().all()
    have = {r.requirement_ref for r in rows}
    added = []
    for ref in requirement_refs:
        ref = ref.strip()
        if ref and ref not in have:
            db.add(RequirementLinkRecord(link_id=RequirementLinkRecord.new_id(),
                                         outcome_id=outcome_id, requirement_ref=ref))
            added.append(ref)
    await db.commit()
    return added


async def attach_session_cost(db: AsyncSession, outcome_id: str,
                              session_id: str) -> dict:
    """Create a cost-attribution entry from an execution session's telemetry
    (source=session). Missing fields stay null — never zero-filled."""
    from .memory_models import ExecutionSessionRecord

    s = await db.get(ExecutionSessionRecord, session_id)
    if s is None:
        raise ValueError("session not found")
    entry = CostAttributionRecord(
        entry_id=CostAttributionRecord.new_id(), outcome_id=outcome_id,
        session_id=session_id, model_id=s.model_id, provider=s.provider,
        api_cost_usd=s.estimated_cost_usd,  # blended authoritative estimate
        source="session", failed_run=False,
    )
    db.add(entry)
    await db.commit()
    return {"entry_id": entry.entry_id, "api_cost_usd": entry.api_cost_usd}


async def set_outcome_state(db: AsyncSession, outcome: PrOutcomeRecord, *,
                            new_state: str, actor: str = "api",
                            accepted_by: str | None = None) -> None:
    """Transition outcome state. ACCEPTED requires an explicit accepted_by —
    the human/product acceptance event (never inferred from MERGED)."""
    from .economics_models import OUTCOME_STATES

    if new_state not in OUTCOME_STATES:
        raise ValueError(f"invalid outcome state: {new_state}")
    if new_state == OUTCOME_ACCEPTED and not accepted_by:
        raise ValueError("ACCEPTED requires accepted_by (explicit human acceptance)")
    prev = outcome.outcome_state
    outcome.outcome_state = new_state
    if new_state == OUTCOME_ACCEPTED:
        outcome.accepted_at = _now()
        outcome.accepted_by = accepted_by
    await add_event(db, event_type="PR_OUTCOME_STATE", actor_source="economics_service",
                    agent_id=outcome.agent_id or "",
                    summary=f"Outcome {outcome.repository or ''}#{outcome.pr_number or outcome.outcome_id[:8]} "
                            f"{prev} -> {new_state}",
                    metadata={"outcome_id": outcome.outcome_id, "prior_state": prev,
                              "new_state": new_state, "actor": actor})
    await db.commit()


def commits_json(shas: list[str]) -> str:
    return json.dumps(shas)


def commits_list(raw: str | None) -> list[str]:
    return json.loads(raw) if raw else []


async def record_execution_usage(db: AsyncSession, *, task, session_id: str | None,
                                 usage: dict, completed_at) -> dict | None:
    """Record run usage for cost attribution (STEA-004 plan §26).

    Uses the execution task's usage_reference (worker-reported input/output
    tokens). Cloud cost stays null until LiteLLM SpendLogs attribution runs —
    tokens are real, cost is honest-null (never $0.00 fabrication). No outcome
    link yet → the entry is stored with a synthetic outcome_id derived from the
    task so it survives until the §26 pipeline links it to pr_outcomes.
    """
    if not usage and session_id is None:
        return None
    from .economics_models import CostAttributionRecord

    entry = CostAttributionRecord(
        entry_id=CostAttributionRecord.new_id(),
        outcome_id=f"task:{task.task_id}",
        session_id=session_id,
        model_id=task.effective_model,
        provider="local" if task.effective_model else None,
        input_tokens=usage.get("input_tokens"),
        output_tokens=usage.get("output_tokens"),
        cached_input_tokens=usage.get("cached_input_tokens"),
        api_cost_usd=None,  # honest null: attribution via SpendLogs is §26
        source="session",
        failed_run=task.status == "FAILED",
        created_at=completed_at,
    )
    db.add(entry)
    await db.commit()
    await db.refresh(entry)
    return {"entry_id": entry.entry_id}
