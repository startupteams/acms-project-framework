"""Phase L — model bake-off experiment framework (2026-09-29 plan §14).

A bake-off = a CONTROLLED model comparison. Hard requirements:
    - NO material paid model spend without a bounded, pre-registered budget
      (spend_cap_usd) and an explicit authorizer recorded on the experiment.
    - Fairness controls enforced structurally: every candidate gets the same
      starting SHA, plan, acceptance criteria, permissions, and time envelope.
    - Candidates run on disposable branches/worktrees; no experiment agent may
      merge to production (state is tracked; the merge prohibition is
      organizational and enforced by branch protection + review discipline).
    - Tracking per candidate: model/provider, spend, wall time, tests, PR
      outcome, failed attempts, cost per accepted result.

This module provides the experiment registry + budget gate; it does NOT launch
agents. Actual launches go through the normal dispatch path with the budget
pre-registered (REQ-059 hard gate applies on top).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone


class BakeoffError(RuntimeError):
    pass


@dataclass
class Candidate:
    name: str
    model_id: str
    provider: str
    branch: str


@dataclass
class BakeoffExperiment:
    experiment_id: str
    goal: str
    starting_sha: str
    plan_markdown: str
    acceptance_criteria: list[str]
    time_budget_hours: float
    spend_cap_usd: float
    authorized_by: str            # required — no anonymous experiments
    candidates: list[Candidate] = field(default_factory=list)
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    status: str = "REGISTERED"    # REGISTERED | RUNNING | COMPLETED | CANCELLED
    results: list[dict] = field(default_factory=list)


def validate_experiment(exp: BakeoffExperiment) -> None:
    """Structural fairness + budget gates. Raises BakeoffError (fail-closed)."""
    if not exp.authorized_by or exp.authorized_by == "unauthorized":
        raise BakeoffError("experiment requires an explicit authorizer (no uncontrolled spend)")
    if exp.spend_cap_usd <= 0:
        raise BakeoffError("spend_cap_usd must be a positive bound — experiments without a "
                           "pre-registered spend cap are not launched")
    if exp.time_budget_hours <= 0:
        raise BakeoffError("time_budget_hours must be positive")
    if not exp.starting_sha:
        raise BakeoffError("starting_sha required — all candidates must start from the same SHA")
    if not exp.acceptance_criteria:
        raise BakeoffError("acceptance_criteria required — comparisons without shared criteria "
                           "are not evidence")
    if len(exp.candidates) < 2:
        raise BakeoffError("a bake-off needs at least 2 candidates")


def record_candidate_result(exp: BakeoffExperiment, *, candidate_name: str,
                            total_spend_usd: float, wall_time_hours: float,
                            tests_passed: bool, pr_number: int | None,
                            pr_state: str, failed_attempts: int,
                            human_repair_minutes: float | None = None) -> dict:
    if exp.status != "RUNNING":
        raise BakeoffError("experiment not RUNNING")
    if total_spend_usd > exp.spend_cap_usd:
        raise BakeoffError(
            f"candidate {candidate_name} spend {total_spend_usd} exceeds cap "
            f"{exp.spend_cap_usd} — record as CANCELLED_SPEND_EXCEEDED, never as a result")
    entry = {
        "candidate": candidate_name,
        "model": next((c.model_id for c in exp.candidates if c.name == candidate_name), "unknown"),
        "provider": next((c.provider for c in exp.candidates if c.name == candidate_name), "unknown"),
        "total_spend_usd": round(total_spend_usd, 4),
        "wall_time_hours": round(wall_time_hours, 3),
        "tests_passed": tests_passed,
        "pr_number": pr_number,
        "pr_state": pr_state,           # distinct outcome states (MERGED != ACCEPTED)
        "failed_attempts": failed_attempts,
        "human_repair_minutes": human_repair_minutes,
        "cost_per_accepted_result": (
            round(total_spend_usd, 4) if pr_state == "ACCEPTED" else None),
    }
    exp.results.append(entry)
    return entry


def summary(exp: BakeoffExperiment) -> dict:
    """Comparison summary; candidates without ACCEPTED show cost-per-accepted=None."""
    accepted = [r for r in exp.results if r["pr_state"] == "ACCEPTED"]
    return {
        "experiment_id": exp.experiment_id,
        "goal": exp.goal,
        "starting_sha": exp.starting_sha,
        "status": exp.status,
        "candidates": len(exp.candidates),
        "results": exp.results,
        "winner": (min(accepted, key=lambda r: r["total_spend_usd"])["candidate"]
                   if accepted else None),
        "total_spend_usd": round(sum(r["total_spend_usd"] for r in exp.results), 4),
        "spend_cap_usd": exp.spend_cap_usd,
        "within_cap": sum(r["total_spend_usd"] for r in exp.results) <= exp.spend_cap_usd,
    }
