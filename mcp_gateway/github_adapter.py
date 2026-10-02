"""GitHub domain adapter (plan §25) — GitHub REST API is the authority.

Direct REST client (mirrors ServerManagerClient patterns). The gateway holds a
single PAT (``MCP_GATEWAY_GITHUB_TOKEN``) whose authority comes from the
token's own GitHub permissions — the gateway never widens it.

Surface (plan §25):

Resources (READ):
  github.repo.get      — repo metadata (default branch, visibility, perms)
  github.branch.get    — one branch (sha + protected flag)
  github.commit.get    — one commit (message, author, files)
  github.pr.get        — one PR (state, head/base, mergeable)
  github.checks.get    — check runs for a ref (PR head or branch)
  github.issue.get     — one issue (state, labels)

Worker tools (SAFE_WRITE):
  github.branch.create — create agent/<work_uid>-<agent> from a base ref
  github.file.write    — stage a file blob on the agent's OWN branch
  github.commit.create — materialize staged blobs as a commit on own branch
  github.pr.create     — open PR own-branch → base
  github.pr.comment    — comment on a PR
  github.review.comment — review comment on a PR (COMMENT events only)

Hard limits (plan §16/§25/§43):
- Branch-ownership policy: writes only on branches matching
  ``agent/<own_work_uid>-<own_agent_name>`` (assignment tokens) or
  ``agent/<work_uid>-<own_agent_name>`` (agent tokens bound to their own
  assignment). Foreign branches => OUT_OF_SCOPE, audited.
- NO merge/delete/force-push/org tools exist at all. DESTRUCTIVE stays
  deny-for-all in the policy engine. Merges remain human authority.
- Assignment tokens are additionally scoped to ``identity.repositories``;
  agent tokens require the repo in ``MCP_GATEWAY_GITHUB_REPOS``.

Git data API flow for writes (contents API rejected for multi-file + atomicity):
  github.file.write   -> POST /repos/{r}/git/blobs            (staged blob)
  github.commit.create-> base commit -> new tree (staged blobs)
                         -> create commit -> update ref (fast-forward only;
                         non-fast-forward => CONFLICT, never force-push).
"""
from __future__ import annotations

import base64
import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from .errors import ConflictError, DomainUnavailableError, NotFoundError, ValidationError_

GITHUB_API = "https://api.github.com"

BRANCH_PREFIX = "agent/"


def owned_branch(work_uid: str, agent_name: str) -> str:
    """The ONLY writable branch for (work_uid, agent). Deterministic (§25)."""
    return f"{BRANCH_PREFIX}{work_uid}-{agent_name}"


def branch_owner(branch: str) -> tuple[str, str] | None:
    """Parse 'agent/<work_uid>-<agent_name>' -> (work_uid, agent_name).

    Work UIDs contain no '-' after the numeric suffix... actually they DO:
    ACMS-WORK-000017-20261002_081122 — so the agent name is everything after
    the UID prefix match. UIDs always start with 'ACMS-WORK-'; the agent name
    is appended after '-'. Parse by locating the agent-name suffix is
    ambiguous; instead ownership checks compare against the caller's OWN
    expected branch string (exact match) — this parser is only used for
    error messages and defense-in-depth prefix validation.
    """
    if not branch.startswith(BRANCH_PREFIX):
        return None
    rest = branch[len(BRANCH_PREFIX):]
    if not rest or "/" in rest:
        return None
    if rest.startswith("ACMS-WORK-"):
        # split at the boundary: UID = ACMS-WORK-######-YYYYMMDD_HHMMSS
        # UID shape is known; find the last '-' before the agent name by
        # matching the timestamp segment (8 digits_6 digits at the end).
        import re
        m = re.match(r"^(ACMS-WORK-\d{6}-\d{8}_\d{6})-(.+)$", rest)
        if m:
            return m.group(1), m.group(2)
    return None


class GithubClient:
    """Thin GitHub REST client (PAT authority; no scope widening)."""

    def __init__(self, token: str, *, timeout_seconds: int = 20,
                 repos_allowlist: list[str] | None = None):
        if not token:
            raise ValueError("GitHub token not configured")
        self._token = token
        self.timeout = timeout_seconds
        self.repos_allowlist = list(repos_allowlist or [])

    # ------------------------------------------------------------- transport

    def request(self, method: str, path: str, payload: dict | None = None,
                *, accept: str = "application/vnd.github+json") -> tuple[int, Any, dict]:
        url = GITHUB_API + path
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(
            url, data=data, method=method,
            headers={"Authorization": f"Bearer {self._token}",
                     "Accept": accept}
            | ({"Content-Type": "application/json"} if data else {}))
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = resp.read().decode("utf-8", errors="replace")
                status = resp.status
                headers = dict(resp.headers)
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            status = exc.code
            headers = dict(exc.headers or {})
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise DomainUnavailableError(
                f"GitHub unreachable: {exc.__class__.__name__}") from exc
        try:
            parsed = json.loads(body) if body else None
        except ValueError:
            parsed = body
        return status, parsed, headers

    # ------------------------------------------------------------- reposCOPE

    def repo_allowed(self, repo: str) -> bool:
        """Repo scope check. Assignment tokens pre-filter; agent tokens use
        the allowlist (empty allowlist = github domain closed for agent
        tokens)."""
        if not self.repos_allowlist:
            return False
        return repo in self.repos_allowlist

    # ------------------------------------------------------------- §25 reads

    def get_repo(self, repo: str) -> dict:
        status, body, _ = self.request("GET", f"/repos/{repo}")
        if status == 404:
            raise NotFoundError(f"repo not found or not accessible: {repo}")
        if status != 200:
            raise DomainUnavailableError(f"repo read failed (HTTP {status})")
        return {
            "full_name": body.get("full_name"),
            "private": body.get("private"),
            "default_branch": body.get("default_branch"),
            "permissions": body.get("permissions"),
            "html_url": body.get("html_url"),
            "pushed_at": body.get("pushed_at"),
        }

    def get_branch(self, repo: str, branch: str) -> dict:
        status, body, _ = self.request(
            "GET", f"/repos/{repo}/branches/{urllib.parse.quote(branch, safe='')}")
        if status == 404:
            raise NotFoundError(f"branch not found: {branch}")
        if status != 200:
            raise DomainUnavailableError(f"branch read failed (HTTP {status})")
        commit = body.get("commit") or {}
        return {
            "name": body.get("name"),
            "sha": (commit.get("commit") or {}).get("sha"),
            "protected": body.get("protected"),
        }

    def get_commit(self, repo: str, ref: str) -> dict:
        status, body, _ = self.request("GET", f"/repos/{repo}/commits/{urllib.parse.quote(ref, safe='')}")
        if status == 404:
            raise NotFoundError(f"commit not found: {ref}")
        if status != 200:
            raise DomainUnavailableError(f"commit read failed (HTTP {status})")
        return {
            "sha": body.get("sha"),
            "message": (body.get("commit") or {}).get("message"),
            "author": ((body.get("commit") or {}).get("author") or {}),
            "html_url": body.get("html_url"),
            "files": [{"filename": f.get("filename"), "status": f.get("status"),
                       "additions": f.get("additions"), "deletions": f.get("deletions")}
                      for f in (body.get("files") or [])[:50]],
        }

    def get_pr(self, repo: str, number: int) -> dict:
        status, body, _ = self.request("GET", f"/repos/{repo}/pulls/{number}")
        if status == 404:
            raise NotFoundError(f"PR not found: {repo}#{number}")
        if status != 200:
            raise DomainUnavailableError(f"PR read failed (HTTP {status})")
        return {
            "number": body.get("number"),
            "state": body.get("state"),
            "title": body.get("title"),
            "head_ref": (body.get("head") or {}).get("ref"),
            "head_sha": (body.get("head") or {}).get("sha"),
            "base_ref": (body.get("base") or {}).get("ref"),
            "draft": body.get("draft"),
            "mergeable": body.get("mergeable"),
            "mergeable_state": body.get("mergeable_state"),
            "html_url": body.get("html_url"),
            "user": (body.get("user") or {}).get("login"),
        }

    def get_checks(self, repo: str, ref: str) -> dict:
        status, body, _ = self.request(
            "GET", f"/repos/{repo}/commits/{urllib.parse.quote(ref, safe='')}/check-runs",
            accept="application/vnd.github+json")
        if status == 404:
            raise NotFoundError(f"ref not found for checks: {ref}")
        if status != 200:
            raise DomainUnavailableError(f"checks read failed (HTTP {status})")
        runs = []
        for run in (body.get("check_runs") or [])[:25]:
            runs.append({
                "name": run.get("name"),
                "status": run.get("status"),
                "conclusion": run.get("conclusion"),
                "html_url": run.get("html_url"),
            })
        return {"ref": ref, "total_count": body.get("total_count"), "check_runs": runs}

    def get_issue(self, repo: str, number: int) -> dict:
        status, body, _ = self.request("GET", f"/repos/{repo}/issues/{number}")
        if status == 404:
            raise NotFoundError(f"issue not found: {repo}#{number}")
        if status != 200:
            raise DomainUnavailableError(f"issue read failed (HTTP {status})")
        return {
            "number": body.get("number"),
            "state": body.get("state"),
            "title": body.get("title"),
            "labels": [l.get("name") for l in (body.get("labels") or [])],
            "html_url": body.get("html_url"),
            "is_pr": "pull_request" in (body or {}),
        }

    # ------------------------------------------------------------- §25 writes

    def create_branch(self, repo: str, new_branch: str, base_ref: str) -> dict:
        status, ref_body, _ = self.request(
            "GET", f"/repos/{repo}/git/ref/heads/{urllib.parse.quote(base_ref, safe='')}")
        if status == 404:
            raise NotFoundError(f"base ref not found: {base_ref}")
        if status != 200:
            raise DomainUnavailableError(f"base ref read failed (HTTP {status})")
        sha = (ref_body.get("object") or {}).get("sha")
        if not sha:
            raise DomainUnavailableError("base ref missing object sha")
        status, body, _ = self.request(
            "POST", f"/repos/{repo}/git/refs",
            payload={"ref": f"refs/heads/{new_branch}", "sha": sha})
        if status == 422:
            detail = (body or {}).get("message", "") if isinstance(body, dict) else ""
            if "already exists" in str(detail).lower() or "Reference already exists" in str(detail):
                raise ConflictError(f"branch already exists: {new_branch}")
            raise ValidationError_(f"branch create rejected: {str(detail)[:200]}")
        if status != 201:
            raise DomainUnavailableError(f"branch create failed (HTTP {status})")
        return {"branch": new_branch, "sha": sha, "base": base_ref}

    def create_blob(self, repo: str, content: str, *, encoding: str = "utf-8") -> str:
        """Stage file content as a blob; returns blob sha (NOT yet in a tree)."""
        raw = content.encode(encoding)
        status, body, _ = self.request(
            "POST", f"/repos/{repo}/git/blobs",
            payload={"content": base64.b64encode(raw).decode(),
                     "encoding": "base64"})
        if status != 201:
            detail = (body or {}).get("message", "") if isinstance(body, dict) else ""
            raise DomainUnavailableError(f"blob create failed (HTTP {status}): {str(detail)[:200]}")
        return body.get("sha", "")

    def get_commit_sha(self, repo: str, branch: str) -> str:
        status, body, _ = self.request(
            "GET", f"/repos/{repo}/git/ref/heads/{urllib.parse.quote(branch, safe='')}")
        if status == 404:
            raise NotFoundError(f"branch not found: {branch}")
        if status != 200:
            raise DomainUnavailableError(f"ref read failed (HTTP {status})")
        return (body.get("object") or {}).get("sha", "")

    def build_tree_from_base(self, repo: str, base_commit_sha: str,
                             staged: list[dict]) -> tuple[str, str]:
        """Create a tree from base commit's tree + staged blobs.

        staged: [{"path": str, "blob_sha": str}] (already-created blobs).
        Returns (new_tree_sha, base_tree_sha).
        """
        status, base_commit, _ = self.request(
            "GET", f"/repos/{repo}/git/commits/{base_commit_sha}")
        if status != 200 or not isinstance(base_commit, dict):
            raise DomainUnavailableError(f"base commit read failed (HTTP {status})")
        base_tree = (base_commit.get("tree") or {}).get("sha", "")
        tree = [{"path": s["path"], "mode": "100644", "type": "blob",
                 "sha": s["blob_sha"]} for s in staged]
        status, body, _ = self.request(
            "POST", f"/repos/{repo}/git/trees",
            payload={"base_tree": base_tree, "tree": tree})
        if status != 201:
            detail = (body or {}).get("message", "") if isinstance(body, dict) else ""
            raise ValidationError_(f"tree create failed (HTTP {status}): {str(detail)[:200]}")
        return body.get("sha", ""), base_tree

    def create_commit(self, repo: str, branch: str, message: str,
                      tree_sha: str, parent_sha: str,
                      author_name: str, author_email: str) -> dict:
        payload: dict = {
            "message": message,
            "tree": tree_sha,
            "parents": [parent_sha],
        }
        if author_name and author_email:
            payload["author"] = {"name": author_name, "email": author_email}
        status, body, _ = self.request(
            "POST", f"/repos/{repo}/git/commits", payload=payload)
        if status != 201:
            detail = (body or {}).get("message", "") if isinstance(body, dict) else ""
            raise ValidationError_(f"commit create failed (HTTP {status}): {str(detail)[:200]}")
        new_sha = body.get("sha", "")
        # fast-forward ref update ONLY (never force): pass expected current sha
        status2, body2, _ = self.request(
            "PATCH", f"/repos/{repo}/git/refs/heads/{urllib.parse.quote(branch, safe='')}",
            payload={"sha": new_sha, "force": False})
        if status2 != 200:
            detail = (body2 or {}).get("message", "") if isinstance(body2, dict) else ""
            raise ConflictError(
                f"ref update failed (HTTP {status2}); branch moved or protected: {str(detail)[:200]}")
        return {"commit_sha": new_sha, "branch": branch, "tree": tree_sha}

    def create_pr(self, repo: str, head: str, base: str, title: str,
                  body: str, *, draft: bool = False) -> dict:
        status, resp, _ = self.request(
            "POST", f"/repos/{repo}/pulls",
            payload={"title": title, "head": head, "base": base,
                     "body": body, "draft": draft})
        if status == 422:
            detail = str((resp or {}).get("message", "")) if isinstance(resp, dict) else ""
            errors = (resp or {}).get("errors") if isinstance(resp, dict) else None
            if errors:
                detail = "; ".join(str(e.get("message", e)) for e in errors if isinstance(e, dict))[:200] or detail
            raise ValidationError_(f"PR create rejected: {detail}")
        if status != 201:
            raise DomainUnavailableError(f"PR create failed (HTTP {status})")
        return {
            "number": resp.get("number"),
            "html_url": resp.get("html_url"),
            "state": resp.get("state"),
            "draft": resp.get("draft"),
        }

    def add_pr_comment(self, repo: str, number: int, body: str) -> dict:
        status, resp, _ = self.request(
            "POST", f"/repos/{repo}/issues/{number}/comments",
            payload={"body": body})
        if status == 404:
            raise NotFoundError(f"PR not found: {repo}#{number}")
        if status == 403:
            raise DomainUnavailableError("comment denied (403) — token or branch policy")
        if status not in (200, 201):
            raise DomainUnavailableError(f"PR comment failed (HTTP {status})")
        return {"comment_id": resp.get("id"), "html_url": resp.get("html_url")}

    def add_review_comment(self, repo: str, number: int, body: str,
                           commit_sha: str, path: str, line: int | None = None,
                           side: str = "RIGHT") -> dict:
        """Review comment (pull-request review comment API).

        COMMIT-id anchored; line optional (file-level comment when omitted).
        COMMENT-event only: the gateway NEVER submits APPROVE/REQUEST_CHANGES —
        review verdicts stay human (plan §25: no merge/approve authority).
        """
        status, pr, _ = self.request("GET", f"/repos/{repo}/pulls/{number}")
        if status == 404:
            raise NotFoundError(f"PR not found: {repo}#{number}")
        if status != 200:
            raise DomainUnavailableError(f"PR read failed (HTTP {status})")
        payload: dict = {"body": body, "commit_id": commit_sha, "path": path,
                         "side": side}
        if line is not None:
            payload["line"] = int(line)
        status, resp, _ = self.request(
            "POST", f"/repos/{repo}/pulls/{number}/comments", payload=payload)
        if status == 422:
            detail = str((resp or {}).get("message", "")) if isinstance(resp, dict) else ""
            raise ValidationError_(f"review comment rejected: {detail[:200]}")
        if status != 201:
            raise DomainUnavailableError(f"review comment failed (HTTP {status})")
        return {"comment_id": resp.get("id"), "html_url": resp.get("html_url")}
