"""W5 gateway tests: pdu.* + power.* capabilities (plan §27/§31).

Covers: power resources (facility current/history/cost, pdu/cooling slices),
pdu read tools (list/status/protection_state/outlet_status), executive
pdu.request_reboot = durable approval (no direct actuation), NO power mutation
tools exist for any role, power domain absent when SM unconfigured, and stale
channels are surfaced as stale (never zeroed).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from mcp_gateway.audit import AuditLog
from mcp_gateway.models import CallIdentity, TokenKind
from mcp_gateway.power_adapter import PowerClient
from mcp_gateway.server import GatewayConfig, GatewayServer
from mcp_gateway.tokens import TokenStore

from tests.test_mcp_gateway_w4 import _asyncio_run, FakeAcms, make_identity


class FakePowerClient(PowerClient):
    """Canned SM pdu/facility surfaces. No network."""

    def __init__(self):
        super().__init__(base_url="http://127.0.0.1:8300", token="fake-sm")

    def pdu_list(self):
        return {"api_version": "1.0.0", "status": "ok",
                "backend": "Direct SSH / PowerAlert menu"}

    def pdu_capabilities(self):
        return {"api_version": "1.0.0", "asset_count": 2, "backend_mode": "real",
                "endpoints": ["GET /api/v1/pdus"]}

    def pdu_assets(self):
        return {"count": 2, "data": [
            {"asset_id": "MIAM-00119", "pdu_id": "MIAM-00151", "outlet": 9,
             "protected": True, "state": "ON"},
            {"asset_id": "MIAM-00135", "pdu_id": "MIAM-00152", "outlet": 3,
             "protected": True, "state": "ON"},
        ]}

    def pdu_outlet_status(self, pdu_key: str, outlet: int):
        return {"pdu": pdu_key, "outlet": outlet, "state": "ON",
                "notes": "read-only surface"}

    def facility_power(self):
        return {"error": None, "generated_at": "2026-10-02T13:00:00Z",
                "channels": [
                    {"channel_num": "1", "label": "PDU MIAM-00151 (Server#1 rack circuit)",
                     "avg_watts_1h": 988.6, "kwh_24h": 4.86, "cost_usd_24h": 0.78,
                     "kwh_30d": 90.7, "cost_usd_30d": 14.6,
                     "last_sample_age_seconds": 31, "stale": False},
                    {"channel_num": "4", "label": "Cooling: 36k 3 Ton mini split",
                     "avg_watts_1h": 1200.0, "kwh_24h": 8.0, "cost_usd_24h": 1.28,
                     "kwh_30d": 150.0, "cost_usd_30d": 24.0,
                     "last_sample_age_seconds": 4000, "stale": True},
                ],
                "total": None, "incomplete_reason": "one or more channels stale"}


def build_server_at(tmp_path: Path, fake_power) -> GatewayServer:
    cfg = GatewayConfig(
        acms_base_url="http://127.0.0.1:9999", acms_token="fake",
        llm_base_url="http://127.0.0.1:8300", llm_token="fake-sm",
        approvals_path=str(tmp_path / "approvals.sqlite3"),
    )
    tokens = TokenStore(str(tmp_path / "tokens.sqlite3"))
    audit = AuditLog(str(tmp_path / "activity.sqlite3"))
    return GatewayServer(cfg, tokens=tokens, audit=audit,
                         acms_client=FakeAcms(), llm_client=None,
                         github_client=None, sandbox_client=fake_power,
                         power_client=fake_power)


def _read_resource(server, resource_name, identity):
    cap = server.registry._capabilities[resource_name]
    return _asyncio_run(cap.handler(identity, {}))


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


def _exec_identity():
    return CallIdentity(token_kind=TokenKind.AGENT, token_id="t-exec",
                        agent_name="stea-004", acms_agent_id="a-exec",
                        roles=["executive"], scopes=["power.read", "power.write"])


def test_power_domain_absent_when_sm_unconfigured(tmp_path):
    cfg = GatewayConfig(acms_base_url="http://127.0.0.1:9999", acms_token="fake",
                        approvals_path=str(tmp_path / "appr.sqlite3"))
    tokens = TokenStore(str(tmp_path / "t.sqlite3"))
    audit = AuditLog(str(tmp_path / "a.sqlite3"))
    server = GatewayServer(cfg, tokens=tokens, audit=audit,
                           acms_client=FakeAcms(), llm_client=None)
    names = {c.name for c in server.registry._capabilities.values()}
    assert not any(n.startswith("power.") for n in names)
    assert not any(n.startswith("pdu.") for n in names)


def test_power_resources_read(tmp_path):
    server = build_server_at(tmp_path, FakePowerClient())
    ident = make_identity()
    cur = _read_resource(server, "power.facility.current", ident)
    assert len(cur["pdu_channels"]) == 1 and len(cur["cooling_channels"]) == 1
    # stale channel surfaces as stale — never coerced to zero
    assert cur["cooling_channels"][0]["stale"] is True
    assert cur["total"] is None and "incomplete_reason" in cur

    hist = _read_resource(server, "power.facility.history", ident)
    assert {c["label"] for c in hist["channels"]} >= {"PDU MIAM-00151 (Server#1 rack circuit)",
                                                     "Cooling: 36k 3 Ton mini split"}
    cost = _read_resource(server, "power.cost.summary", ident)
    assert cost["cost_usd_24h"] > 0

    pdu = _read_resource(server, "power.pdu.current", ident)
    assert len(pdu["channels"]) == 1
    cooling = _read_resource(server, "power.cooling.current", ident)
    assert cooling["channels"][0]["stale"] is True


def test_pdu_read_tools(tmp_path):
    server = build_server_at(tmp_path, FakePowerClient())
    ident = make_identity()
    assert _call_tool(server, "pdu_list", ident)["status"] == "ok"
    assert _call_tool(server, "pdu_status", ident)["capabilities"]["backend_mode"] == "real"
    assets = _call_tool(server, "pdu_protection_state", ident)
    assert assets["count"] == 2
    assert all(a.get("protected") for a in assets["data"])
    out = _call_tool(server, "pdu_outlet_status", ident, pdu="MIAM-00151", outlet=9)
    assert out["state"] == "ON"


def test_no_power_mutation_tools_exist(tmp_path):
    server = build_server_at(tmp_path, FakePowerClient())
    names = {c.name for c in server.registry._capabilities.values()}
    # Destructive outlets must NOT exist in any form
    for banned in ("pdu.outlet.power_off", "pdu.outlet.power_on", "pdu.outlet.control",
                   "power.outlet.set", "pdu.power_off"):
        assert banned not in names
    # only the approval-path reboot request exists (executive, no direct actuation)
    assert "pdu.request_reboot" in names


def test_request_reboot_is_approval_path(tmp_path):
    server = build_server_at(tmp_path, FakePowerClient())
    out = _call_tool(server, "pdu_request_reboot", _exec_identity(),
                     target="MIAM-00119", reason="W5 acceptance reboot request")
    assert out["approval_request_id"].startswith("apr-")
    assert out["status"] == "PENDING"


def test_request_reboot_worker_gets_durable_approval_path(tmp_path):
    """Plan §16: SENSITIVE_WRITE for a worker = APPROVAL_REQUIRED (durable, one-time
    grant) — the reboot never executes directly; approval store owns the decision."""
    from mcp_gateway.errors import ApprovalRequiredError
    server = build_server_at(tmp_path, FakePowerClient())
    with pytest.raises(ApprovalRequiredError):
        _call_tool(server, "pdu_request_reboot", make_identity(),
                   target="MIAM-00119", reason="worker tries reboot")