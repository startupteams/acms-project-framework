"""Business-idea → product bootstrap (window-5 plan §8/§9/§10).

Pipeline:
    idea source → parse grounded metadata → idea review screen (human edits)
    → [create] private repo (adapter) → baseline docs → ACMS hierarchy
      (Product/Project/initial Work Item) → Jira link (create-or-link)
    → DRAFT (execution requires the Jira gate: human ready-state + AI assignee)

Honesty rules (§9.2/§9.4):
- Idea briefs are DRAFT/UNVALIDATED source material. Speculative statements
  become ASSUMPTIONS / RESEARCH QUESTIONS in the docs — never confirmed
  requirements. Requirements generated from an idea carry explicit
  ``draft/unvalidated`` + validation-priority markers.
- Everything is RESUMABLE (§9.5): a durable request id + per-step results;
  retries reuse created resources; ambiguous remote outcomes reconcile by
  correlation metadata before retrying; nothing is deleted to compensate.
- Jira creation/linking failure retains completed artifacts and shows
  "Awaiting Jira linkage"; execution stays blocked (no unlinked fallback).
- Creating a Jira issue NEVER sets To Start (human gate, §9.5).

Source options for this sprint (§8.2): pasted Markdown, GitHub Business Idea
Generator import (token-granting repos only — reported honestly), blank.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .bootstrap_models import BootstrapRequestRecord
from .settings import get_settings
from .telemetry_service import add_event
from .work_models import WorkItemCreate, WorkItemKind, WorkItemRecord, WorkItemStatus
from . import work_service

IDEA_REPO = "startupteams/business_idea_generator"
IDEA_PATH_PREFIX = "data/ideas/"


class BootstrapError(Exception):
    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code


# ---------------------------------------------------------------- idea parsing

def parse_idea_markdown(text: str) -> dict[str, Any]:
    """Extract ONLY grounded metadata that actually exists (§8.3).

    Frontmatter (YAML-lite subset: flat scalars + simple lists) + body sections.
    Unknown fields are preserved verbatim under ``extra`` — never dropped, and
    nothing is inferred that the file does not say.
    """
    out: dict[str, Any] = {"extra": {}}
    fm: dict[str, Any] = {}
    body = text
    if text.startswith("---"):
        m = re.match(r"^---\n(.*?)\n---\n?(.*)$", text, re.S)
        if m:
            body = m.group(2)
            # frontmatter is YAML-lite: flat scalars, simple lists (2-space
            # dash items), and ONE nesting level of sub-keys (e.g. score_inputs)
            current_key: str | None = None
            for line in m.group(1).splitlines():
                if not line.strip() or line.strip().startswith("#"):
                    continue
                dash_item = re.match(r"^\s+-\s+(.*)$", line)
                if dash_item and current_key is not None:
                    cur = fm.get(current_key)
                    if isinstance(cur, list):
                        cur.append(_scalar(dash_item.group(1)))
                    elif isinstance(cur, dict):
                        if not cur:
                            fm[current_key] = [_scalar(dash_item.group(1))]  # was a list after all
                        else:
                            cur["__items__"] = cur.get("__items__", []) + [dash_item.group(1)]
                    continue
                sub_kv = re.match(r"^\s{2,}([A-Za-z0-9_]+):\s*(.*)$", line)
                if sub_kv and current_key is not None:
                    cur = fm.setdefault(current_key, {})
                    if isinstance(cur, dict):
                        cur[sub_kv.group(1)] = _scalar(sub_kv.group(2))
                    continue
                kv = re.match(r"^([A-Za-z0-9_]+):\s*(.*)$", line)
                if kv:
                    key, val = kv.group(1), kv.group(2).strip()
                    current_key = key
                    if val == "":
                        fm[key] = {}  # may become a list or nested dict on next lines
                    else:
                        fm[key] = _scalar(val)
            # normalize empty-dict-that-was-a-list: source_urls/tags are lists;
            # if a dict only got __items__ entries or stayed empty but the key is
            # known-list, coerce. Unknown keys stay dicts (honest preservation).
            for key, val in list(fm.items()):
                if isinstance(val, dict):
                    if not val and key in ("tags", "source_urls"):
                        fm[key] = []
                    elif "__items__" in val and len(val) == 1:
                        fm[key] = val["__items__"]

    KNOWN = ("title", "slug", "persona", "pain", "category", "status", "confidence",
             "goodness_score", "competition", "potential", "estimated_mrr",
             "difficulty", "best_market", "time_to_mvp", "validation_priority",
             "validation_next_step", "validation_model_version", "date", "created_by")
    for k in KNOWN:
        if k in fm:
            out[k] = fm.pop(k)
    out["tags"] = fm.pop("tags", [])
    out["extra"] = fm  # score_inputs, source_urls, competition etc. preserved

    # body sections (grounded — only what the file actually contains)
    def section(name: str) -> str | None:
        m = re.search(rf"^##\s+{re.escape(name)}\s*\n(.*?)(?=^##\s|\Z)", body, re.S | re.M)
        return m.group(1).strip() if m else None

    out["one_line_summary"] = section("One-line summary")
    out["product_concept"] = section("Product concept")
    out["assumptions_to_validate"] = section("Assumptions to validate")
    out["next_actions"] = section("Next actions")
    out["business_model"] = section("Business model")
    out["body_markdown"] = body
    out["content_sha256"] = hashlib.sha256(text.encode()).hexdigest()[:16]
    return out


def _scalar(v: str) -> Any:
    v = v.strip().strip('"').strip("'")
    if re.fullmatch(r"-?\d+", v):
        return int(v)
    if re.fullmatch(r"-?\d+\.\d+", v):
        return float(v)
    return v


def validation_classified(idea: dict[str, Any]) -> dict[str, str]:
    """§9.2 classification of the idea's claims — draft material stays draft."""
    status = str(idea.get("status", "draft/unvalidated"))
    validated = status.strip().lower() in ("validated", "validated/committed")
    return {
        "source_status": status,
        "is_validated": validated,
        "requirements_class": "human-approved requirement" if validated else "assumption",
        "validation_priority": str(idea.get("validation_priority", "unspecified")),
        "validation_next_step": str(idea.get("validation_next_step", "unspecified")),
    }


# ---------------------------------------------------------------- github import

def fetch_idea_from_github(path: str) -> dict[str, Any]:
    """Fetch one idea file from the Business Idea Generator repo (§8.3).

    Uses ACMS_GITHUB_ECONOMICS_TOKEN. A token not granted the repo surfaces a
    human-gate error (never a fabricated idea). Provenance: commit sha of the
    fetched blob.
    """
    tok = os.environ.get("ACMS_GITHUB_ECONOMICS_TOKEN", "")
    if not tok:
        raise BootstrapError("github-token-missing",
                             "ACMS_GITHUB_ECONOMICS_TOKEN not configured")
    url = f"https://api.github.com/repos/{IDEA_REPO}/contents/{path}"
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {tok}", "Accept": "application/vnd.github+json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            d = json.load(r)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise BootstrapError(
                "idea-repo-not-granted",
                f"the configured token cannot read {IDEA_REPO} (404 — fine-grained "
                f"PATs are repo-scoped). Grant read on {IDEA_REPO} in the GitHub UI "
                f"(web-only action — human gate) or paste/import the Markdown instead.",
            ) from None
        raise BootstrapError("github-error", f"HTTP {e.code}") from None
    content = base64.b64decode(d.get("content", "")).decode()
    idea = parse_idea_markdown(content)
    idea["source_provenance"] = {
        "source_repo": IDEA_REPO, "source_path": path,
        "source_commit_sha": d.get("sha"),
        "import_timestamp": datetime.now(timezone.utc).isoformat(),
        "content_sha256": idea["content_sha256"],
    }
    return idea


def list_github_ideas() -> list[dict[str, str]]:
    """Browse the idea directory (discovered, not hard-coded names)."""
    tok = os.environ.get("ACMS_GITHUB_ECONOMICS_TOKEN", "")
    if not tok:
        raise BootstrapError("github-token-missing", "token not configured")
    url = f"https://api.github.com/repos/{IDEA_REPO}/contents/{IDEA_PATH_PREFIX}"
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {tok}", "Accept": "application/vnd.github+json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            d = json.load(r)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise BootstrapError(
                "idea-repo-not-granted",
                f"the configured token cannot list {IDEA_REPO} — grant read access "
                f"in the GitHub UI (human gate) or paste the Markdown directly.",
            ) from None
        raise BootstrapError("github-error", f"HTTP {e.code}") from None
    return [{"path": x["path"], "name": x["name"]} for x in d if x["name"].endswith(".md")]


# ---------------------------------------------------------------- docs builder

def baseline_docs(idea: dict[str, Any], product_name: str, classification: dict[str, str],
                  jira_note: str = "") -> dict[str, str]:
    """Startup Teams project-framework baseline docs (§9.2) — draft-preserving.

    Returns {filename: content}. Speculative idea content is classified as
    assumptions/research questions, never as confirmed requirements (§9.2).
    """
    name = product_name
    slug = idea.get("slug") or re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    val = classification
    assumps = idea.get("assumptions_to_validate") or "_none recorded in the source idea_"
    next_step = idea.get("validation_next_step") or val["validation_next_step"]

    readme = f"""# {name}

**Status: {val['source_status']}** — this product originated from a business-idea
brief and MUST NOT be treated as validated product requirements.

- Persona: {idea.get('persona', '_unspecified_')}
- Problem: {idea.get('pain', '_unspecified_')}
- One-line summary: {idea.get('one_line_summary') or '_see source idea_'}

Provenance: {json.dumps(idea.get('source_provenance', {}))}
"""

    requirements = f"""# Requirements — {name}

> **Everything below is {val['requirements_class'].upper()} material.**
> Source idea status: **{val['source_status']}** · validation priority:
> **{val['validation_priority']}** (§9.4: uncertainty is preserved; the idea's
> own recommendation to validate before heavy productization is respected).

## Validation-gated requirements
- RQ-1 (research question): {next_step}
- RQ-2 (research question): confirm pricing model acceptance before building reusable software.

## Assumptions (NOT requirements)
{assumps}

## Acceptance criteria for the FIRST work item
- A human-reviewable product/requirements package exists (this repo).
- Validation research (interviews / paid pilot evidence) is planned or executed.
"""

    problems = f"""# Business Problems — {name}

Source pain (verbatim, {val['source_status']}): {idea.get('pain', '_unspecified_')}

Vendor/demand figures in the source idea are **demand signals, not validation**
(§8.3 honesty rule).
"""

    architecture = f"""# Architecture — {name}

To be drafted AFTER validation evidence exists. No implementation stack is
committed at bootstrap time (§9.4: do not skip from idea score to software build).
"""

    future_work = f"""# Future Work — {name}

- (Source-Derived) Productization after repeated pilot implementations.
- (Source-Derived) Template offer with fixed scope from repeated patterns.

Human-Directed / Agent-Discovered items are added as they arise.
"""

    initial = f"""# Initial idea artifact — {name}

Source idea (imported {datetime.now(timezone.utc).date()}, provenance above):
{idea.get('body_markdown') or '_pasted idea body_'}
{jira_note}
"""

    return {
        "README.md": readme,
        "REQUIREMENTS.md": requirements,
        "BUSINESS_PROBLEMS.md": problems,
        "ARCHITECTURE.md": architecture,
        "FUTURE_WORK.md": future_work,
        "INITIAL_IDEAS.md": initial,
    }


# ---------------------------------------------------------------- resumable core

async def create_bootstrap_request(db: AsyncSession, *, requested_by: str,
                                   source_kind: str) -> str:
    req_id = str(uuid.uuid4())
    db.add(BootstrapRequestRecord(
        request_id=req_id, requested_by=requested_by, source_kind=source_kind,
        state="draft", created_at=datetime.now(timezone.utc),
        steps_json="{}",
    ))
    await add_event(db, event_type="BOOTSTRAP_REQUESTED", actor_source="product_bootstrap",
                    summary=f"product bootstrap request {req_id[:8]} ({source_kind}) by {requested_by}",
                    metadata={"request_id": req_id})
    await db.commit()
    return req_id


async def _step(db: AsyncSession, req_id: str, name: str, fn):
    """Run one bootstrap step with durable per-step results (§9.5)."""
    req = await db.get(BootstrapRequestRecord, req_id)
    steps = json.loads(req.steps_json or "{}")
    if steps.get(name, {}).get("status") == "done":
        return steps[name]["result"], False  # resumable: reuse prior work
    try:
        result = await fn()
    except BootstrapError as e:
        steps[name] = {"status": "failed", "error_code": e.code, "error": str(e)[:400],
                       "at": datetime.now(timezone.utc).isoformat()}
        req.steps_json = json.dumps(steps)
        await db.commit()
        raise
    steps[name] = {"status": "done", "result": result,
                   "at": datetime.now(timezone.utc).isoformat()}
    req.steps_json = json.dumps(steps)
    await db.commit()
    return result, True


async def run_bootstrap(db: AsyncSession, *, req_id: str, product_name: str,
                        idea: dict[str, Any], executive_agent_id: str | None,
                        jira_issue_key: str | None = None,
                        create_jira: bool = False, actor: str = "api") -> dict[str, Any]:
    """Execute (or resume) the bootstrap: repo → docs → hierarchy → Jira link.

    Never: sets Jira ready state, auto-starts execution, deletes a repo to
    compensate for Jira failure, promotes assumptions to requirements.
    """
    from .jira_gate import gate_work_item

    classification = validation_classified(idea)

    # ---- step: repository (adapter — honest when the token can't create) ----
    async def _repo():
        from .repo_adapter import create_private_repo

        return await create_private_repo(product_name, actor=actor)
    repo_result, _ = await _step(db, req_id, "repository", _repo)

    # ---- step: baseline docs (content created locally; pushed with repo) ----
    async def _docs():
        docs = baseline_docs(idea, product_name, classification)
        from .repo_adapter import push_baseline_docs

        return await push_baseline_docs(repo_result, docs, actor=actor)
    docs_result, _ = await _step(db, req_id, "baseline_docs", _docs)

    # ---- step: ACMS hierarchy (Product → Project → initial Work Item) -------
    async def _hierarchy():
        product = await work_service.create_work_item(db, WorkItemCreate(
            kind=WorkItemKind.PRODUCT, title=product_name,
            created_by=f"bootstrap:{actor}",
            scope_markdown=idea.get("one_line_summary") or ""))
        project = await work_service.create_work_item(db, WorkItemCreate(
            kind=WorkItemKind.PROJECT, title=f"{product_name} — first wedge",
            parent_id=product.work_item_id, created_by=f"bootstrap:{actor}",
            scope_markdown=idea.get("persona") or ""))
        # FIRST work item = measurable goal (§10.2): the validation package,
        # NOT "build the whole product" (the idea says validate first).
        initial = await work_service.create_work_item(db, WorkItemCreate(
            kind=WorkItemKind.FEATURE, title="Produce human-reviewable product/requirements package",
            parent_id=project.work_item_id, created_by=f"bootstrap:{actor}",
            scope_markdown=(f"Measurable goal: a human-reviewable package exists that captures "
                            f"the source idea (status {classification['source_status']}), its "
                            f"classified assumptions, and a validation research plan "
                            f"({classification['validation_priority']}). "
                            f"Validation gate: interviews / paid pilot evidence.")))
        if executive_agent_id:
            from .work_models import AssignmentCreate

            record, err = await work_service.assign_primary(db, AssignmentCreate(
                work_item_id=initial.work_item_id, agent_id=executive_agent_id))
            if err:
                # honest: assignment refused — bootstrap continues (assignment
                # can be made later); never fabricate an assignment
                await add_event(db, event_type="BOOTSTRAP_ASSIGN_REFUSED",
                                actor_source="product_bootstrap",
                                summary=f"executive assignment refused: {err}",
                                metadata={"work_item_id": initial.work_item_id})
        return {"product_id": product.work_item_id, "project_id": project.work_item_id,
                "initial_work_item_id": initial.work_item_id,
                "initial_work_key": initial.work_key}
    hierarchy, _ = await _step(db, req_id, "acms_hierarchy", _hierarchy)

    # ---- step: Jira linkage (create-or-link; failure retains everything) ----
    jira_result: dict[str, Any]
    try:
        async def _jira():
            from .jira_client import JiraClient, JiraError

            client = JiraClient()
            if jira_issue_key:
                try:
                    issue = client.get_issue(jira_issue_key)
                except JiraError as e:
                    raise BootstrapError("jira-link-failed",
                                         f"linking {jira_issue_key} failed: {str(e)[:200]} "
                                         f"— artifacts retained; execution stays blocked") from None
                return {"linked": True, "issue_key": issue.key, "issue_url": issue.url,
                        "issue_id": getattr(issue, "issue_id", None),
                        "status": issue.status}
            if not create_jira:
                return {"linked": False, "awaiting": "human chooses create-or-link"}
            # create a HIGH-LEVEL issue only (§7.3: no microtask fan-out);
            # readiness is NEVER inferred — the human sets To Start + assignee.
            from .repo_adapter import create_jira_issue  # uses configured project

            return await create_jira_issue(product_name, idea, actor=actor)
        jira_result, _ = await _step(db, req_id, "jira_linkage", _jira)
    except BootstrapError as e:
        jira_result = {"linked": False, "error_code": e.code, "error": str(e)[:300],
                       "awaiting": "Awaiting Jira linkage — artifacts retained"}

    # persist linkage on the initial work item when linked
    gate: dict[str, Any] | None = None
    if jira_result.get("linked"):
        initial = await db.get(WorkItemRecord, hierarchy["initial_work_item_id"])
        if initial is not None:
            initial.jira_issue_key = jira_result.get("issue_key")
            initial.jira_url = jira_result.get("issue_url")
            # immutable id when the linker provided it (display key can change)
            if jira_result.get("issue_id"):
                initial.jira_issue_id = jira_result["issue_id"]
            await db.commit()
            g = await gate_work_item(db, hierarchy["initial_work_item_id"], live_check=True)
            gate = g.to_dict()

    req = await db.get(BootstrapRequestRecord, req_id)
    req.state = "completed" if jira_result.get("linked") else "awaiting_jira"
    await add_event(db, event_type="BOOTSTRAP_COMPLETED", actor_source="product_bootstrap",
                    summary=f"bootstrap {req_id[:8]}: repo={'ok' if repo_result else '?'} "
                            f"hierarchy={hierarchy} jira={jira_result.get('linked')}",
                    metadata={"request_id": req_id, **hierarchy})
    await db.commit()
    return {"request_id": req_id, "state": req.state,
            "repository": repo_result, "docs": list(docs_result) if docs_result else [],
            "hierarchy": hierarchy, "jira": jira_result,
            "gate": gate,
            "classification": classification,
            "note": ("execution requires the Jira kickoff gate: a human must set the issue to a "
                     "ready state (default TO START) assigned to startupteamscompany@gmail.com; "
                     "then Check Jira now — create/link alone never authorizes execution"
                     if jira_result.get("linked") else
                     "Awaiting Jira linkage — execution blocked; completed artifacts retained")}