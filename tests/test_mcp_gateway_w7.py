"""W7 gateway tests: registry.* + monitoring.* capabilities (plan §29).

Covers: registry services list/get/dependencies (read-only), monitoring
status/monitor/incidents (read-only via the verified Kuma socket.io contract,
faked), domains absent when unconfigured, NO mutation tools exist for any
role (plan §29: registry/Kuma = context, not authorization).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from mcp_gateway.audit import AuditLog
from mcp_gateway.errors import NotFoundError
from mcp_gateway.models import CallIdentity, TokenKind
from mcp_gateway.registry_adapter import MonitoringClient, RegistryClient
from mcp_gateway.server import GatewayConfig, GatewayServer
from mcp_gateway.tokens import TokenStore

from tests.test_mcp_gateway_w4 import _asyncio_run, FakeAcms, make_identity


class FakeRegistry(RegistryClient):
    def __init__(self):
        super().__init__("https://registry.example", "fake-token")
        self._services = [
            {"id": "acms", "name": "ACMS Work Control Plane", "category": "Platform",
             "host": "MIAM-00135", "guest": "CT122", "url": "https://10.0.20.122/ui",
             "health_url": "https://10.0.20.122/health", "state": "managed",
             "managed": True, "tags": ["acms", "platform"]},
            {"id": "mcp-gateway", "name": "MIAM MCP Gateway", "category": "Infrastructure",
             "host": "MIAM-00100", "guest": "VM114", "url": "https://mcp.miam.home.arpa",
             "health_url": "https://mcp.miam.home.arpa/health", "state": "managed",
             "managed": True, "tags": ["mcp", "gateway"]},
            {"id": "registry", "name": "MIAM Service Registry", "category": "Infrastructure",
             "host": "MIAM-00135", "guest": "VM119", "url": "https://registry.miam.home.arpa",
             "health_url": "https://registry.miam.home.arpa/health", "state": "managed",
             "managed": True, "tags": ["registry"]},
        ]

    def list_services(self):
        return list(self._services)


class FakeMonitoring(MonitoringClient):
    def __init__(self):
        super().__init__("http://127.0.0.1:3001", "fake", "fake")
        self._monitors = [
            {"id": 1, "name": "ACMS Work Control Plane", "type": "http",
             "url": "https://10.0.20.122/health", "interval": 60, "active": 1,
             "upsideDown": False},
            {"id": 2, "name": "Legacy Guest (disabled)", "type": "port",
             "url": "tcp://10.0.20.250:8000", "interval": 60, "active": 0,
             "upsideDown": False},
        ]

    def list_monitors(self):
        return [dict(m) for m in self._monitors]

    def incidents(self):
        return [{"monitor_id": 2, "name": "Legacy Guest (disabled)",
                 "active": 0, "beat_status": None}]


def build_server_at(tmp_path: Path, registry=None, monitoring=None) -> GatewayServer:
    cfg = GatewayConfig(
        acms_base_url="http://127.0.0.1:9999", acms_token="fake",
        llm_base_url="http://127.0.0.1:8300", llm_token="fake-sm",
        approvals_path=str(tmp_path / "approvals.sqlite3"),
    )
    tokens = TokenStore(str(tmp_path / "tokens.sqlite3"))
    audit = AuditLog(str(tmp_path / "activity.sqlite3"))
    return GatewayServer(cfg, tokens=tokens, audit=audit,
                         acms_client=FakeAcms(), llm_client=None,
                         registry_client=registry, monitoring_client=monitoring)


def _ident():
    return CallIdentity(token_kind=TokenKind.AGENT, token_id="t1",
                        agent_name="uid-005", acms_agent_id="a-5",
                        roles=["worker"],
                        scopes=["acms.read", "registry.read", "monitoring.read"])


def _read(server, name, identity, params=None):
    cap = server.registry._capabilities[name]
    return _asyncio_run(cap.handler(identity, params or {}))


def test_w7_domains_absent_when_unconfigured(tmp_path):
    server = build_server_at(tmp_path, None, None)
    names = {c.name for c in server.registry._capabilities.values()}
    assert not any(n.startswith("registry.") for n in names)
    assert not any(n.startswith("monitoring.") for n in names)


def test_registry_services_list(tmp_path):
    server = build_server_at(tmp_path, FakeRegistry(), None)
    out = _read(server, "registry.services.list", _ident())
    assert out["count"] == 3
    ids = [s["id"] for s in out["services"]]
    assert "acms" in ids and "mcp-gateway" in ids


def test_registry_service_get(tmp_path):
    server = build_server_at(tmp_path, FakeRegistry(), None)
    out = _read(server, "registry.service.get", _ident(), {"service_id": "acms"})
    assert out["service"]["guest"] == "CT122"
    assert out["service"]["url"] == "https://10.0.20.122/ui"
    # same-host dependency hint: acms + registry both live on MIAM-00135
    hosts = {d["id"] for d in out["dependencies"]["same_host"]}
    assert "registry" in hosts and "acms" not in hosts


def test_registry_service_not_found(tmp_path):
    server = build_server_at(tmp_path, FakeRegistry(), None)
    with pytest.raises(NotFoundError):
        _read(server, "registry.service.get", _ident(), {"service_id": "nope"})


def test_monitoring_status_readonly_view(tmp_path):
    server = build_server_at(tmp_path, None, FakeMonitoring())
    out = _read(server, "monitoring.status", _ident())
    assert out["monitor_count"] == 2
    assert out["disabled"] == 1
    assert out["active"] == 1


def test_monitoring_monitor_get(tmp_path):
    server = build_server_at(tmp_path, None, FakeMonitoring())
    out = _read(server, "monitoring.monitor.get", _ident(), {"monitor_id": "1"})
    assert out["monitor"]["name"] == "ACMS Work Control Plane"
    with pytest.raises(NotFoundError):
        _read(server, "monitoring.monitor.get", _ident(), {"monitor_id": "999"})


def test_monitoring_incidents(tmp_path):
    server = build_server_at(tmp_path, None, FakeMonitoring())
    out = _read(server, "monitoring.incidents", _ident())
    assert len(out["incidents"]) == 1
    assert out["incidents"][0]["monitor_id"] == 2


def test_no_registry_or_monitoring_mutation_tools(tmp_path):
    """Plan §29: registry/Kuma = context and observability, NOT authorization.
    No write tool exists for any role."""
    server = build_server_at(tmp_path, FakeRegistry(), FakeMonitoring())
    names = {c.name for c in server.registry._capabilities.values()}
    for banned in ("registry.service.create", "registry.service.update",
                   "registry.service.delete", "registry.service.disable",
                   "monitoring.monitor.add", "monitoring.monitor.delete",
                   "monitoring.monitor.edit"):
        assert banned not in names
    # all registered W7 capabilities are READ-class
    for cap in server.registry._capabilities.values():
        if cap.name.startswith(("registry.", "monitoring.")):
            assert cap.risk.value.lower() == "read"