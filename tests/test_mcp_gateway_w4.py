"""W4 gateway tests: proxmox.sandbox.* capabilities (plan §26/§31).

Covers: sandbox status resource (own only), fleet view role-gated, sandbox
create (SENSITIVE_WRITE approval path), start/stop/extend/destroy-own
ownership enforcement (foreign sandbox denied), deployment.request durable
approval, unknown-args rejection, absence of generic PVE/delete tools.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from mcp_gateway.audit import AuditLog
from mcp_gateway.models import CallIdentity, TokenKind
from mcp_gateway.proxmox_adapter import SandboxClient, sandbox_name
from mcp_gateway.server import GatewayConfig, GatewayServer
from mcp_gateway.tokens import TokenStore


# ------------------------------------------------------------------ fakes

class FakeSandboxClient(SandboxClient):
    """Records calls; canned ARM runtime shapes. No network."""

    def __init__(self):
        super().__init__(base_url="http://127.0.0.1:8300", token="fake-sm")
        self.calls: list[tuple] = []
        self.runtimes: list[dict] = []
        self.jobs: list[dict] = []
        self.desired: list[tuple] = []
        self._next = 1

    def _runtime(self, name, agent_id, cls="sandbox"):
        self._next += 1
        return {"runtime_id": f"rt-{self._next:03d}", "name": name,
                "acms_agent_id": agent_id, "runtime_class": cls,
                "desired_state": "DESIRED_RUNNING", "actual_state": "ABSENT",
                "node": None, "vmid": None}

    def list_runtimes(self, acms_agent_id=None):
        self.calls.append(("list", acms_agent_id))
        return list(self.runtimes)

    def get_runtime(self, runtime_id):
        self.calls.append(("get", runtime_id))
        for r in self.runtimes:
            if r["runtime_id"] == runtime_id:
                return r
        from mcp_gateway.errors import NotFoundError
        raise NotFoundError("runtime not found")

    def create_sandbox(self, *, acms_agent_id, request_id, name, ttl_hours):
        self.calls.append(("create", acms_agent_id, request_id, name, ttl_hours))
        rt = self._runtime(name, acms_agent_id)
        self.runtimes.append(rt)
        job = {"job_id": "job-1", "request_id": request_id,
               "runtime_id": rt["runtime_id"], "state": "QUEUED", "steps": []}
        self.jobs.append(job)
        return job

    def set_desired_state(self, runtime_id, desired_state, reason):
        self.calls.append(("desired", runtime_id, desired_state, reason))
        self.desired.append((runtime_id, desired_state))
        for r in self.runtimes:
            if r["runtime_id"] == runtime_id:
                r["desired_state"] = desired_state
        return {"runtime_id": runtime_id, "desired": desired_state}

    def extend_ttl(self, runtime_id, ttl_hours, reason):
        self.calls.append(("extend", runtime_id, ttl_hours, reason))
        return {"runtime_id": runtime_id, "ttl_hours": ttl_hours,
                "sandbox_expires_at": "2026-10-03T00:00:00+00:00"}


class FakeAcms:
    def active_assignment(self, agent_id):
        return {"work_item_id": "wi-1"}

    def find_work_item(self, work_item_id):
        if work_item_id == "wi-1":
            return {"work_item_id": "wi-1", "work_uid": "ACMS-WORK-000042-20261002_010203"}
        return None

    def request(self, *a, **k):
        return 404, None


def make_identity(kind=TokenKind.ASSIGNMENT, agent="acms-hermes-worker-uid-005",
                  work_uid="ACMS-WORK-000042-20261002_010203",
                  roles=("worker",)):
    return CallIdentity(
        token_kind=kind, token_id="tok-1", agent_name=agent,
        acms_agent_id="11111111-1111-1111-1111-111111111111" if kind == TokenKind.AGENT else None,
        roles=list(roles),
        scopes=["acms.read", "acms.write", "proxmox.read", "proxmox.write",
                "llm.read", "llm.write", "runtime.write", "github.read", "github.write",
                "power.read"],
        work_uid=work_uid if kind == TokenKind.ASSIGNMENT else None,
        repositories=["startupteams/acms-project-framework"] if kind == TokenKind.ASSIGNMENT else [],
    )


def build_server_at(tmp_path: Path, fake_sbx) -> GatewayServer:
    cfg = GatewayConfig(
        acms_base_url="http://127.0.0.1:9999", acms_token="fake",
        llm_base_url="http://127.0.0.1:8300", llm_token="fake-sm",
        approvals_path=str(tmp_path / "approvals.sqlite3"),
    )
    tokens = TokenStore(str(tmp_path / "tokens.sqlite3"))
    audit = AuditLog(str(tmp_path / "activity.sqlite3"))
    return GatewayServer(cfg, tokens=tokens, audit=audit,
                         acms_client=FakeAcms(), llm_client=None,
                         github_client=None, sandbox_client=fake_sbx)


def _run(coro):
    import asyncio
    return asyncio.get_event_loop().run_until_complete(coro) if False else _asyncio_run(coro)


def _asyncio_run(coro):
    import asyncio
    try:
        loop = asyncio.get_event_loop()
        if loop.is_closed():
            raise RuntimeError
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    return loop.run_until_complete(coro)


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
    return _run(_go())


def _read_resource(server, resource_name, identity):
    """Read a STATIC proxmox resource via its registry handler (identity, {})."""
    cap = server.registry._capabilities[resource_name]
    return _run(cap.handler(identity, {}))



def _arm_grant(server, agent_name, capability):
    """Arm a one-time TTL-bounded grant (executive approval path, W2 store)."""
    req = server.approvals.create(agent_name=agent_name, capability=capability,
                                  reason="test grant", args_hash="x")
    out = server.approvals.decide(req["request_id"], decision="APPROVED",
                                  decided_by="exec-test")
    assert out["status"] == "APPROVED"

# ------------------------------------------------------------------ unit

def test_sandbox_name_shape():
    n = sandbox_name("acms-hermes-worker-uid-005", "ACMS-WORK-000042-20261002_010203")
    assert n == "sbx-acms-work-000042-20261002-010203-acms-hermes-worker-uid-005"
    assert len(n) <= 63 and " " not in n and "/" not in n and "_" not in n


def test_domain_absent_when_sm_unconfigured(tmp_path):
    cfg = GatewayConfig(acms_base_url="http://127.0.0.1:9999", acms_token="fake",
                        approvals_path=str(tmp_path / "appr.sqlite3"))
    tokens = TokenStore(str(tmp_path / "t.sqlite3"))
    audit = AuditLog(str(tmp_path / "a.sqlite3"))
    server = GatewayServer(cfg, tokens=tokens, audit=audit,
                           acms_client=FakeAcms(), llm_client=None)
    names = {c.name for c in server.registry._capabilities.values()}
    assert not any(n.startswith("proxmox.") for n in names)


# ------------------------------------------------------------------ resources

def test_sandbox_status_own_only(tmp_path):
    sbx = FakeSandboxClient()
    own_name = sandbox_name("acms-hermes-worker-uid-005", "ACMS-WORK-000042-20261002_010203")
    sbx.runtimes.append(sbx._runtime(own_name, "11111111-1111-1111-1111-111111111111"))
    # another agent's sandbox
    sbx.runtimes.append(sbx._runtime("sbx-other-uid-999-acms-hermes-worker-uid-004",
                                     "22222222-2222-2222-2222-222222222222"))
    server = build_server_at(tmp_path, sbx)
    identity = make_identity()
    out = _read_resource(server, "proxmox.sandbox.status", identity)
    assert out["sandbox_name"] == own_name
    names = [s["name"] for s in out["sandboxes"]]
    assert names == [own_name]  # foreign sandbox NOT included


def test_fleet_status_role_gated(tmp_path):
    sbx = FakeSandboxClient()
    sbx.runtimes.append(sbx._runtime("sbx-anything", "a1"))
    server = build_server_at(tmp_path, sbx)
    worker = make_identity()
    from mcp_gateway.errors import RoleRequiredError
    with pytest.raises(RoleRequiredError):
        _read_resource(server, "proxmox.vm.status", worker)
    # executive sees fleet
    from mcp_gateway.models import TokenKind as TK
    exec_ident = CallIdentity(token_kind=TK.AGENT, token_id="t2",
                              agent_name="stea-004",
                              acms_agent_id="a1", roles=["executive"],
                              scopes=["proxmox.read"])
    out = _read_resource(server, "proxmox.vm.status", exec_ident)
    assert len(out["runtimes"]) >= 1


# ------------------------------------------------------------------ tools

def test_sandbox_create_lifecycle(tmp_path):
    sbx = FakeSandboxClient()
    server = build_server_at(tmp_path, sbx)
    identity = make_identity()
    _arm_grant(server, identity.agent_name, "proxmox.sandbox.create")
    out = _call_tool(server, "proxmox_sandbox_create", identity,
                     ttl_hours=4, reason="test sandbox for work")
    assert out["sandbox_name"] == sandbox_name(
        "acms-hermes-worker-uid-005", "ACMS-WORK-000042-20261002_010203")
    assert out["ttl_hours"] == 4
    assert sbx.calls and sbx.calls[0][0] == "create"


def test_sandbox_start_stop_extend_destroy_own_only(tmp_path):
    sbx = FakeSandboxClient()
    server = build_server_at(tmp_path, sbx)
    identity = make_identity()
    # no sandbox yet -> NotFound (honest, not fabricated; approval gate first)
    from mcp_gateway.errors import NotFoundError
    _arm_grant(server, identity.agent_name, "proxmox.sandbox.stop")
    with pytest.raises(NotFoundError):
        _call_tool(server, "proxmox_sandbox_stop", identity, reason="stop my sandbox")

    # create then stop/extend/destroy all act on the OWN sandbox (one grant each)
    _arm_grant(server, identity.agent_name, "proxmox.sandbox.create")
    _call_tool(server, "proxmox_sandbox_create", identity, ttl_hours=4,
               reason="test sandbox for work")
    _arm_grant(server, identity.agent_name, "proxmox.sandbox.stop")
    out = _call_tool(server, "proxmox_sandbox_stop", identity, reason="stop my sandbox")
    assert out["desired"]["desired"] == "DESIRED_STOPPED"
    _arm_grant(server, identity.agent_name, "proxmox.sandbox.start")
    out = _call_tool(server, "proxmox_sandbox_start", identity, reason="start my sandbox")
    assert out["desired"]["desired"] == "DESIRED_RUNNING"
    _arm_grant(server, identity.agent_name, "proxmox.sandbox.extend_ttl")
    out = _call_tool(server, "proxmox_sandbox_extend_ttl", identity, ttl_hours=8,
                     reason="need more time")
    assert out["ttl_hours"] == 8
    _arm_grant(server, identity.agent_name, "proxmox.sandbox.destroy_own")
    out = _call_tool(server, "proxmox_sandbox_destroy_own", identity,
                     reason="work completed")
    assert out["desired"]["desired"] == "DESIRED_DESTROYED"
    # every lifecycle op only ever touched the OWN runtime id
    own_ids = {x["runtime_id"] for x in sbx.runtimes
               if x["name"].endswith("acms-hermes-worker-uid-005")}
    touched = {c[1] for c in sbx.calls if c[0] == "desired"} | {
        c[1] for c in sbx.calls if c[0] == "extend"}
    assert touched <= own_ids


def test_foreign_sandbox_not_reachable(tmp_path):
    """A second agent cannot start/stop/extend ANOTHER agent's sandbox — the
    ownership resolution finds only the CALLER's own sandbox by name."""
    sbx = FakeSandboxClient()
    server = build_server_at(tmp_path, sbx)
    # sandbox owned by uid-004 (foreign to our uid-005 identity)
    sbx.runtimes.append(sbx._runtime(
        "sbx-acms-work-000042-20261002-010203-acms-hermes-worker-uid-004", "a-004"))
    identity = make_identity()  # uid-005
    from mcp_gateway.errors import NotFoundError
    _arm_grant(server, identity.agent_name, "proxmox.sandbox.stop")
    with pytest.raises(NotFoundError):
        _call_tool(server, "proxmox_sandbox_stop", identity, reason="foreign attempt")
    assert not any(c[0] == "desired" for c in sbx.calls)


def test_deployment_request_creates_approval(tmp_path):
    sbx = FakeSandboxClient()
    server = build_server_at(tmp_path, sbx)
    identity = make_identity()
    _arm_grant(server, identity.agent_name, "proxmox.deployment.request")
    out = _call_tool(server, "proxmox_deployment_request", identity,
                     target="acms CT122", reason="release v0.12 wiring",
                     change="gateway env")
    assert out["approval_request_id"]
    assert out["status"] == "PENDING"


def test_no_generic_pve_or_delete_tools(tmp_path):
    sbx = FakeSandboxClient()
    server = build_server_at(tmp_path, sbx)
    names = {c.name for c in server.registry._capabilities.values()
             if c.domain == "proxmox"}
    # §26: the ONLY proxmox capabilities are the sandbox set + deployment request
    assert names == {
        "proxmox.sandbox.status", "proxmox.vm.status",
        "proxmox.sandbox.create", "proxmox.sandbox.start",
        "proxmox.sandbox.stop", "proxmox.sandbox.extend_ttl",
        "proxmox.sandbox.destroy_own", "proxmox.deployment.request"}


def test_unknown_tool_args_rejected(tmp_path):
    sbx = FakeSandboxClient()
    server = build_server_at(tmp_path, sbx)
    identity = make_identity()
    from mcp_gateway.errors import ValidationError_
    _arm_grant(server, identity.agent_name, "proxmox.sandbox.create")
    with pytest.raises(ValidationError_) as ei:
        _call_tool(server, "proxmox_sandbox_create", identity,
                   reason="test unknown args", node="miam-00147")
    assert "unknown argument" in str(ei.value)


def test_worker_cannot_target_protected_nodes(tmp_path):
    """§31: MIAM00133/MIAM00147 denied to ordinary workers — no tool even
    accepts a node argument; the create path goes through SM placement
    policy which excludes protected nodes."""
    sbx = FakeSandboxClient()
    server = build_server_at(tmp_path, sbx)
    identity = make_identity()
    _arm_grant(server, identity.agent_name, "proxmox.sandbox.create")
    _call_tool(server, "proxmox_sandbox_create", identity,
               ttl_hours=2, reason="protected test")
    # the create call must NOT have carried any node hint (SM decides)
    assert all("miam-00147" not in str(c) and "miam00133" not in str(c)
               for c in sbx.calls)
