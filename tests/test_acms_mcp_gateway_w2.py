"""W2 ACMS-side tests: MCP gateway client (fail-soft) + dispatch auto-mint
envelope behavior (plan §13: mint failure NEVER blocks dispatch)."""
from __future__ import annotations

import json
import urllib.error
from unittest.mock import patch

import pytest

from acms.mcp_gateway_client import McpGatewayClient


# ---------------------------------------------------------------- client (pure)

def test_client_unconfigured_degrades_softly():
    client = McpGatewayClient(base_url="", token="")
    assert client.configured is False
    assert client.capability_card("x") is None
    assert client.activity(work_uid="ACMS-WORK-1") == []
    assert client.mint_assignment(agent_name="a", work_uid="w", ttl_hours=8) is None


def test_mint_success_returns_body_with_token():
    class _Resp:
        status = 201

        def read(self):
            return json.dumps({"token": "mcpt_abc123", "token_id": "t1",
                               "agent_name": "a", "work_uid": "w",
                               "expires_at": "2026-10-02T16:00:00+00:00"}).encode()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    client = McpGatewayClient(base_url="http://gw.test", token="s3cret")
    assert client.configured is True

    captured = {}

    def fake_urlopen(req, timeout):
        captured["url"] = req.full_url
        captured["auth"] = req.headers.get("Authorization")
        captured["payload"] = json.loads(req.data.decode())
        return _Resp()

    import urllib.request

    with patch.object(urllib.request, "urlopen", fake_urlopen):
        out = client.mint_assignment(agent_name="a", work_uid="w", ttl_hours=8,
                                     jira_issue_key="STNA-89")
    assert out is not None and out["token"] == "mcpt_abc123"
    assert captured["auth"] == "Bearer s3cret"
    assert captured["payload"]["ttl_hours"] == 8
    assert "/internal/mint-assignment" in captured["url"]


def _http_error(status: int, body: dict):
    return urllib.error.HTTPError("http://gw.test", status,
                                  json.dumps(body).encode(), {}, None)


def test_mint_http_404_returns_none():
    import urllib.request

    client = McpGatewayClient(base_url="http://gw.test", token="s3cret")

    def raiser(req, timeout):
        raise _http_error(404, {"error": "OUT_OF_SCOPE"})

    with patch.object(urllib.request, "urlopen", raiser):
        out = client.mint_assignment(agent_name="ghost", work_uid="w", ttl_hours=8)
    assert out is None  # fail-soft: dispatch proceeds without a token


def test_mint_network_error_returns_none_everywhere():
    import urllib.request

    client = McpGatewayClient(base_url="http://gw.test", token="s3cret")

    def boom(req, timeout):
        raise urllib.error.URLError("connection refused")

    with patch.object(urllib.request, "urlopen", boom):
        assert client.mint_assignment(agent_name="a", work_uid="w", ttl_hours=8) is None
        assert client.capability_card("a") is None
        assert client.activity(work_uid="w") == []


def test_capability_card_and_activity_parse():
    class _Resp:
        def __init__(self, status, body):
            self.status = status
            self._b = json.dumps(body).encode()

        def read(self):
            return self._b

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    import urllib.request

    client = McpGatewayClient(base_url="http://gw.test", token="s3cret")
    card_body = {"agent_name": "a", "mcp_connected": True,
                 "agent_token": {"active": True, "roles": ["worker"]},
                 "resources_available": [], "tools_available": [],
                 "tools_requiring_approval": []}
    with patch.object(urllib.request, "urlopen",
                      lambda req, timeout: _Resp(200, card_body)):
        assert client.capability_card("a")["mcp_connected"] is True
    with patch.object(urllib.request, "urlopen",
                      lambda req, timeout: _Resp(200, {"activity": [{"name": "x", "status": "ok"}]})):
        assert client.activity(work_uid="w")[0]["name"] == "x"


# ---------------------------------------------------------------- settings defaults

def test_w2_settings_defaults_off():
    import inspect

    from acms.settings import Settings

    s = Settings()
    assert s.mcp_gateway_base_url == ""
    assert s.mcp_gateway_internal_token == ""
    assert s.mcp_dispatch_mint_enabled is True
    assert s.mcp_assignment_ttl_hours == 8
    assert inspect.isclass(Settings)
