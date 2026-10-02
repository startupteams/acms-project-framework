"""W6 gateway tests: jira.* capabilities (plan §28).

Covers: jira read resources (issue/comments/status/template), SAFE_WRITE
tools (comment/description/artifact-link/transition) with assignment-scoped
issue enforcement, plan §28 transition policy (worker TO START->IN PROGRESS
->IN REVIEW only; BLOCKED executive-only), global mutation-flag fail-closed,
ADF conversion (heading guard), and jira domain absent when unconfigured.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from mcp_gateway.audit import AuditLog
from mcp_gateway.errors import (ConflictError, OutOfScopeError,
                                RoleRequiredError, ValidationError_)
from mcp_gateway.jira_adapter import JiraMcpClient, _markdown_to_adf
from mcp_gateway.models import CallIdentity, TokenKind
from mcp_gateway.server import GatewayConfig, GatewayServer
from mcp_gateway.tokens import TokenStore

from tests.test_mcp_gateway_w4 import _asyncio_run, FakeAcms, make_identity


class FakeJira(JiraMcpClient):
    """Canned Jira REST surfaces. No network."""

    def __init__(self, *, status: str = "TO START", transitions: list[dict] | None = None):
        super().__init__("https://jira.example", "svc@example", "fake-token")
        self._status = status
        self._transitions = transitions if transitions is not None else [
            {"id": "11", "name": "In Progress", "to": "In Progress"},
            {"id": "21", "name": "In Review", "to": "In Review"},
            {"id": "31", "name": "Blocked", "to": "Blocked"},
        ]
        self.comments: list[dict] = []
        self.transitions_done: list[tuple[str, str]] = []
        self.descriptions: dict[str, str] = {}

    def issue_brief(self, issue_key: str) -> dict:
        return {
            "key": issue_key, "issue_id": "10001",
            "summary": f"Summary for {issue_key}",
            "description": "Template description",
            "status": self._status, "status_category": "To Do",
            "priority": "Medium", "labels": ["acms"],
            "assignee_display": "AI Service",
            "assignee_account_id": "712020:fake",
            "project_key": issue_key.split("-")[0], "issue_type": "Task",
            "updated": "2026-10-02T00:00:00.000+0000",
            "url": f"https://jira.example/browse/{issue_key}",
        }

    def get_comments(self, issue_key: str, max_results: int = 50) -> dict:
        return {"issue_key": issue_key, "total": len(self.comments),
                "comments": list(self.comments)[:max_results]}

    def add_comment(self, issue_key: str, body_markdown: str) -> dict:
        self.comments.append({"id": f"c{len(self.comments) + 1}",
                              "issue_key": issue_key, "body": body_markdown})
        return {"comment_id": f"c{len(self.comments)}", "issue_key": issue_key}

    def update_description(self, issue_key: str, description_markdown: str) -> dict:
        self.descriptions[issue_key] = description_markdown
        return {"updated": True, "issue_key": issue_key}

    def list_transitions(self, issue_key: str) -> list[dict]:
        return list(self._transitions)

    def transition(self, issue_key: str, target_status: str) -> dict:
        match = next((t for t in self._transitions
                      if t["name"].lower() == target_status.lower()
                      or t["to"].lower() == target_status.lower()), None)
        if match is None:
            raise ValidationError_(
                f"transition '{target_status}' not available for {issue_key}")
        self._status = match["to"]
        self.transitions_done.append((issue_key, match["to"]))
        return {"transitioned": True, "issue_key": issue_key,
                "target": target_status, "transition_id": match["id"]}


def build_server_at(tmp_path: Path, fake_jira, *, mutation_enabled: bool = True) -> GatewayServer:
    cfg = GatewayConfig(
        acms_base_url="http://127.0.0.1:9999", acms_token="fake",
        llm_base_url="http://127.0.0.1:8300", llm_token="fake-sm",
        approvals_path=str(tmp_path / "approvals.sqlite3"),
        jira_status_mutation_enabled=mutation_enabled,
    )
    tokens = TokenStore(str(tmp_path / "tokens.sqlite3"))
    audit = AuditLog(str(tmp_path / "activity.sqlite3"))
    return GatewayServer(cfg, tokens=tokens, audit=audit,
                         acms_client=FakeAcms(), llm_client=None,
                         jira_client=fake_jira)


def _call_tool(server, tool_name, identity, **kwargs):
    from mcp_gateway.server import CURRENT_IDENTITY
    storage_key = tool_name.replace(".", "_")
    gt = server.mcp._tool_manager._tools[storage_key]
    async def _go():
        token = CURRENT_IDENTITY.set(identity)
        try:
            return await gt.run(dict(kwargs), context=None)
        finally:
            CURRENT_IDENTITY.reset(token)
    return _asyncio_run(_go())


def _read_resource(server, resource_name, identity, params=None):
    cap = server.registry._capabilities[resource_name]
    return _asyncio_run(cap.handler(identity, params or {}))


def _worker(jira_key: str | None = "STNA-88"):
    return CallIdentity(token_kind=TokenKind.AGENT, token_id="t-w",
                        agent_name="uid-005", acms_agent_id="a-w5",
                        roles=["worker"],
                        scopes=["acms.read", "acms.write", "jira.read", "jira.write"],
                        jira_issue_key=jira_key)


def _exec():
    return CallIdentity(token_kind=TokenKind.AGENT, token_id="t-x",
                        agent_name="stea-004", acms_agent_id="a-x",
                        roles=["executive"],
                        scopes=["acms.read", "acms.write", "jira.read", "jira.write"])


def test_jira_domain_absent_when_unconfigured(tmp_path):
    cfg = GatewayConfig(acms_base_url="http://127.0.0.1:9999", acms_token="fake",
                        approvals_path=str(tmp_path / "a.sqlite3"))
    server = GatewayServer(cfg, tokens=TokenStore(str(tmp_path / "t.sqlite3")),
                           audit=AuditLog(str(tmp_path / "act.sqlite3")),
                           acms_client=FakeAcms(), llm_client=None)
    names = {c.name for c in server.registry._capabilities.values()}
    assert not any(n.startswith("jira.") for n in names)


def test_jira_resources_read(tmp_path):
    server = build_server_at(tmp_path, FakeJira())
    ident = _worker()
    issue = _read_resource(server, "jira.issue.get", ident, {"issue_key": "STNA-88"})
    assert issue["issue"]["key"] == "STNA-88"
    assert issue["issue"]["status"] == "TO START"
    status = _read_resource(server, "jira.status.get", ident, {"issue_key": "STNA-88"})
    assert status["mutation_enabled"] is True
    assert status["worker_allowed_transitions"]["TO START"] == ["IN PROGRESS"]
    comments = _read_resource(server, "jira.comments.list", ident, {"issue_key": "STNA-88"})
    assert comments["total"] == 0


def test_jira_read_out_of_scope_issue_denied(tmp_path):
    """Plan §31 golden test: worker cannot read out-of-scope context."""
    server = build_server_at(tmp_path, FakeJira())
    ident = _worker(jira_key="STNA-88")
    with pytest.raises(OutOfScopeError):
        _read_resource(server, "jira.issue.get", ident, {"issue_key": "STNA-99"})
    # the resource resolves URI params — call through the handler params path
    cap = server.registry._capabilities["jira.issue.get"]
    with pytest.raises(OutOfScopeError):
        _asyncio_run(cap.handler(ident, {"issue_key": "STNA-99"}))


def test_worker_no_assignment_no_jira_access(tmp_path):
    server = build_server_at(tmp_path, FakeJira())
    ident = _worker(jira_key=None)
    with pytest.raises(OutOfScopeError):
        _read_resource(server, "jira.issue.get", ident, {"issue_key": "STNA-88"})


def test_comment_add_assignment_scoped(tmp_path):
    server = build_server_at(tmp_path, FakeJira())
    jira = server.jira
    out = _call_tool(server, "jira_comment_add", _worker(),
                     issue_key="STNA-88", body_markdown="## Progress\n- done x")
    assert out["comment_id"] == "c1"
    assert len(jira.comments) == 1
    with pytest.raises(OutOfScopeError):
        _call_tool(server, "jira_comment_add", _worker(),
                   issue_key="OTHER-1", body_markdown="nope")


def test_transition_worker_allowed_paths(tmp_path):
    """Plan §28: implementation worker TO START -> IN PROGRESS -> IN REVIEW."""
    jira = FakeJira(status="TO START")
    server = build_server_at(tmp_path, jira)
    ident = _worker()
    out = _call_tool(server, "jira_transition_request", ident,
                     issue_key="STNA-88", target_status="IN PROGRESS")
    assert out["transitioned"] is True
    assert jira.transitions_done == [("STNA-88", "In Progress")]
    out2 = _call_tool(server, "jira_transition_request", ident,
                      issue_key="STNA-88", target_status="IN REVIEW")
    assert out2["transitioned"] is True
    assert jira.transitions_done[-1] == ("STNA-88", "In Review")


def test_transition_worker_illegal_jump_denied(tmp_path):
    """From TO START a worker may NOT skip to IN REVIEW (plan §28 policy)."""
    server = build_server_at(tmp_path, FakeJira(status="TO START"))
    with pytest.raises(RoleRequiredError):
        _call_tool(server, "jira_transition_request", _worker(),
                   issue_key="STNA-88", target_status="IN REVIEW")


def test_transition_worker_cannot_go_blocked(tmp_path):
    """Plan §31 golden test: ordinary worker NEVER transitions to BLOCKED."""
    server = build_server_at(tmp_path, FakeJira(status="IN PROGRESS"))
    with pytest.raises(RoleRequiredError) as exc:
        _call_tool(server, "jira_transition_request", _worker(),
                   issue_key="STNA-88", target_status="BLOCKED")
    assert "IN PROGRESS" in str(exc.value)  # actionable guidance present


def test_transition_executive_can_go_blocked(tmp_path):
    server = build_server_at(tmp_path, FakeJira(status="IN PROGRESS"))
    jira = server.jira
    out = _call_tool(server, "jira_transition_request", _exec(),
                     issue_key="STNA-88", target_status="BLOCKED")
    assert out["transitioned"] is True
    assert jira.transitions_done == [("STNA-88", "Blocked")]


def test_transition_fail_closed_when_mutation_disabled(tmp_path):
    """STNA-88 posture: global mutation flag off => fail-closed, actionable."""
    server = build_server_at(tmp_path, FakeJira(), mutation_enabled=False)
    with pytest.raises(ConflictError) as exc:
        _call_tool(server, "jira_transition_request", _worker(),
                   issue_key="STNA-88", target_status="IN PROGRESS")
    assert "ACMS_JIRA_STATUS_MUTATION_ENABLED" in str(exc.value)
    assert server.jira.transitions_done == []


def test_no_direct_clone_or_mutation_beyond_policy(tmp_path):
    """Plan §31: no out-of-policy Jira mutation surface exists."""
    names = {c.name for c in build_server_at(tmp_path, FakeJira())
             .registry._capabilities.values()}
    for banned in ("jira.issue.create", "jira.issue.delete",
                   "jira.project.create", "jira.transition.force"):
        assert banned not in names
    # template clone = durable approval request (executive), never a direct clone
    assert "jira.template.clone_request" in names


def test_template_clone_request_is_approval_path(tmp_path):
    server = build_server_at(tmp_path, FakeJira())
    out = _call_tool(server, "jira_template_clone_request", _exec(),
                     template_key="STNA-86", reason="authorized clone request W6")
    assert out["approval_request_id"].startswith("apr-")
    assert out["status"] == "PENDING"
    from mcp_gateway.errors import ApprovalRequiredError
    with pytest.raises(ApprovalRequiredError):
        _call_tool(server, "jira_template_clone_request", _worker(),
                   template_key="STNA-86", reason="worker attempt W6")


def test_markdown_to_adf_heading_contract():
    """ADF heading contract: type=heading + attrs.level (NOT heading2 — the
    live-400 class found 2026-10-02 on the STNA-88 BLUF)."""
    doc = _markdown_to_adf("## BLUF\n\n- item one\n- item two\n\n```python\nx = 1\n```")
    content = doc["content"]
    heading = content[0]
    assert heading["type"] == "heading"
    assert heading["attrs"]["level"] == 2
    bullets = [c for c in content if c["type"] == "bulletList"]
    assert len(bullets) == 1
    code = [c for c in content if c["type"] == "codeBlock"]
    assert len(code) == 1 and code[0]["content"][0]["text"] == "x = 1"


def test_artifact_link_posts_exact_url(tmp_path):
    server = build_server_at(tmp_path, FakeJira())
    jira = server.jira
    out = _call_tool(server, "jira_artifact_link", _worker(),
                     issue_key="STNA-88", artifact_uid="ACMS-ARTIFACT-000005",
                     url="https://10.0.20.122/ui/artifacts/ACMS-ARTIFACT-000005")
    assert out["comment_id"]
    assert "ACMS-ARTIFACT-000005" in jira.comments[0]["body"]
    assert "https://10.0.20.122/ui/artifacts/ACMS-ARTIFACT-000005" in jira.comments[0]["body"]