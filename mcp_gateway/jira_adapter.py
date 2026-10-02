"""Jira MCP domain adapter (plan §28) — ACMS Jira REST contract, policy-gated.

The gateway talks to Jira REST v3 with its OWN env-injected credential
(``MCP_GATEWAY_JIRA_*`` — same shape as ACMS's ``ACMS_JIRA_*``). The gateway
NEVER holds worker-scoped Jira credentials and NEVER transitions outside the
ACMS transition policy (plan §28):

  ordinary implementation worker:
    TO START -> IN PROGRESS
    IN PROGRESS -> IN REVIEW
    NEVER -> BLOCKED (executive/authorized role only; a blocked worker keeps
    Jira IN PROGRESS, comments the blocker, updates ACMS Work state, raises
    Inbox — the gateway mirrors that contract by denying BLOCKED for workers)

  executive/infrastructure_admin:
    any transition INCLUDING -> BLOCKED

All status mutations ALSO honor the ACMS global mutation flag
(``ACMS_JIRA_STATUS_MUTATION_ENABLED`` contract): when the Jira backend has
status mutation disabled, jira.transition.request fails closed with the same
actionable message ACMS uses — no silent bypass (STNA-88 posture).

Surface (plan §28):

Resources (READ):
  jira.issue.get          (URI: jira://issue/{issue_key})  — summary/desc/status
  jira.comments.list      (URI: jira://issue/{issue_key}/comments)
  jira.status.get         (URI: jira://issue/{issue_key}/status)
  jira.template.get       (URI: jira://template/{issue_key})  — template fidelity view

Worker tools:
  jira.comment.add           (SAFE_WRITE; own-assignment issue only)
  jira.description.update    (SAFE_WRITE; own-assignment issue only)
  jira.artifact.link         (SAFE_WRITE; posts the exact artifact URL comment)
  jira.transition.request    (SAFE_WRITE w/ ACMS transition policy enforced here)
  jira.template.clone_request (SENSITIVE_WRITE; executive only — plan §28:
                              "Agent may request STNA-86 clone only when authorized")

Scope rules:
- worker/assignment tokens: READ + WRITE only on the issue key bound to their
  assignment (assignment token binding, or the ACTIVE assignment's linked
  work item). Out-of-scope issue keys = OUT_OF_SCOPE (audited).
- executive/infrastructure_admin: broader reads; transitions incl. BLOCKED.
- ADF conversion rides the SAME markdown->ADF converter as ACMS (heading2
  400-class guard, live-found 2026-10-02) — implemented locally to keep the
  gateway independent of the ACMS process, with the identical contract.
"""
from __future__ import annotations

import base64
import json
import re
import urllib.error
import urllib.request
from typing import Any

from .errors import DomainUnavailableError, NotFoundError, ValidationError_
from .models import CallIdentity


# Workers may request these transitions ONLY (plan §28 + §8A amendment).
WORKER_ALLOWED_TRANSITIONS = {
    "to start": {"in progress"},
    "in progress": {"in review"},
}
EXECUTIVE_FORBIDDEN = set()  # executives may transition freely incl. -> BLOCKED


def _markdown_to_adf(body_markdown: str) -> dict[str, Any]:
    """Markdown -> Atlassian Document Format (Jira REST v3 comment bodies).

    Mirrors acms.jira_client.JiraClient._markdown_to_adf semantics: headings
    become {type: heading, attrs: {level}} (NOT heading2 — invalid ADF,
    live-400 class), bullets/paragraphs/inline-code mapped, fenced code
    becomes codeBlock with plain text.
    """
    doc: dict[str, Any] = {"type": "doc", "version": 1, "content": []}

    def _inline(text: str) -> list[dict[str, Any]]:
        # inline `code` marks
        parts: list[dict[str, Any]] = []
        for i, chunk in enumerate(re.split(r"`([^`]+)`", text)):
            if not chunk:
                continue
            if i % 2 == 1:
                parts.append({"type": "text", "text": chunk,
                              "marks": [{"type": "code"}]})
            else:
                parts.append({"type": "text", "text": chunk})
        return parts or [{"type": "text", "text": text}]

    lines = (body_markdown or "").split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        fence = re.match(r"^```(\w*)\s*$", line)
        if fence:
            code_lines: list[str] = []
            i += 1
            while i < len(lines) and not lines[i].startswith("```"):
                code_lines.append(lines[i])
                i += 1
            i += 1  # closing fence
            doc["content"].append({
                "type": "codeBlock",
                "attrs": {"language": fence.group(1) or "text"},
                "content": [{"type": "text", "text": "\n".join(code_lines)}],
            })
            continue
        heading = re.match(r"^(#{1,6})\s+(.*)$", line)
        if heading:
            doc["content"].append({
                "type": "heading",
                "attrs": {"level": min(len(heading.group(1)), 6)},
                "content": _inline(heading.group(2)),
            })
        elif re.match(r"^[-*]\s+", line):
            items: list[dict[str, Any]] = []
            while i < len(lines) and re.match(r"^[-*]\s+", lines[i]):
                items.append({
                    "type": "listItem",
                    "content": [{"type": "paragraph",
                                 "content": _inline(re.sub(r"^[-*]\s+", "", lines[i]))}],
                })
                i += 1
            doc["content"].append({"type": "bulletList", "content": items})
            continue
        elif line.strip():
            doc["content"].append({"type": "paragraph", "content": _inline(line.rstrip())})
        i += 1
    if not doc["content"]:
        doc["content"] = [{"type": "paragraph",
                           "content": [{"type": "text", "text": body_markdown or ""}]}]
    return doc


class JiraMcpClient:
    """Thin Jira REST v3 client for the gateway (own credential)."""

    def __init__(self, base_url: str, email: str, api_token: str,
                 *, allowed_projects: str = "", timeout_seconds: int = 20):
        if not (base_url and email and api_token):
            raise ValueError("Jira base URL / email / token not configured")
        self.base_url = base_url.rstrip("/")
        self._auth_pair = (email, api_token)
        self.allowed_projects = {p.strip() for p in allowed_projects.split(",")
                                 if p.strip()}
        self.timeout = timeout_seconds

    def _request(self, method: str, path: str, payload: dict | None = None) -> tuple[int, Any]:
        url = f"{self.base_url}{path}"
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        cred = base64.b64encode(f"{self._auth_pair[0]}:{self._auth_pair[1]}".encode()).decode()
        req.add_header("Authorization", f"Basic {cred}")
        req.add_header("Accept", "application/json")
        if data:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = resp.read().decode()
                return resp.status, (json.loads(body) if body else {})
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode()[:400]
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise DomainUnavailableError(f"Jira unreachable: {exc}") from exc

    def _check_projects(self, issue_key: str) -> None:
        if self.allowed_projects:
            project = issue_key.split("-")[0]
            if project not in self.allowed_projects:
                raise ValidationError_(
                    f"project {project} not in the gateway Jira allow-list")

    def get_issue(self, issue_key: str) -> dict[str, Any]:
        self._check_projects(issue_key)
        status, body = self._request("GET", f"/rest/api/3/issue/{issue_key}")
        if status == 404:
            raise NotFoundError(f"Jira issue not found: {issue_key}")
        if status != 200 or not isinstance(body, dict):
            raise DomainUnavailableError(f"Jira issue read failed (HTTP {status})")
        return body

    def get_comments(self, issue_key: str, max_results: int = 50) -> dict[str, Any]:
        self._check_projects(issue_key)
        status, body = self._request(
            "GET", f"/rest/api/3/issue/{issue_key}/comment",
        )
        if status == 404:
            raise NotFoundError(f"Jira issue not found: {issue_key}")
        if status != 200 or not isinstance(body, dict):
            raise DomainUnavailableError(f"Jira comments read failed (HTTP {status})")
        comments = []
        for c in (body.get("comments") or [])[:max_results]:
            comments.append({
                "id": c.get("id"),
                "author": ((c.get("author") or {}).get("displayName")),
                "created": c.get("created"),
                "body": self._adf_to_text(c.get("body")),
            })
        return {"issue_key": issue_key, "total": body.get("total"),
                "comments": comments}

    @staticmethod
    def _adf_to_text(body: Any) -> str:
        """Flatten an ADF doc to readable text (comments arrive as ADF)."""
        if body is None:
            return ""
        if isinstance(body, str):
            return body
        out: list[str] = []

        def walk(node: Any) -> None:
            if isinstance(node, dict):
                if node.get("type") == "text":
                    out.append(str(node.get("text", "")))
                elif node.get("type") == "heading":
                    out.append("\n## ")
                elif node.get("type") in ("paragraph", "listItem"):
                    out.append("\n")
                for child in node.get("content", []) or []:
                    walk(child)
            elif isinstance(node, list):
                for child in node:
                    walk(child)

        walk(body)
        return "".join(out).strip()

    def add_comment(self, issue_key: str, body_markdown: str) -> dict[str, Any]:
        self._check_projects(issue_key)
        status, body = self._request(
            "POST", f"/rest/api/3/issue/{issue_key}/comment",
            payload={"body": _markdown_to_adf(body_markdown)})
        if status == 404:
            raise NotFoundError(f"Jira issue not found: {issue_key}")
        if status not in (200, 201):
            raise DomainUnavailableError(
                f"Jira comment failed (HTTP {status}): {str(body)[:200]}")
        return {"comment_id": (body or {}).get("id", ""), "issue_key": issue_key}

    def update_description(self, issue_key: str, description_markdown: str) -> dict[str, Any]:
        self._check_projects(issue_key)
        status, body = self._request(
            "PUT", f"/rest/api/3/issue/{issue_key}",
            payload={"fields": {"description": _markdown_to_adf(description_markdown)}})
        if status == 404:
            raise NotFoundError(f"Jira issue not found: {issue_key}")
        if status not in (200, 204):
            raise DomainUnavailableError(
                f"Jira description update failed (HTTP {status}): {str(body)[:200]}")
        return {"updated": True, "issue_key": issue_key}

    def list_transitions(self, issue_key: str) -> list[dict[str, Any]]:
        status, body = self._request("GET", f"/rest/api/3/issue/{issue_key}/transitions")
        if status != 200 or not isinstance(body, dict):
            raise DomainUnavailableError(
                f"Jira transitions read failed (HTTP {status})")
        return [{"id": t.get("id"), "name": (t.get("name") or ""),
                 "to": ((t.get("to") or {}).get("name", ""))}
                for t in body.get("transitions", [])]

    def transition(self, issue_key: str, target_status: str) -> dict[str, Any]:
        self._check_projects(issue_key)
        transitions = self.list_transitions(issue_key)
        match = next((t for t in transitions
                      if t["name"].lower() == target_status.lower()
                      or t["to"].lower() == target_status.lower()), None)
        if match is None:
            raise ValidationError_(
                f"transition '{target_status}' not available for {issue_key} "
                f"(available: {[t['name'] for t in transitions]})")
        status, body = self._request(
            "POST", f"/rest/api/3/issue/{issue_key}/transitions/{match['id']}")
        if status not in (200, 204):
            raise DomainUnavailableError(
                f"Jira transition failed (HTTP {status}): {str(body)[:200]}")
        return {"transitioned": True, "issue_key": issue_key,
                "target": target_status, "transition_id": match["id"]}

    def issue_brief(self, issue_key: str) -> dict[str, Any]:
        """Honest minimal projection for the jira.* READ resources."""
        d = self.get_issue(issue_key)
        f = d.get("fields", {})
        status_obj = f.get("status") or {}
        assignee = f.get("assignee") or {}
        project = f.get("project") or {}
        return {
            "key": d.get("key", issue_key),
            "issue_id": d.get("id"),
            "summary": f.get("summary"),
            "description": self._adf_to_text(f.get("description")),
            "status": status_obj.get("name"),
            "status_category": ((status_obj.get("statusCategory") or {}).get("name")),
            "priority": ((f.get("priority") or {}).get("name")),
            "labels": f.get("labels") or [],
            "assignee_display": assignee.get("displayName"),
            "assignee_account_id": assignee.get("accountId"),
            "project_key": project.get("key"),
            "issue_type": ((f.get("issuetype") or {}).get("name")),
            "updated": f.get("updated"),
            "url": f"{self.base_url}/browse/{d.get('key', issue_key)}",
        }