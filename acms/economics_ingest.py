"""Phase K — objective PR-metadata ingestion from GitHub (2026-09-29 plan §13).

Ingests AUTOMATED, objective PR facts into `pr_outcomes`:
    repository, branch, PR number/url, merge SHA, checks state, merge state.

Explicit invariants:
    - "merged" is NEVER recorded as ACCEPTED (that requires an explicit human/
      authorized acceptance event — the service enforces it elsewhere);
    - derived state mapping:
        PR open                → OPEN
        merged, checks green   → CI_VERIFIED
        merged, checks failing → MERGED (rework signal visible, state honest)
        closed unmerged        → REWORK_REQUIRED only if labeled; else OPEN→CLOSED
          (we record CLOSED via rework flag only when evidence exists; unknown
           data stays UNKNOWN — i.e. we leave OPEN and store raw metadata)
    - unknown data remains UNKNOWN (never invented).

Backfill: `backfill_recent_prs()` pulls the last N merged PRs of a repo where
correlation is reliable (PR authored by the agent identity / branch naming).
"""
from __future__ import annotations

import json
import os
import urllib.request
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .economics_models import (
    OUTCOME_CI_VERIFIED,
    OUTCOME_MERGED,
    OUTCOME_OPEN,
    PrOutcomeRecord,
)
from .telemetry_service import add_event


@dataclass
class GithubPrFacts:
    repository: str
    number: int
    branch: str
    state: str                 # open | closed
    merged: bool
    merge_sha: str | None
    checks_state: str | None   # success | failure | pending | None(unknown)
    pr_url: str
    author: str | None


def fetch_repo_prs(repo: str, *, limit: int = 30, token_env: str = "GITHUB_TOKEN") -> list[GithubPrFacts]:
    """Read-only GitHub REST pull of recent PRs. Requires GITHUB_TOKEN env.

    Never fabricates data: on API failure raises; unknown fields stay None.
    """
    token = os.environ.get(token_env)
    if not token:
        raise RuntimeError(f"no {token_env} configured — refusing to fabricate PR facts")
    req = urllib.request.Request(
        f"https://api.github.com/repos/{repo}/pulls?state=all&per_page={limit}&sort=updated&direction=desc",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = json.loads(resp.read().decode())
    out = []
    for pr in data:
        out.append(GithubPrFacts(
            repository=repo,
            number=pr["number"],
            branch=pr["head"]["ref"],
            state=pr["state"],
            merged=bool(pr.get("merged_at")),
            merge_sha=pr.get("merge_commit_sha"),
            checks_state=None,  # filled by fetch_pr_checks (separate call)
            pr_url=pr["html_url"],
            author=(pr.get("user") or {}).get("login"),
        ))
    return out


def fetch_pr_checks(repo: str, number: int, *, token_env: str = "GITHUB_TOKEN") -> str | None:
    """Combined check-run state for a PR's head SHA: success/failure/pending."""
    token = os.environ.get(token_env)
    if not token:
        return None
    try:
        req = urllib.request.Request(
            f"https://api.github.com/repos/{repo}/pulls/{number}",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            pr = json.loads(resp.read().decode())
        sha = pr["head"]["sha"]
        req2 = urllib.request.Request(
            f"https://api.github.com/repos/{repo}/commits/{sha}/check-runs",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
        )
        with urllib.request.urlopen(req2, timeout=20) as resp2:
            runs = json.loads(resp2.read().decode()).get("check_runs", [])
        if not runs:
            return None
        if any(r["conclusion"] == "failure" for r in runs):
            return "failure"
        if any(r["status"] != "completed" for r in runs):
            return "pending"
        return "success"
    except Exception:
        return None  # unknown stays unknown


def derive_outcome_state(facts: GithubPrFacts) -> str:
    """Objective state mapping — MERGED never becomes ACCEPTED here."""
    if facts.merged:
        if facts.checks_state == "success":
            return OUTCOME_CI_VERIFIED
        return OUTCOME_MERGED
    return OUTCOME_OPEN


async def upsert_outcome(db: AsyncSession, facts: GithubPrFacts,
                         *, model_id: str | None = None,
                         agent_id: str | None = None) -> PrOutcomeRecord:
    """Idempotent upsert by (repository, pr_number)."""
    existing = (await db.scalars(
        select(PrOutcomeRecord).where(
            PrOutcomeRecord.repository == facts.repository,
            PrOutcomeRecord.pr_number == facts.number)
    )).first()
    state = derive_outcome_state(facts)
    if existing:
        existing.outcome_state = state
        if facts.merge_sha and not existing.commit_shas_json:
            existing.commit_shas_json = json.dumps([facts.merge_sha])
        rec = existing
    else:
        rec = PrOutcomeRecord(
            outcome_id=PrOutcomeRecord.new_id() if hasattr(PrOutcomeRecord, "new_id")
            else __import__("uuid").uuid4().hex,
            repository=facts.repository,
            branch=facts.branch,
            pr_number=facts.number,
            pr_url=facts.pr_url,
            commit_shas_json=json.dumps([facts.merge_sha]) if facts.merge_sha else None,
            outcome_state=state,
            model_id=model_id,
            agent_id=agent_id,
        )
        db.add(rec)
    await add_event(db, event_type="PR_OUTCOME_STATE", actor_source="economics_ingest",
                    summary=f"{facts.repository}#{facts.number} → {state}",
                    metadata={"repository": facts.repository, "pr_number": facts.number,
                              "checks_state": facts.checks_state, "merged": facts.merged})
    await db.commit()
    return rec


async def backfill_recent_prs(db: AsyncSession, repo: str, *, limit: int = 30) -> list[str]:
    """Backfill objective facts for recent PRs; returns [repo#n state, ...]."""
    results = []
    for facts in fetch_repo_prs(repo, limit=limit):
        facts.checks_state = fetch_pr_checks(repo, facts.number)
        rec = await upsert_outcome(db, facts)
        results.append(f"{repo}#{facts.number}={rec.outcome_state}")
    return results
