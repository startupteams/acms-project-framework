"""Context manifest (plan §14): ``mcp://acms/context/current``.

W1: the manifest is assembled from the assignment token's scope record plus
durable ACMS lookups via the ACMS adapter (Work item, Project, Jira identity).
``required_context`` lists pointers (titles + URLs), NOT full documents —
"do not dump every document into the initial prompt; let the agent fetch"
(plan §14). Fetching happens through the corresponding acms.* resources.
"""
from __future__ import annotations

from typing import Any

from .models import CallIdentity


def build_context_manifest(
    identity: CallIdentity,
    *,
    work: dict[str, Any] | None,
    project: dict[str, Any] | None,
    agent: dict[str, Any] | None,
    acms_base_url: str,
) -> dict[str, Any]:
    work = work or {}
    project = project or {}
    agent = agent or {}

    work_uid = identity.work_uid or work.get("work_uid") or work.get("work_key")
    jira = identity.jira_issue_key or work.get("jira_issue_key")

    manifest: dict[str, Any] = {
        "agent": {
            "name": identity.agent_name,
            "id": identity.acms_agent_id or agent.get("agent_id"),
            "roles": identity.roles,
        },
        "assignment": {
            "work_uid": work_uid,
            "jira": jira,
            "project": project.get("slug") or project.get("name"),
            "product": project.get("product_id"),
        },
        "repositories": identity.repositories or [],
        "required_context": [],
        "optional_context": [],
        "permissions": {
            "resources": [],
            "tools": [],
        },
        "approvals": {"required_for": []},
    }

    pointers = []
    if work_uid:
        pointers.append({
            "kind": "work",
            "ref": f"mcp://acms/work/{work_uid}",
            "title": work.get("title") or work_uid,
        })
    if project.get("project_id") or project.get("slug"):
        pid = project.get("project_id") or project.get("slug")
        pointers.append({
            "kind": "project",
            "ref": f"mcp://acms/project/{pid}",
            "title": project.get("name"),
        })
    if jira:
        pointers.append({
            "kind": "jira_description",
            "ref": f"mcp://acms/work/{work_uid}#jira",
            "title": f"Jira issue {jira} description (fetched from the linked Work record)",
        })
    manifest["required_context"] = pointers

    if agent:
        manifest["agent"]["trust_class"] = agent.get("trust_class")
        manifest["agent"]["harness"] = agent.get("harness")

    manifest["_acms_base_url"] = acms_base_url
    return manifest
