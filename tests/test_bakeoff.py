"""Phase L: model bake-off framework tests (budget-gated, no uncontrolled spend)."""

from __future__ import annotations

import pytest

from acms.bakeoff import (
    BakeoffError,
    BakeoffExperiment,
    Candidate,
    record_candidate_result,
    summary,
    validate_experiment,
)


def _exp(**kw) -> BakeoffExperiment:
    base = dict(
        experiment_id="bo-1", goal="compare models on P0 feature",
        starting_sha="abc1234", plan_markdown="same plan for all",
        acceptance_criteria=["tests pass", "P0 checklist complete"],
        time_budget_hours=4.0, spend_cap_usd=5.0, authorized_by="jordan",
        candidates=[Candidate("glm", "glm-5.3-flash", "openrouter", "cand/glm"),
                    Candidate("big", "frontier-model", "together", "cand/big")],
    )
    base.update(kw)
    return BakeoffExperiment(**base)


def test_valid_experiment_passes():
    exp = _exp(status="RUNNING")
    validate_experiment(exp)


def test_no_authorizer_rejected():
    with pytest.raises(BakeoffError, match="authorizer"):
        validate_experiment(_exp(authorized_by=""))


def test_zero_spend_cap_rejected():
    with pytest.raises(BakeoffError, match="spend cap"):
        validate_experiment(_exp(spend_cap_usd=0))


def test_no_starting_sha_rejected():
    with pytest.raises(BakeoffError, match="starting_sha"):
        validate_experiment(_exp(starting_sha=""))


def test_single_candidate_rejected():
    with pytest.raises(BakeoffError, match="at least 2"):
        validate_experiment(_exp(candidates=[Candidate("a", "m", "p", "b")]))


def test_spend_over_cap_refuses_result():
    exp = _exp(status="RUNNING")
    with pytest.raises(BakeoffError, match="exceeds cap"):
        record_candidate_result(exp, candidate_name="glm", total_spend_usd=9.0,
                                wall_time_hours=1.0, tests_passed=True,
                                pr_number=9, pr_state="MERGED", failed_attempts=0)


def test_result_and_summary():
    exp = _exp(status="RUNNING")
    record_candidate_result(exp, candidate_name="glm", total_spend_usd=0.05,
                            wall_time_hours=1.0, tests_passed=True,
                            pr_number=41, pr_state="ACCEPTED", failed_attempts=0)
    record_candidate_result(exp, candidate_name="big", total_spend_usd=4.0,
                            wall_time_hours=2.0, tests_passed=True,
                            pr_number=42, pr_state="MERGED", failed_attempts=1)
    s = summary(exp)
    assert s["winner"] == "glm"
    assert s["within_cap"] is True
    # MERGED candidate has cost_per_accepted=None (never equate merged/accepted)
    big = next(r for r in s["results"] if r["candidate"] == "big")
    assert big["cost_per_accepted_result"] is None
