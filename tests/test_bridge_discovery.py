"""Phase G: ARM-authoritative bridge discovery tests."""

from __future__ import annotations

import pytest

from acms import bridge_discovery as bd
from acms.bridge_discovery import DiscoveryError, discover_bridge


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("ACMS_SERVER_MANAGER_BASE_URL", "http://sm.test:8300")
    monkeypatch.setenv("ACMS_SERVER_MANAGER_TOKEN", "tok")
    monkeypatch.setenv("ACMS_BRIDGE_TARGETS_JSON", "")
    from acms.settings import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


RUNTIME = {
    "runtime_id": "rt-1", "acms_agent_id": "agent-x", "actual_state": "RUNNING",
    "vmid": 124, "node": "miam00111", "harness": "hermes",
    "state_sync_health": "VERIFIED",
    "bridge_base_url": "http://10.0.20.203:8402",
    "bridge_api_key": "arm-key-1",
}


def test_arm_authoritative_endpoint_wins(monkeypatch):
    monkeypatch.setattr(bd, "_sm_request", lambda p, timeout=8.0: {"runtimes": [dict(RUNTIME)]})
    d = discover_bridge("agent-x")
    assert d.source == "server_manager"
    assert d.base_url == "http://10.0.20.203:8402"
    assert d.api_key == "arm-key-1"
    assert d.state_sync_health == "VERIFIED"


def test_arm_prefers_verified_over_unknown(monkeypatch):
    rt_unknown = dict(RUNTIME, runtime_id="rt-bad", state_sync_health="UNKNOWN",
                      bridge_base_url="http://wrong:1", bridge_api_key="wrong")
    monkeypatch.setattr(bd, "_sm_request", lambda p, timeout=8.0: {"runtimes": [rt_unknown, dict(RUNTIME)]})
    d = discover_bridge("agent-x")
    assert d.runtime_id == "rt-1"


def test_manual_fallback_when_arm_absent(monkeypatch):
    monkeypatch.setattr(bd, "_sm_request", lambda p, timeout=8.0: None)
    monkeypatch.setenv("ACMS_BRIDGE_TARGETS_JSON",
                       '[{"agent_id":"agent-x","base_url":"http://10.0.20.203:8402","api_key":"manual-key"}]')
    from acms.settings import get_settings

    get_settings.cache_clear()
    d = discover_bridge("agent-x")
    assert d.source == "manual_config"
    assert d.api_key == "manual-key"


def test_arm_rejects_non_running(monkeypatch):
    rt = dict(RUNTIME, actual_state="STOPPED")
    monkeypatch.setattr(bd, "_sm_request", lambda p, timeout=8.0: {"runtimes": [rt]})
    with pytest.raises(DiscoveryError):
        discover_bridge("agent-x")


def test_fail_closed(monkeypatch):
    monkeypatch.setattr(bd, "_sm_request", lambda p, timeout=8.0: None)
    with pytest.raises(DiscoveryError):
        discover_bridge("agent-unknown")


def test_bridge_factory_uses_discovery(monkeypatch):
    monkeypatch.setattr(bd, "_sm_request", lambda p, timeout=8.0: {"runtimes": [dict(RUNTIME)]})
    from acms.bridge import get_bridge_for_agent

    b = get_bridge_for_agent("agent-x")
    assert b._t.base_url == "http://10.0.20.203:8402"
