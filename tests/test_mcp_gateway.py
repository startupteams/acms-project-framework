"""Golden security + behavior tests for the MIAM MCP gateway (plan §31 subset).

W1 scope: identity/token model, scope enforcement, policy denials, audit,
context manifest, and the MCP protocol E2E flow (stateless HTTP).
"""
from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path
from typing import Any

import httpx
import pytest

from mcp_gateway.audit import AuditLog
from mcp_gateway.errors import (
    ApprovalRequiredError,
    DestructiveDenyError,
    OutOfScopeError,
    RoleRequiredError,
    ScopeRequiredError,
    UnauthenticatedError,
    ExpiredAgentTokenError,
    RevokedAgentTokenError,
    ExpiredAssignmentTokenError,
)
from mcp_gateway.models import (
    AgentTokenRecord,
    AssignmentTokenRecord,
    CallIdentity,
    RiskClass,
    TokenKind,
    utcnow,
)
from mcp_gateway.policy import Capability, PolicyRegistry, match_uri_template
from mcp_gateway.server import CURRENT_IDENTITY, GatewayConfig, GatewayServer
from mcp_gateway.tokens import TokenStore
from datetime import datetime, timedelta, timezone


# ---------------------------------------------------------------- fixtures


class FakeAcms:
    """In-memory ACMS double exercising the same REST surface the adapter uses."""

    def __init__(self, *, agents=None, work_items=None, assignments=None,
                 projects=None, products=None, artifacts=None, inbox=None,
                 tasks=None):
        self.agents = agents or {}
        self.work_items = work_items or {}
        self.assignments = assignments or []
        self.projects = projects or {}
        self.products = products or {}
        self.artifacts = artifacts or {}
        self.inbox = inbox or []
        self.tasks = tasks or {}
        self.calls: list[tuple[str, str]] = []
        self.created_artifacts: list[dict] = []
        self.created_handoffs: list[dict] = []
        self.status_updates: list[tuple[str, str]] = []
        self.events: list[dict] = []

    def request(self, method: str, path: str, payload=None, query=None):
        self.calls.append((method, path))
        if path == "/api/v1/work/items" and method == "GET":
            return 200, list(self.work_items.values())
        if path.startswith("/api/v1/work/items/") and method == "GET":
            ref = path.rsplit("/", 1)[1]
            item = self._find_work(ref)
            return (200, item) if item else (404, None)
        if path.startswith("/api/v1/work/items/") and method == "PATCH":
            ref = path.rsplit("/", 1)[1]
            item = self._find_work(ref)
            if item is None:
                return 404, None
            if payload and "status" in payload:
                item["status"] = payload["status"]
                self.status_updates.append((item["work_uid"], payload["status"]))
            return 200, item
        if path == "/api/v1/work/assignments" and method == "GET":
            q = query or {}
            rows = [a for a in self.assignments
                    if (not q.get("agent_id") or a.get("agent_id") == q["agent_id"])
                    and (not q.get("status") or a.get("status") == q["status"])]
            return 200, rows
        if path == "/api/v1/work/handoffs" and method == "POST":
            self.created_handoffs.append(payload)
            return 201, {"handoff_id": "ho-123", **payload}
        if path == "/artifacts" and method == "POST":
            art = {"artifact_id": "art-uuid-1", "artifact_uid": "ACMS-ARTIFACT-000006-x",
                   "sha256": "abc", **payload}
            self.created_artifacts.append(art)
            return 201, art
        if path == "/inbox" and method == "POST":
            item = {"item_id": "inbox-1", **payload}
            self.inbox.append(item)
            return 201, item
        if path == "/inbox" and method == "GET":
            return 200, self.inbox
        if path.startswith("/execution/") and path.endswith("/events") and method == "POST":
            self.events.append(payload)
            return 202, {"accepted": True}
        if path.startswith("/api/v1/agents/") and method == "GET":
            ref = path.rsplit("/", 1)[1]
            for a in self.agents.values():
                if ref in (a.get("agent_id"), a.get("display_name")):
                    return 200, a
            return 404, None
        if path == "/api/v1/agents" and method == "GET":
            return 200, list(self.agents.values())
        if path.startswith("/projects/") and method == "GET":
            ref = path.rsplit("/", 1)[1]
            for p in self.projects.values():
                if ref in (p.get("project_id"), p.get("slug"), p.get("name")):
                    return 200, p
            return 404, None
        if path == "/projects" and method == "GET":
            return 200, list(self.projects.values())
        if path == "/api/v1/work/tasks" and method == "GET":
            q = query or {}
            rows = [t for t in self.tasks.values()
                    if (not q.get("work_item_id") or t.get("work_item_id") == q["work_item_id"])]
            return 200, rows
        if path.startswith("/products/") and method == "GET":
            ref = path.rsplit("/", 1)[1]
            p = self.products.get(ref)
            return (200, p) if p else (404, None)
        if path.startswith("/artifacts/") and path.endswith("/content") and method == "GET":
            aid = path.split("/")[2]
            for a in self.artifacts.values():
                if aid in (a.get("artifact_id"), a.get("artifact_uid")):
                    return 200, a.get("content", "")
            return 404, None
        if path.startswith("/artifacts/") and method == "GET":
            ref = path.split("/")[2]
            for a in self.artifacts.values():
                if ref in (a.get("artifact_id"), a.get("artifact_uid"), a.get("sha256", "")[:8]):
                    return 200, a
            return 404, None
        if path == "/artifacts" and method == "GET":
            return 200, list(self.artifacts.values())
        return 404, None

    def _find_work(self, ref: str):
        for w in self.work_items.values():
            if ref in (w.get("work_item_id"), w.get("work_key"), w.get("work_uid")):
                return w
        return None

    # ---- adapter-facing convenience methods (mirror AcmsClient API) ----

    def find_work_item(self, ref: str):
        item = self._find_work(ref)
        return item

    def active_assignment(self, agent_id: str):
        for a in self.assignments:
            if a.get("agent_id") == agent_id and a.get("status") == "ACTIVE":
                return a
        return None

    def get_project(self, ref: str):
        for p in self.projects.values():
            if ref in (p.get("project_id"), p.get("slug"), p.get("name")):
                return p
        return None

    def get_product(self, product_id: str):
        return self.products.get(product_id)

    def find_artifact(self, ref: str, *, work_item_id: str | None = None):
        for a in self.artifacts.values():
            if ref in (a.get("artifact_id"), a.get("artifact_uid")):
                return a
        return None

    def artifact_content(self, artifact_id: str):
        for a in self.artifacts.values():
            if artifact_id in (a.get("artifact_id"), a.get("artifact_uid")):
                return a.get("content", "")
        return None

    def get_agent(self, ref: str):
        for a in self.agents.values():
            if ref in (a.get("agent_id"), a.get("display_name")):
                return a
        return None

    def _running_task_for(self, work_item_id: str):
        for t in self.tasks.values():
            if t.get("work_item_id") == work_item_id and t.get("status") == "RUNNING":
                return t
        return None


@pytest.fixture()
def gateway_env():
    """A gateway server wired to a FakeAcms with: one project, one work item,
    one active assignment for worker uid-005, one artifact, one task RUNNING."""
    tmp = Path(tempfile.mkdtemp())

    agent_id_005 = "0f0f0f0f-0055-4000-8000-000000000005"
    project = {"project_id": "proj-1", "name": "ACMS Control Plane",
               "slug": "acms-control-plane", "jira_project_key": "STNA"}
    work = {
        "work_item_id": "work-uuid-17",
        "work_key": "ACMS-WORK-000017",
        "work_uid": "ACMS-WORK-000017-20261002_063208",
        "title": "STNA-88 implementation",
        "status": "active",
        "project_id": "proj-1",
        "jira_issue_key": "STNA-88",
        "kind": "task",
    }
    assignment = {
        "assignment_id": "asg-9ca62464", "agent_id": agent_id_005,
        "work_item_id": "work-uuid-17", "status": "ACTIVE",
    }
    artifact = {
        "artifact_id": "art-uuid-5", "artifact_uid": "ACMS-ARTIFACT-000005-20261002_064110",
        "work_item_id": "work-uuid-17", "title": "Canonical handoff",
        "artifact_type": "handoff", "content": "# BLUF\nartifact body", "sha256": "deadbeef",
    }
    task = {"task_id": "task-62784f59", "work_item_id": "work-uuid-17",
            "agent_id": agent_id_005, "status": "RUNNING"}

    fake = FakeAcms(
        agents={agent_id_005: {"agent_id": agent_id_005,
                               "display_name": "acms-hermes-worker-uid-005",
                               "trust_class": "internal", "harness": "hermes",
                               "bridge_version": "0.19.0", "protocol_version": "1"}},
        work_items={"work-uuid-17": work},
        assignments=[assignment],
        projects={"proj-1": project},
        products={},
        artifacts={"art-uuid-5": artifact},
        inbox=[],
        tasks={"task-62784f59": task},
    )

    cfg = GatewayConfig(
        acms_base_url="https://acms.test", acms_token="dummy-gw",
        acms_insecure_tls=False,
        tokens_path=str(tmp / "tokens.sqlite3"),
        audit_path=str(tmp / "audit.sqlite3"),
    )
    store = TokenStore(cfg.tokens_path)
    audit = AuditLog(cfg.audit_path)
    # inject the ACMS double BEFORE capability construction (resolvers close
    # over the client at build time — lazy attribute injection does NOT work)
    server = GatewayServer(cfg, tokens=store, audit=audit, acms_client=fake)

    rec_agent, agent_raw = store.mint_agent_token(
        "acms-hermes-worker-uid-005", acms_agent_id=agent_id_005,
    )
    rec_exec, exec_raw = store.mint_agent_token(
        "stea-004-executive", acms_agent_id=None,
        roles=["executive", "infrastructure_admin"],
    )
    rec_asg, asg_raw = store.mint_assignment_token(
        agent_name="acms-hermes-worker-uid-005",
        work_uid=work["work_uid"], agent_token_raw=agent_raw,
        ttl_hours=8, project_id="proj-1", project_slug="acms-control-plane",
        jira_issue_key="STNA-88", repositories=["startupteams/acms-project-framework"],
    )
    return {
        "server": server, "store": store, "audit": audit, "fake": fake,
        "agent_raw": agent_raw, "exec_raw": exec_raw, "asg_raw": asg_raw,
        "work": work, "project": project, "assignment": assignment,
        "artifact": artifact, "agent_id_005": agent_id_005, "config": cfg,
    }


ACCEPT = {"Accept": "application/json, text/event-stream"}

import contextlib


@contextlib.asynccontextmanager
async def gateway_sessions(server, tokens: list[str | None]):
    """ONE session-manager run; yields one httpx client per token (order kept).
    The SDK manager refuses multiple .run() calls per instance, so a test that
    needs several token identities must share a single run (matches production:
    the service enters run() exactly once for the process lifetime)."""
    transport = httpx.ASGITransport(app=server.app)
    cm = server.mcp.session_manager
    async with cm.run():
        clients = []
        for tok in tokens:
            hdrs = dict(ACCEPT)
            if tok:
                hdrs["Authorization"] = f"Bearer {tok}"
            clients.append(httpx.AsyncClient(transport=transport, base_url="http://gw.test",
                                             headers=hdrs))
        try:
            yield clients if len(clients) > 1 else clients[0]
        finally:
            for c in clients:
                await c.aclose()


@contextlib.asynccontextmanager
async def gateway_client(server, token: str | None = None, *, headers: dict | None = None):
    """Single-identity convenience wrapper around gateway_sessions."""
    # header overrides unsupported in the shared-run path; tests needing custom
    # headers should build their own client inside gateway_sessions.
    async with gateway_sessions(server, [token]) as client:
        yield client


async def mcp_post(client, payload: dict) -> httpx.Response:
    return await client.post("/mcp", json=payload)


def json_body(resp) -> dict:
    return json.loads(resp.text)


# ---------------------------------------------------------------- token store


def test_agent_token_mint_and_resolution(gateway_env):
    store = gateway_env["store"]
    rec, raw = store.mint_agent_token("w-test")
    assert raw.startswith("mcp_")
    assert rec.token_hash != raw  # hash only
    resolved = store.resolve_agent_token(raw)
    assert resolved is not None and resolved.agent_name == "w-test"
    assert store.resolve_agent_token("bogus") is None


def test_agent_token_expiry_and_revocation(gateway_env):
    store = gateway_env["store"]
    rec, raw = store.mint_agent_token("w-exp", ttl_days=0)
    # ttl 0 -> expires immediately (created_at + 0)
    assert store.resolve_agent_token(raw).is_active() is False
    rec2, raw2 = store.mint_agent_token("w-rev")
    ok = store.revoke_agent_token(rec2.token_id, "operator test")
    assert ok
    assert store.resolve_agent_token(raw2).revoked_at is not None
    assert store.resolve_agent_token(raw2).is_active() is False


def test_assignment_token_binds_work_and_expiry():
    tmp = Path(tempfile.mkdtemp())
    store = TokenStore(tmp / "t.sqlite3")
    agent_rec, agent_raw = store.mint_agent_token("w-bind")
    rec, raw = store.mint_assignment_token(
        agent_name="w-bind", work_uid="ACMS-WORK-000001-x", agent_token_raw=agent_raw,
        ttl_hours=2,
    )
    assert rec.work_uid == "ACMS-WORK-000001-x"
    assert rec.is_active()
    # wrong-agent binding refused
    with pytest.raises(PermissionError):
        store.mint_assignment_token(
            agent_name="someone-else", work_uid="W", agent_token_raw=agent_raw)
    # mint without auth refused
    with pytest.raises(PermissionError):
        store.mint_assignment_token(agent_name="w-bind", work_uid="W")


# ---------------------------------------------------------------- policy


def test_policy_risk_gates():
    identity_worker = CallIdentity(
        token_kind=TokenKind.AGENT, token_id="t", agent_name="w",
        roles=["worker"], scopes=["acms.read", "acms.write"])
    destructive = Capability(
        name="proxmox.vm.delete", domain="proxmox", risk=RiskClass.DESTRUCTIVE,
        roles=set(), scopes=set())
    sensitive = Capability(
        name="runtime.restart_self", domain="runtime", risk=RiskClass.SENSITIVE_WRITE,
        roles={"worker"}, scopes=set())
    read = Capability(
        name="acms.work.get", domain="acms", risk=RiskClass.READ,
        roles={"worker"}, scopes={"acms.read"})

    with pytest.raises(DestructiveDenyError):
        destructive.authorize(identity_worker)
    with pytest.raises(ApprovalRequiredError):
        sensitive.authorize(identity_worker)
    read.authorize(identity_worker)  # no raise

    identity_exec = CallIdentity(
        token_kind=TokenKind.AGENT, token_id="t2", agent_name="exec",
        roles=["executive"], scopes=["acms.read"])
    # executive still denied destructive in W1 (explicit-human-approval rule)
    with pytest.raises(DestructiveDenyError):
        destructive.authorize(identity_exec)
    sensitive.authorize(identity_exec)  # executive passes sensitive gate


def test_policy_role_and_scope_errors():
    identity = CallIdentity(token_kind=TokenKind.AGENT, token_id="t",
                            agent_name="w", roles=["observer"], scopes=["acms.read"])
    cap = Capability(name="acms.work.update_progress", domain="acms",
                     risk=RiskClass.SAFE_WRITE, roles={"worker"},
                     scopes={"acms.write"})
    with pytest.raises(RoleRequiredError):
        cap.authorize(identity)
    identity2 = CallIdentity(token_kind=TokenKind.AGENT, token_id="t",
                             agent_name="w", roles=["worker"], scopes=["acms.read"])
    with pytest.raises(ScopeRequiredError):
        cap.authorize(identity2)


def test_uri_template_matching():
    tpl = "acms://work/{work_uid_or_id}"
    assert match_uri_template(tpl, "acms://work/ACMS-WORK-000017-x") ==         {"work_uid_or_id": "ACMS-WORK-000017-x"}
    assert match_uri_template(tpl, "acms://work/a/b") is None
    assert match_uri_template("acms://agent/self", "acms://agent/self") == {}


# ---------------------------------------------------------------- protocol E2E
async def test_health_is_public(gateway_env):
    server = gateway_env["server"]
    transport = httpx.ASGITransport(app=server.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://gw.test") as client:
        r = await client.get("/health", headers=ACCEPT)
        assert r.status_code == 200
        body = json_body(r)
        assert body["status"] == "ok" and body["gateway"] == "miam-mcp-gateway"


async def test_unauthenticated_request_is_401_and_audited(gateway_env):
    server = gateway_env["server"]
    audit = gateway_env["audit"]
    async with gateway_client(server, None) as client:
        r = await mcp_post(client, {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
    assert r.status_code == 401
    assert "UNAUTHENTICATED" in r.text
    assert audit.count() >= 1
    assert len(audit.recent(denied_only=True)) >= 1


async def test_invalid_token_is_401(gateway_env):
    server = gateway_env["server"]
    async with gateway_client(server, "mcp_totallyfake") as client:
        r = await mcp_post(client, {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
    assert r.status_code == 401


async def test_tools_list_and_dotted_names(gateway_env):
    server = gateway_env["server"]
    async with gateway_client(server, gateway_env["agent_raw"]) as client:
        r = await mcp_post(client, {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
    assert r.status_code == 200
    tools = json_body(r)["result"]["tools"]
    names = {t["name"] for t in tools}
    assert "acms_work_update_progress" in names
    canonical = {t["_meta"]["gateway_canonical_name"] for t in tools if t.get("_meta")}
    assert "acms.work.update_progress" in canonical


async def test_resources_listed(gateway_env):
    server = gateway_env["server"]
    async with gateway_client(server, gateway_env["agent_raw"]) as client:
        r = await mcp_post(client, {"jsonrpc": "2.0", "id": 1, "method": "resources/list", "params": {}})
        assert r.status_code == 200
        res = json_body(r)["result"]
        # SDK: concrete resources in resources/list; parametrized URI templates in
        # resources/templates/list (MCP spec separation).
        names = [t["name"] for t in res["resources"]]
        assert "acms_agent_self" in names
        assert "acms_context_current" in names
        r2 = await mcp_post(client, {"jsonrpc": "2.0", "id": 2,
                                     "method": "resources/templates/list", "params": {}})
        assert r2.status_code == 200
        tnames = [t["name"] for t in json_body(r2)["result"]["resourceTemplates"]]
        assert "acms_work_get" in tnames
        assert "acms_project_get" in tnames


async def test_worker_reads_own_work_resource(gateway_env):
    server = gateway_env["server"]
    env = gateway_env
    async with gateway_client(server, env["agent_raw"]) as client:
        r = await mcp_post(client, {"jsonrpc": "2.0", "id": 1, "method": "resources/read",
                                    "params": {"uri": f"acms://work/{env['work']['work_uid']}"}})
    assert r.status_code == 200
    result = json_body(r)["result"]
    payload = json.loads(result["contents"][0]["text"])
    assert payload["work"]["work_uid"] == env["work"]["work_uid"]


async def test_worker_reads_context_manifest(gateway_env):
    server = gateway_env["server"]
    env = gateway_env
    async with gateway_client(server, env["agent_raw"]) as client:
        r = await mcp_post(client, {"jsonrpc": "2.0", "id": 1, "method": "resources/read",
                                    "params": {"uri": "acms://context/current"}})
    assert r.status_code == 200
    payload = json.loads(json_body(r)["result"]["contents"][0]["text"])
    manifest = payload["manifest"]
    assert manifest["agent"]["name"] == "acms-hermes-worker-uid-005"
    assert manifest["assignment"]["work_uid"] == env["work"]["work_uid"]
    assert manifest["assignment"]["jira"] == "STNA-88"
    assert manifest["assignment"]["project"] == "acms-control-plane"
    refs = [p["ref"] for p in manifest["required_context"]]
    assert any(p.startswith("mcp://acms/work/") for p in refs)


async def test_agent_self_resource(gateway_env):
    server = gateway_env["server"]
    env = gateway_env
    async with gateway_client(server, env["agent_raw"]) as client:
        r = await mcp_post(client, {"jsonrpc": "2.0", "id": 1, "method": "resources/read",
                                    "params": {"uri": "acms://agent/self"}})
    assert r.status_code == 200
    payload = json.loads(json_body(r)["result"]["contents"][0]["text"])
    assert payload["agent"]["display_name"] == "acms-hermes-worker-uid-005"


async def test_assignment_scoped_token_cannot_read_other_work(gateway_env):
    server = gateway_env["server"]
    env = gateway_env
    env["fake"].work_items["work-other"] = {
        "work_item_id": "work-other", "work_uid": "ACMS-WORK-000099-x",
        "work_key": "ACMS-WORK-000099", "title": "other", "status": "active",
    }
    async with gateway_client(server, env["asg_raw"]) as client:
        r = await mcp_post(client, {"jsonrpc": "2.0", "id": 1, "method": "resources/read",
                                    "params": {"uri": "acms://work/ACMS-WORK-000099-x"}})
    # resource-read failures surface as JSON-RPC ERRORS (protocol-correct for
    # resources; the isError+content shape only applies to tools/call).
    assert r.status_code == 200
    body = json_body(r)
    assert "error" in body
    assert "OUT_OF_SCOPE" in body["error"]["message"]


async def test_worker_tool_submit_handoff_creates_handoff(gateway_env):
    server = gateway_env["server"]
    env = gateway_env
    async with gateway_client(server, env["agent_raw"]) as client:
        r = await mcp_post(client, {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                    "params": {"name": "acms_work_submit_handoff",
                                               "arguments": {"title": "Handoff from MCP",
                                                             "body_markdown": "# done\nAll good."}}})
    assert r.status_code == 200
    result = json_body(r)["result"]
    assert result["isError"] is False
    structured = result["structuredContent"]
    assert structured["handoff_id"] == "ho-123"
    assert env["fake"].created_handoffs[0]["work_item_id"] == "work-uuid-17"


async def test_worker_tool_create_artifact(gateway_env):
    server = gateway_env["server"]
    env = gateway_env
    async with gateway_client(server, env["agent_raw"]) as client:
        r = await mcp_post(client, {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                    "params": {"name": "acms_artifact_create",
                                               "arguments": {"title": "Evidence",
                                                             "content_markdown": "# proof"}}})
    assert r.status_code == 200
    result = json_body(r)["result"]
    assert result["isError"] is False
    assert result["structuredContent"]["artifact_uid"].startswith("ACMS-ARTIFACT-")
    assert env["fake"].created_artifacts[0]["work_item_id"] == "work-uuid-17"


async def test_worker_tool_update_progress_requires_running_task(gateway_env):
    server = gateway_env["server"]
    env = gateway_env
    async with gateway_client(server, env["agent_raw"]) as client:
        r = await mcp_post(client, {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                    "params": {"name": "acms_work_update_progress",
                                               "arguments": {"note": "implemented X"}}})
        assert r.status_code == 200
        result = json_body(r)["result"]
        assert result["isError"] is False
        assert result["structuredContent"]["recorded"] is True
        assert env["fake"].events[-1]["kind"] == "PROGRESS_NOTE"
        env["fake"].tasks.clear()
        r2 = await mcp_post(client, {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                                     "params": {"name": "acms_work_update_progress",
                                                "arguments": {"note": "again"}}})
    result2 = json_body(r2)["result"]
    assert result2["isError"] is True
    assert "CONFLICT" in result2["content"][0]["text"]


async def test_worker_review_request_marks_work_in_review(gateway_env):
    server = gateway_env["server"]
    env = gateway_env
    async with gateway_client(server, env["agent_raw"]) as client:
        r = await mcp_post(client, {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                    "params": {"name": "acms_review_request",
                                               "arguments": {"note": "ready"}}})
    assert r.status_code == 200
    result = json_body(r)["result"]
    assert result["isError"] is False
    assert ("ACMS-WORK-000017-20261002_063208", "in_review") in env["fake"].status_updates


async def test_worker_inbox_raise(gateway_env):
    server = gateway_env["server"]
    env = gateway_env
    async with gateway_client(server, env["agent_raw"]) as client:
        r = await mcp_post(client, {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                    "params": {"name": "acms_inbox_raise",
                                               "arguments": {"title": "Need decision",
                                                             "summary": "Choose A or B please",
                                                             "severity": "high"}}})
    assert r.status_code == 200
    result = json_body(r)["result"]
    assert result["isError"] is False
    assert env["fake"].inbox[-1]["severity"] == "high"


async def test_audit_records_ok_and_denied(gateway_env):
    server = gateway_env["server"]
    env = gateway_env
    rec, raw2 = env["store"].mint_agent_token("no-assignment-worker")
    async with gateway_sessions(server, [env["agent_raw"], raw2]) as (client, client2):
        await mcp_post(client, {"jsonrpc": "2.0", "id": 1, "method": "resources/read",
                                "params": {"uri": f"acms://work/{env['work']['work_uid']}"}})
        await mcp_post(client2, {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                                 "params": {"name": "acms_review_request", "arguments": {}}})
    rows = env["audit"].recent(limit=50)
    statuses = [r["status"] for r in rows]
    assert "ok" in statuses and "denied" in statuses
    denied_rows = env["audit"].recent(denied_only=True)
    assert any(d["name"] == "acms.review.request" for d in denied_rows)
    dumped = json.dumps(rows, default=str)
    assert env["agent_raw"] not in dumped and raw2 not in dumped


async def test_expired_and_revoked_tokens_fail_closed(gateway_env):
    server = gateway_env["server"]
    store = gateway_env["store"]
    rec, raw = store.mint_agent_token("w-exp2", ttl_days=0)
    rec2, raw2 = store.mint_agent_token("w-rev2")
    store.revoke_agent_token(rec2.token_id, "revoked test")
    async with gateway_sessions(server, [raw, raw2]) as (client, client2):
        r = await mcp_post(client, {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
        assert r.status_code == 401
        assert "EXPIRED" in r.text
        r2 = await mcp_post(client2, {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        assert r2.status_code == 401
        assert "REVOKED" in r2.text


async def test_expired_assignment_token_fails(gateway_env):
    server = gateway_env["server"]
    store = gateway_env["store"]
    rec, raw = store.mint_assignment_token(
        agent_name="acms-hermes-worker-uid-005", work_uid="ACMS-WORK-000017-20261002_063208",
        agent_token_raw=gateway_env["agent_raw"], ttl_hours=0)
    async with gateway_client(server, raw) as client:
        r = await mcp_post(client, {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
    assert r.status_code == 401
    assert "EXPIRED" in r.text


async def test_executive_reads_policy_current(gateway_env):
    server = gateway_env["server"]
    env = gateway_env
    async with gateway_client(server, env["exec_raw"]) as client:
        r = await mcp_post(client, {"jsonrpc": "2.0", "id": 1, "method": "resources/read",
                                    "params": {"uri": "acms://policy/current"}})
    assert r.status_code == 200
    payload = json.loads(json_body(r)["result"]["contents"][0]["text"])
    roles = payload["identity"]["roles"]
    assert "executive" in roles
    assert any("proxmox.vm.delete" in d for d in payload["denied_forever"])


async def test_multi_tenant_isolation_interleaved(gateway_env):
    """Two identities interleaved: each call sees its own identity (contextvar isolation)."""
    server = gateway_env["server"]
    env = gateway_env
    store = env["store"]
    rec_b, raw_b = store.mint_agent_token("worker-b")
    seen = []
    async with gateway_sessions(server, [env["agent_raw"], raw_b]) as (client_a, client_b):
        for client in (client_a, client_b, client_a):
                r = await mcp_post(client, {"jsonrpc": "2.0", "id": 1, "method": "resources/read",
                                            "params": {"uri": "acms://agent/self"}})
                payload = json.loads(json_body(r)["result"]["contents"][0]["text"])
                seen.append(payload["identity"]["agent_name"])
    assert seen == ["acms-hermes-worker-uid-005", "worker-b", "acms-hermes-worker-uid-005"]

