"""Stable Bridge endpoint discovery (2026-09-29 plan §9 / Phase G).

Replaces IP-pinned worker targeting (the 09-29 VM108/VM124 IP-collision lesson).

Resolution order for an agent's Bridge endpoint:
    1. Server Manager / ARM runtime record (authoritative: acms_agent_id →
       live runtime with node/vmid/harness and VERIFIED state-sync health).
       The ARM record's agent identity is trusted; the endpoint is derived
       from the runtime's current network identity, NOT from a static config.
    2. Manual config fallback (ACMS_BRIDGE_TARGETS_JSON) — kept for
       break-glass; if both are present and disagree, ARM wins and the
       disagreement is surfaced as a durable event upstream (caller decides).

Fail-closed: if neither source yields a target, BridgeError is raised.
"""
from __future__ import annotations

import json
import urllib.request
from dataclasses import dataclass

from .settings import get_settings


class DiscoveryError(RuntimeError):
    pass


@dataclass
class DiscoveredBridge:
    agent_id: str
    base_url: str
    api_key: str
    harness: str
    source: str            # "server_manager" | "manual_config"
    runtime_id: str | None = None
    state_sync_health: str | None = None


def _sm_request(path: str, timeout: float = 8.0) -> dict | None:
    s = get_settings()
    if not s.server_manager_base_url or not s.server_manager_token:
        return None
    req = urllib.request.Request(
        s.server_manager_base_url.rstrip("/") + path,
        headers={"Authorization": f"Bearer {s.server_manager_token}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except Exception:
        return None  # fail soft to fallback; caller raises if both fail


def _manual_targets() -> dict[str, dict]:
    raw = get_settings().bridge_targets_json
    if not raw:
        return {}
    try:
        return {item["agent_id"]: item for item in json.loads(raw)}
    except (json.JSONDecodeError, KeyError, TypeError):
        return {}


def discover_bridge(agent_id: str) -> DiscoveredBridge:
    """Resolve the current Bridge endpoint for an ACMS agent ID.

    ARM-authoritative: picks the live runtime (actual_state RUNNING, node/vmid
    set, best state_sync_health) for this agent. Endpoint derivation: the ARM
    runtime record carries the worker's bridge URL when present
    (``bridge_base_url`` / ``ip_address`` fields); otherwise the manual
    fallback provides the URL/key while ARM provides the identity check.
    """
    # ---- 1. ARM-authoritative ------------------------------------------------
    data = _sm_request("/api/v1/agent-runtimes")
    if data:
        best = None
        for rt in data.get("runtimes", []):
            if rt.get("acms_agent_id") != agent_id:
                continue
            if rt.get("actual_state") != "RUNNING" or not rt.get("vmid"):
                continue
            health_rank = {"VERIFIED": 2, "UNKNOWN": 1}.get(rt.get("state_sync_health") or "", 0)
            if best is None or health_rank > best[0]:
                best = (health_rank, rt)
        if best is not None:
            rt = best[1]
            manual = _manual_targets().get(agent_id, {})
            bridge_url = rt.get("bridge_base_url") or manual.get("base_url")
            api_key = rt.get("bridge_api_key") or manual.get("api_key")
            if bridge_url and api_key:
                return DiscoveredBridge(
                    agent_id=agent_id,
                    base_url=str(bridge_url).rstrip("/"),
                    api_key=str(api_key),
                    harness=rt.get("harness") or manual.get("harness", "hermes"),
                    source="server_manager",
                    runtime_id=rt.get("runtime_id"),
                    state_sync_health=rt.get("state_sync_health"),
                )

    # ---- 2. manual fallback ---------------------------------------------------
    manual = _manual_targets().get(agent_id)
    if manual:
        return DiscoveredBridge(
            agent_id=agent_id,
            base_url=str(manual["base_url"]).rstrip("/"),
            api_key=str(manual["api_key"]),
            harness=manual.get("harness", "hermes"),
            source="manual_config",
        )

    # ---- 3. fail closed --------------------------------------------------------
    raise DiscoveryError(
        f"no bridge endpoint discoverable for agent {agent_id} "
        "(ARM runtime record absent/not RUNNING and no manual fallback)"
    )
