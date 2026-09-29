"""Jira↔ACMS sync service (v1, 2026-09-29 plan §6/Phase D).

Inbound:  poll assigned Jira issues → high-level ACMS work context (one work
          item per Jira issue MAX; Executive decomposes further internally).
Outbound: useful lifecycle summaries posted as Jira comments (started /
          milestone / blocked / PR available / completed / final handoff).
NEVER:    one Jira ticket per internal Work Item or task (plan §D7 invariant).
"""
from __future__ import annotations

from .jira_client import JiraClient, JiraError
from .telemetry_service import TelemetryService


# Correlation: Jira key stored on the Work Item title/scope prefix.
JIRA_CORRELATION_PREFIX = "jira:"


def correlation_tag(issue_key: str) -> str:
    return f"{JIRA_CORRELATION_PREFIX}{issue_key}"


def extract_jira_key(scope_or_title: str) -> str | None:
    idx = scope_or_title.find(JIRA_CORRELATION_PREFIX)
    if idx < 0:
        return None
    rest = scope_or_title[idx + len(JIRA_CORRELATION_PREFIX):]
    token = rest.split()[0].split("\n")[0].strip()
    return token or None


class JiraSyncService:
    """Stateless-ish orchestration over JiraClient; db session injected."""

    def __init__(self, client: JiraClient | None = None) -> None:
        self.client = client or JiraClient()

    async def inbound_assigned_issues(self, db) -> list[dict]:
        """Fetch assigned issues and record an inbound-sync event per issue.

        Returns issue dicts for the Executive Agent to decompose. Does NOT
        create Work Items automatically (Executive decides; plan §D3/D4).
        """
        issues = self.client.search_assigned_issues()
        out = []
        for issue in issues:
            await TelemetryService.record_event(
                db,
                event_type="JIRA_INBOUND_SYNCED",
                summary=f"Jira {issue.key} assigned: {issue.summary[:120]}",
                actor_source="jira_sync",
                metadata={"issue_key": issue.key, "status": issue.status,
                          "priority": issue.priority, "url": issue.url},
            )
            out.append({
                "key": issue.key, "summary": issue.summary,
                "description": issue.description, "status": issue.status,
                "priority": issue.priority, "labels": issue.labels,
                "url": issue.url, "correlation_tag": correlation_tag(issue.key),
            })
        return out

    async def outbound_summary(self, db, *, issue_key: str, kind: str, detail: str) -> str:
        """Post one lifecycle summary comment (started/milestone/blocked/pr/completed/handoff)."""
        body = f"[ACMS Executive] {kind.upper()}: {detail}"
        comment_id = self.client.add_comment(issue_key, body)
        await TelemetryService.record_event(
            db,
            event_type="JIRA_OUTBOUND_POSTED",
            summary=f"Jira {issue_key} <- {kind}: {detail[:120]}",
            actor_source="jira_sync",
            metadata={"issue_key": issue_key, "kind": kind, "comment_id": comment_id},
        )
        return comment_id
