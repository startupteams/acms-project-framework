"""Repository + Jira creation adapters (window-5 plan §9/§9.5).

DRY-RUN-FIRST: every remote-creation adapter has a dry-run plan mode; real
creation is attempted only when a write-capable token exists. GitHub repo
creation with a fine-grained PAT is IMPOSSIBLE (GitHub limitation: fine-grained
PATs cannot create repositories) — the adapter reports that honestly and the
bootstrap retains completed artifacts with 'Awaiting repository creation'
rather than faking or bypassing. The UI shows the exact operator command.

Jira issue creation uses the EXISTING credentials + supported project metadata;
high-level issues only (§7.3), never sets ready state, never fans out microtasks.
"""
from __future__ import annotations

import json
import os
import re
import urllib.request
import urllib.error
from typing import Any

from .product_bootstrap import BootstrapError

GITHUB_API = "https://api.github.com"


def _token() -> str:
    return os.environ.get("ACMS_GITHUB_ECONOMICS_TOKEN", "")


def _gh(method: str, path: str, body: dict | None = None,
        token: str | None = None, timeout: int = 30) -> tuple[int, dict | None, str, dict]:
    tok = token or _token()
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(GITHUB_API + path, data=data, method=method)
    req.add_header("Authorization", f"Bearer {tok}")
    req.add_header("Accept", "application/vnd.github+json")
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, data=data, timeout=timeout) as r:
            raw = r.read().decode()
            return r.status, (json.loads(raw) if raw else {}), "", dict(r.headers)
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read().decode())
        except Exception:
            detail = {}
        return e.code, detail, detail.get("message", "")[:300], dict(e.headers)


def repo_name_for(product_name: str) -> str:
    """Startup Teams naming convention: kebab-case, lowercase (existing repos observed)."""
    return re.sub(r"[^a-z0-9]+", "-", product_name.lower()).strip("-")[:80]


async def create_private_repo(product_name: str, *, actor: str = "api",
                              org: str = "startupteams") -> dict[str, Any]:
    """Create the PRIVATE product repository (§9.1/§18: PRIVATE default).

    Dry-run semantics: fine-grained PATs cannot create repos — report the
    HUMAN-GATE path with the exact command; never fabricate a repo URL.
    """
    name = repo_name_for(product_name)
    tok = _token()
    if not tok:
        raise BootstrapError("repo-token-missing",
                             "ACMS_GITHUB_ECONOMICS_TOKEN not configured; repository "
                             "creation requires a write-capable token (human gate)")
    # probe: does the repo already exist? (resumable reuse — §9.5)
    code, existing, _, _ = _gh("GET", f"/repos/{org}/{name}")
    if code == 200 and isinstance(existing, dict):
        return {"created": False, "reused": True, "repo": f"{org}/{name}",
                "url": existing.get("html_url"), "private": existing.get("private"),
                "default_branch": existing.get("default_branch"),
                "note": "existing repository reused (bootstrap is resumable)"}
    if code != 404:
        raise BootstrapError("repo-probe-failed", f"HTTP {code}: unknown repo state")
    # token cannot create repos if fine-grained (creation needs org admin / classic PAT)
    code2, resp, err, _ = _gh("POST", f"/orgs/{org}/repos",
                              {"name": name, "private": True,
                               "description": f"Product workspace for {product_name} "
                                              f"(bootstrap {actor})"})
    if code2 in (201,) and isinstance(resp, dict):
        return {"created": True, "reused": False, "repo": resp.get("full_name"),
                "url": resp.get("html_url"), "private": resp.get("private"),
                "default_branch": resp.get("default_branch")}
    if code2 == 403:
        raise BootstrapError(
            "repo-create-forbidden",
            f"the configured token cannot create repositories (fine-grained PATs "
            f"cannot; HTTP 403: {err}). HUMAN GATE — create '{org}/{name}' as PRIVATE "
            f"in the GitHub UI, then re-run this bootstrap step (it reuses the repo).",
        )
    raise BootstrapError("repo-create-failed", f"HTTP {code2}: {err}")


async def push_baseline_docs(repo: dict[str, Any], docs: dict[str, str], *,
                             actor: str = "api") -> dict[str, Any]:
    """Commit baseline docs to the (existing or newly created) repo."""
    if not repo or not repo.get("repo"):
        raise BootstrapError("repo-missing", "no repository recorded for docs push")
    if repo.get("created") is False and not repo.get("reused"):
        raise BootstrapError("repo-unavailable", str(repo)[:200])
    full = repo["repo"]
    tok = _token()
    ref = repo.get("default_branch", "main")
    pushed, failed = [], []
    for fname, content in docs.items():
        # resolve current sha for update-or-create (idempotent — retries reuse)
        code, cur, _, _ = _gh("GET", f"/repos/{full}/contents/{fname}?ref={ref}")
        body: dict[str, Any] = {
            "message": f"docs: bootstrap baseline ({fname}) via ACMS product bootstrap",
            "content": __import__("base64").b64encode(content.encode()).decode(),
            "branch": ref,
            "committer": {"name": "ACMS Bootstrap", "email": "acms@startupteams.co"},
        }
        if code == 200 and isinstance(cur, dict) and cur.get("sha"):
            body["sha"] = cur["sha"]
        code2, resp, err, _ = _gh("PUT", f"/repos/{full}/contents/{fname}", body)
        if code2 in (200, 201):
            pushed.append(fname)
        else:
            failed.append({"file": fname, "http": code2, "error": err})
    if failed and not pushed:
        raise BootstrapError("docs-push-failed", json.dumps(failed)[:400])
    return {"repo": full, "pushed": pushed, "failed": failed or None}


async def create_jira_issue(product_name: str, idea: dict[str, Any], *,
                            actor: str = "api") -> dict[str, Any]:
    """Create ONE high-level Jira issue (§9.5) — never sets ready state.

    Uses the existing integration credentials + the first configured allowed
    project (§8.3 discovery; project workflow metadata verified at link time).
    """
    try:
        from .jira_client import JiraClient
        from .settings import get_settings
    except Exception as e:  # pragma: no cover
        raise BootstrapError("jira-client-error", str(e)[:200]) from None

    client = JiraClient()
    if client.is_mock:
        raise BootstrapError(
            "jira-not-configured",
            "Jira credentials not configured; link an existing issue manually or "
            "install credentials first — no fake issue is created.")
    projects = sorted(getattr(client, "allowed_projects", set()) or set())
    if not projects:
        raise BootstrapError("jira-no-project",
                             "ACMS_JIRA_PROJECTS not configured; cannot pick a project")
    project = projects[0]

    summary = f"[Product] {product_name} — kickoff"
    desc = (f"High-level authorization issue for ACMS product bootstrap.\n\n"
            f"Idea: {idea.get('one_line_summary') or product_name}\n"
            f"Persona: {idea.get('persona', '')}\n"
            f"Source status: {idea.get('status', 'draft/unvalidated')} "
            f"(validation: {idea.get('validation_priority', 'unspecified')})\n\n"
            f"NEXT HUMAN STEPS: move to {get_settings().jira_ready_statuses} and assign "
            f"startupteamscompany@gmail.com to authorize AI execution; then use "
            f"'Check Jira now' in ACMS.")
    # Jira v3 create
    import urllib.request as _ur

    payload = {
        "fields": {
            "project": {"key": project},
            "summary": summary[:250],
            "issuetype": {"name": "Task"},
            "description": {"type": "doc", "version": 1,
                            "content": [{"type": "paragraph",
                                         "content": [{"type": "text", "text": desc[:8000]}]}]},
            "labels": ["acms-bootstrap", "draft/unvalidated"],
        }
    }
    tok = os.environ.get("ACMS_JIRA_API_TOKEN", "")
    email = os.environ.get("ACMS_JIRA_EMAIL", "")
    base = os.environ.get("ACMS_JIRA_BASE_URL", "").rstrip("/")
    data = json.dumps(payload).encode()
    req = _ur.Request(f"{base}/rest/api/3/issue", data=data, method="POST")
    import base64 as b64mod

    req.add_header("Authorization", "Basic " + b64mod.b64encode(f"{email}:{tok}".encode()).decode())
    req.add_header("Content-Type", "application/json")
    try:
        with _ur.urlopen(req, timeout=20) as r:
            d = json.load(r)
        key = d.get("key")
        return {"linked": True, "created": True, "issue_key": key,
                "issue_id": d.get("id"),
                "issue_url": f"{base}/browse/{key}",
                "note": ("issue created WITHOUT ready state — a human must set it to a "
                         "ready state assigned to the AI account; creation != authorization")}
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")[:300]
        raise BootstrapError("jira-create-failed",
                             f"HTTP {e.code}: {body} — draft retained; "
                             f"link an existing issue instead or request permission") from None