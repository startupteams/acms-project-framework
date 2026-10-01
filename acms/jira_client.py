"""Jira ↔ Executive Agent v1 integration (2026-09-29 plan §1.5 / Phase D).

Architecture (human-approved):
    Jira issue = human-facing work/goal
    Executive Agent = conceptual AI assignee (service identity: miamtechnologypvtltd@gmail.com)
    ACMS Work Items = internal AI decomposition (NEVER mirrored back as Jira tickets)
    summaries/links flow back up to Jira

v1 invariants (plan §D7/D8):
    ACMS Work Item creation != Jira ticket creation — the client below has NO
    create-issue capability at all. Status mutation is config-flagged OFF by
    default. Linear is intentionally not implemented (existing Jira↔Linear sync
    remains the fallback).

Credentials come from env/settings only — never committed:
    ACMS_JIRA_BASE_URL   e.g. https://startupteams.atlassian.net
    ACMS_JIRA_EMAIL      the service account email
    ACMS_JIRA_API_TOKEN  Jira API token (Atlassian account settings)
    ACMS_JIRA_PROJECTS   optional comma-separated allowed project keys (inbound filter)
    ACMS_JIRA_STATUS_MUTATION_ENABLED  (default false)

If configuration is absent, the client runs in MOCK mode: requests are recorded
in-memory and deterministic fake payloads are returned, so the contract and
tests work without fabricating credentials (plan §D2).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import httpx

from .settings import get_settings

# Fields needed for kickoff-gate observations (§13.4): status + statusCategory
# + assignee accountId (immutable identity) + updated (changelog identity).
_ISSUE_FIELDS = "summary,description,status,priority,labels,assignee,updated"


def ai_account_id_for_jql() -> str:
    """Dedicated AI account's Jira accountId for JQL queries.

    JQL string-literal safety: accountId contains ':' and hex — quote it for
    JQL; reject anything that could break out of the literal.
    """
    aid = (getattr(get_settings(), "jira_ai_account_id", "") or "").strip()
    if not aid:
        return ""
    if any(ch in aid for ch in ("'", "\\", "\n", ";")):
        raise JiraError("ACMS_JIRA_AI_ACCOUNT_ID contains characters unsafe for JQL")
    return aid


@dataclass
class JiraIssue:
    key: str
    summary: str
    description: str
    status: str
    priority: str
    labels: list[str]
    assignee: str | None
    url: str
    # window-5 §7.1: match assignments by immutable accountId, never by
    # display name or email visibility.
    issue_id: str | None = None
    assignee_account_id: str | None = None
    status_category: str | None = None
    updated: str | None = None

    @classmethod
    def from_api(cls, d: dict[str, Any], base_url: str) -> "JiraIssue":
        f = d.get("fields", {})
        assignee = f.get("assignee") or {}
        status_obj = f.get("status") or {}
        return cls(
            key=d.get("key", ""),
            summary=f.get("summary", ""),
            description=f.get("description") or "",
            status=status_obj.get("name", ""),
            priority=(f.get("priority") or {}).get("name", ""),
            labels=list(f.get("labels") or []),
            assignee=assignee.get("emailAddress"),
            url=f"{base_url}/browse/{d.get('key', '')}",
            issue_id=d.get("id"),
            assignee_account_id=assignee.get("accountId"),
            status_category=((status_obj.get("statusCategory") or {}).get("name")),
            updated=f.get("updated"),
        )


class JiraError(RuntimeError):
    pass


class JiraClient:
    """Bounded Jira REST v3 client: read assigned issues + post comments only."""

    def __init__(self, *, mock: bool | None = None) -> None:
        s = get_settings()
        self.base_url = (s.jira_base_url or "").rstrip("/")
        self.email = s.jira_email
        self.token = s.jira_api_token
        self.allowed_projects = {p.strip() for p in s.jira_projects.split(",") if p.strip()}
        self.status_mutation_enabled = s.jira_status_mutation_enabled
        self.is_mock = mock if mock is not None else not (self.base_url and self.email and self.token)
        self.mock_calls: list[dict[str, Any]] = []
        self._mock_issues: dict[str, dict[str, Any]] = {}

    # ------------------------------------------------------------------ mock

    def add_mock_issue(self, key: str, summary: str, description: str = "", **kw: Any) -> None:
        self._mock_issues[key] = {
            "key": key,
            "fields": {
                "summary": summary,
                "description": description,
                "status": {"name": kw.get("status", "To Do")},
                "priority": {"name": kw.get("priority", "Medium")},
                "labels": kw.get("labels", []),
                "assignee": {"emailAddress": kw.get("assignee")} if kw.get("assignee") else None,
            },
        }

    def _record(self, op: str, **kw: Any) -> None:
        self.mock_calls.append({"op": op, **kw})

    # ------------------------------------------------------------------ auth

    def _auth(self) -> tuple[str, str]:
        return (self.email, self.token)

    def _check_projects(self, issue_key: str) -> None:
        if self.allowed_projects:
            project = issue_key.split("-")[0]
            if project not in self.allowed_projects:
                raise JiraError(f"project {project} not in ACMS_JIRA_PROJECTS allow-list")

    # ------------------------------------------------------------------ inbound

    def search_assigned_issues(self, max_results: int = 25) -> list[JiraIssue]:
        """Issues assigned to the service identity (bounded page)."""
        if self.is_mock:
            self._record("search_assigned", max_results=max_results)
            issues = [JiraIssue.from_api(d, "https://mock.jira") for d in self._mock_issues.values()]
            return issues[:max_results]
        jql = "assignee = currentuser() ORDER BY updated DESC"
        with httpx.Client(base_url=self.base_url, auth=self._auth(), timeout=20) as c:
            r = c.get("/rest/api/3/search/jql", params={"jql": jql, "maxResults": max_results,
                                                        "fields": _ISSUE_FIELDS})
            r.raise_for_status()
            return [JiraIssue.from_api(d, self.base_url) for d in r.json().get("issues", [])]

    def search_ai_account_issues(self, max_results: int = 50, start_at: int = 0,
                                 project: str | None = None) -> tuple[list[JiraIssue], int | None]:
        """Issues assigned to the DEDICATED AI account — paginated (§13.4 step 2).

        Returns (issues, next_start_at-or-None). JQL matches by accountId so it
        works regardless of email-visibility settings.
        """
        account_id = ai_account_id_for_jql()
        if not account_id:
            raise JiraError("ACMS_JIRA_AI_ACCOUNT_ID not configured; cannot search AI-assigned issues")
        jql = f"assignee = '{account_id}' ORDER BY updated DESC"
        if project:
            jql = f"project = {project} AND {jql}"
        if self.is_mock:
            self._record("search_ai_account_issues", max_results=max_results)
            issues = [JiraIssue.from_api(d, "https://mock.jira") for d in self._mock_issues.values()]
            page = issues[start_at:start_at + max_results]
            nxt = start_at + max_results if start_at + max_results < len(issues) else None
            return page, nxt
        with httpx.Client(base_url=self.base_url, auth=self._auth(), timeout=20) as c:
            r = c.get("/rest/api/3/search/jql", params={
                "jql": jql, "maxResults": max_results, "startAt": start_at,
                "fields": _ISSUE_FIELDS})
            r.raise_for_status()
            body = r.json()
            issues = [JiraIssue.from_api(d, self.base_url) for d in body.get("issues", [])]
            total = body.get("total")
            nxt = start_at + max_results if total is not None and start_at + max_results < total else None
            return issues, nxt

    def get_issue(self, issue_key: str) -> JiraIssue:
        self._check_projects(issue_key)
        if self.is_mock:
            self._record("get_issue", key=issue_key)
            if issue_key not in self._mock_issues:
                raise JiraError(f"mock issue {issue_key} not found")
            return JiraIssue.from_api(self._mock_issues[issue_key], "https://mock.jira")
        with httpx.Client(base_url=self.base_url, auth=self._auth(), timeout=20) as c:
            r = c.get(f"/rest/api/3/issue/{issue_key}",
                      params={"fields": _ISSUE_FIELDS})
            r.raise_for_status()
            return JiraIssue.from_api(r.json(), self.base_url)

    def get_issue_by_id(self, issue_id: str) -> JiraIssue:
        """Fetch by IMMUTABLE issue id (§13.4 step 2: keys can change)."""
        if self.is_mock:
            self._record("get_issue_by_id", issue_id=issue_id)
            for d in self._mock_issues.values():
                if str(d.get("id", "")) == str(issue_id):
                    return JiraIssue.from_api(d, "https://mock.jira")
            raise JiraError(f"mock issue id {issue_id} not found")
        with httpx.Client(base_url=self.base_url, auth=self._auth(), timeout=20) as c:
            r = c.get(f"/rest/api/3/issue/{issue_id}", params={"fields": _ISSUE_FIELDS})
            r.raise_for_status()
            return JiraIssue.from_api(r.json(), self.base_url)

    def get_comments(self, issue_key: str, max_results: int = 50) -> list[str]:
        self._check_projects(issue_key)
        if self.is_mock:
            self._record("get_comments", key=issue_key)
            return []
        with httpx.Client(base_url=self.base_url, auth=self._auth(), timeout=20) as c:
            r = c.get(f"/rest/api/3/issue/{issue_key}/comment", params={"maxResults": max_results})
            r.raise_for_status()
            return [c_.get("body", "") for c_ in r.json().get("comments", [])]

    # ------------------------------------------------------------------ outbound

    @staticmethod
    def _markdown_to_adf(body_markdown: str) -> dict:
        """Convert simple Markdown/plain text to Atlassian Document Format (ADF).

        Jira REST v3 REJECTS plain-string bodies ('Comment body is not valid!')
        — the body MUST be an ADF doc. This converter handles the subset ACMS
        posts (headings, paragraphs, bullet lines, code, links kept as literal
        text) — hit live 2026-10-01 when the STNA-87 correction note 400'd.
        """
        import re as _re

        content: list[dict] = []
        lines = (body_markdown or "").strip().splitlines()
        i = 0
        while i < len(lines):
            line = lines[i]
            stripped = line.strip()
            if not stripped:
                i += 1
                continue
            # fenced code block
            if stripped.startswith("```"):
                code_lines = []
                i += 1
                while i < len(lines) and not lines[i].strip().startswith("```"):
                    code_lines.append(lines[i])
                    i += 1
                i += 1  # skip closing fence
                content.append({
                    "type": "codeBlock",
                    "attrs": {"language": "text"},
                    "content": [{"type": "text", "text": "\n".join(code_lines)}],
                })
                continue
            # headings: # .. ####
            m = _re.match(r"^(#{1,6})\s+(.*)$", stripped)
            if m:
                level = min(len(m.group(1)), 6)
                content.append({
                    "type": f"heading{level}" if level > 3 else {1: "heading1", 2: "heading2", 3: "heading3"}[level],
                    "content": [{"type": "text", "text": m.group(2)}],
                })
                i += 1
                continue
            # bullet
            if stripped.startswith("- ") or stripped.startswith("* "):
                items = []
                while i < len(lines) and (lines[i].strip().startswith("- ") or lines[i].strip().startswith("* ")):
                    items.append({
                        "type": "listItem",
                        "content": [{"type": "paragraph", "content": [
                            {"type": "text", "text": lines[i].strip()[2:].strip()}]}],
                    })
                    i += 1
                content.append({"type": "bulletList", "content": items})
                continue
            # paragraph: accumulate until blank line
            para_lines = [stripped]
            i += 1
            while i < len(lines) and lines[i].strip() and not lines[i].strip().startswith(("#", "- ", "* ", "```")):
                para_lines.append(lines[i].strip())
                i += 1
            para_content: list[dict] = []
            for j, pl in enumerate(para_lines):
                if j:
                    para_content.append({"type": "hardBreak"})
                para_content.append({"type": "text", "text": pl})
            content.append({"type": "paragraph", "content": para_content})
        return {"type": "doc", "version": 1, "content": content or
                [{"type": "paragraph", "content": [{"type": "text", "text": "(empty)"}]}]}


    def add_comment(self, issue_key: str, body_markdown: str) -> str:
        """Post a useful summary comment (status summaries, handoffs, links).

        Body is converted to ADF (Jira REST v3 requires a document body — a raw
        string 400s with 'Comment body is not valid!'); markdown structure
        (headings/bullets/code) is preserved where mappable.
        """
        self._check_projects(issue_key)
        if self.is_mock:
            self._record("add_comment", key=issue_key, body=body_markdown)
            return f"mock-comment-{len(self.mock_calls)}"
        with httpx.Client(base_url=self.base_url, auth=self._auth(), timeout=20) as c:
            r = c.post(f"/rest/api/3/issue/{issue_key}/comment",
                       json={"body": JiraClient._markdown_to_adf(body_markdown)})
            r.raise_for_status()
            return r.json().get("id", "")

    def transition_status(self, issue_key: str, target_status: str) -> bool:
        """Config-flagged OFF by default (plan §D6). Mock records the attempt."""
        if not self.status_mutation_enabled:
            raise JiraError(
                "Jira status mutation is disabled (ACMS_JIRA_STATUS_MUTATION_ENABLED=false); "
                "v1 posts comments/summaries only"
            )
        self._check_projects(issue_key)
        if self.is_mock:
            self._record("transition_status", key=issue_key, target=target_status)
            return True
        with httpx.Client(base_url=self.base_url, auth=self._auth(), timeout=20) as c:
            r = c.get(f"/rest/api/3/issue/{issue_key}/transitions")
            r.raise_for_status()
            match = next((t for t in r.json().get("transitions", [])
                          if t.get("name", "").lower() == target_status.lower()), None)
            if match is None:
                raise JiraError(f"transition '{target_status}' not available for {issue_key}")
            r2 = c.post(f"/rest/api/3/issue/{issue_key}/transitions/{match['id']}")
            r2.raise_for_status()
            return True
