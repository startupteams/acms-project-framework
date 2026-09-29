"""Phase K: GitHub PR-outcome ingestion tests (state derivation, no fabrication)."""

from __future__ import annotations

import pytest

from acms.economics_ingest import GithubPrFacts, derive_outcome_state
from acms.economics_models import (
    OUTCOME_CI_VERIFIED,
    OUTCOME_MERGED,
    OUTCOME_OPEN,
)


def _facts(**kw) -> GithubPrFacts:
    base = dict(repository="startupteams/acms-project-framework", number=1,
                branch="feat/x", state="closed", merged=True,
                merge_sha="abc123", checks_state="success", pr_url="u",
                author="jordatech")
    base.update(kw)
    return GithubPrFacts(**base)


def test_merged_green_checks_is_ci_verified_not_accepted():
    assert derive_outcome_state(_facts()) == OUTCOME_CI_VERIFIED
    # INVARIANT: no mapping produces ACCEPTED
    for checks in ("success", "failure", "pending", None):
        for merged in (True, False):
            state = derive_outcome_state(_facts(checks_state=checks, merged=merged))
            assert state != "ACCEPTED", "merged must never auto-become ACCEPTED"


def test_merged_failing_checks_is_merged_only():
    assert derive_outcome_state(_facts(checks_state="failure")) == OUTCOME_MERGED


def test_merged_unknown_checks_is_merged_only():
    assert derive_outcome_state(_facts(checks_state=None)) == OUTCOME_MERGED


def test_open_pr_is_open():
    assert derive_outcome_state(_facts(state="open", merged=False)) == OUTCOME_OPEN


def test_no_token_refuses_to_fabricate(monkeypatch):
    import acms.economics_ingest as ei

    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    with pytest.raises(RuntimeError, match="refusing to fabricate"):
        ei.fetch_repo_prs("x/y")
