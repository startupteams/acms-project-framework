"""LLM/runtime domain adapter (plan §21) — Server Manager is the authority.

The gateway reads model/runtime facts from Server Manager (:8300 on VM114,
bearer service identity, scoped). LLM-Manager web surfaces are session-auth
(LDAP humans) and are deliberately NOT consumed by the gateway.

Surface (plan §21):

Resources (READ):
  llm.models.list        — routable model routes (model_registry via SM)
  llm.model.get          — one route's backends
  llm.model_health       — health/routability per logical model
  llm.loaded_models      — active hosts (active_hosts ∪ legacy constants)
  llm.host_capacity      — hosts inventory (GPU count/model per physical host)
  llm.route_status       — alias/resolution status (is_alias/alias_target)
  llm.usage              — LiteLLM_SpendLogs aggregate (bounded hours)
  runtime.self           — the CALLING agent's own runtime (ACMS fleet view)
  runtime.fleet          — role-gated broad fleet view (SM agent-runtimes)

Worker tools:
  llm.request_model      — set own work item's model policy (validated model)
  llm.request_fallback   — request cloud fallback for own work item (bounded
                           by ACMS ADR-0015 budget caps; SAFE_WRITE, audited)

Hard limits (plan §16/§21/§43):
- Workers NEVER create workers, restart other agents, or stop other runtimes.
- runtime.restart_self = SENSITIVE_WRITE via ACMS fleet API (self only).
- No PDU mutation, no VM lifecycle beyond the self-restart path.
"""
from __future__ import annotations

import json
import ssl
import urllib.error
import urllib.request
from typing import Any

from .errors import DomainUnavailableError, NotFoundError, ValidationError_


class ServerManagerClient:
    """Thin REST client for Server Manager v1 (bearer service identity)."""

    def __init__(self, base_url: str, token: str, *, timeout_seconds: int = 10):
        if not base_url:
            raise ValueError("Server Manager base URL not configured")
        if not token:
            raise ValueError("Server Manager token not configured")
        self.base_url = base_url.rstrip("/")
        self._token = token
        self.timeout = timeout_seconds

    def request(self, method: str, path: str,
                query: dict[str, str] | None = None) -> tuple[int, Any]:
        url = f"{self.base_url}{path}"
        if query:
            from urllib.parse import urlencode
            url += "?" + urlencode(query)
        req = urllib.request.Request(
            url, headers={"Authorization": f"Bearer {self._token}",
                          "Accept": "application/json"}, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = resp.read().decode("utf-8", errors="replace")
                status = resp.status
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            status = exc.code
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise DomainUnavailableError(
                f"Server Manager unreachable: {exc.__class__.__name__}") from exc
        try:
            parsed = json.loads(body) if body else None
        except ValueError:
            parsed = body
        return status, parsed

    # ---------------- plan §21 reads ----------------

    def model_routes(self) -> dict[str, list[dict]]:
        status, body = self.request("GET", "/api/v1/model-routes")
        if status != 200 or not isinstance(body, dict):
            raise DomainUnavailableError(f"model routes failed (HTTP {status})")
        return body.get("routes", {})

    def hosts(self) -> list[dict]:
        status, body = self.request("GET", "/api/v1/hosts")
        if status != 200 or not isinstance(body, dict):
            raise DomainUnavailableError(f"hosts failed (HTTP {status})")
        return body.get("hosts", [])

    def active_hosts(self) -> list[dict]:
        status, body = self.request("GET", "/api/v1/hosts")
        if status != 200 or not isinstance(body, dict):
            raise DomainUnavailableError(f"hosts failed (HTTP {status})")
        return body.get("active_hosts", [])

    def usage(self, hours: int) -> dict:
        status, body = self.request("GET", "/api/v1/usage",
                                    query={"hours": str(hours)})
        if status != 200 or not isinstance(body, dict):
            raise DomainUnavailableError(f"usage failed (HTTP {status})")
        return body

    def agent_runtimes(self, acms_agent_id: str | None = None) -> list[dict]:
        query = {"acms_agent_id": acms_agent_id} if acms_agent_id else None
        status, body = self.request("GET", "/api/v1/agent-runtimes", query=query)
        if status != 200 or not isinstance(body, dict):
            raise DomainUnavailableError(f"agent-runtimes failed (HTTP {status})")
        return body.get("runtimes", [])


# ---------------- response pruners (honest minimal projections) ----------------

def _prune_route(name: str, backends: list[dict]) -> dict:
    return {
        "model": name,
        "backends": [{
            "engine": b.get("engine"),
            "context_limit": b.get("context_limit"),
            "health": b.get("health"),
            "routable": b.get("routable"),
            "is_alias": b.get("is_alias"),
            "alias_target": b.get("alias_target"),
        } for b in backends],
    }


def _prune_host(h: dict) -> dict:
    return {k: h.get(k) for k in (
        "host_id", "name", "guest_ip", "gpu_count", "gpu_model", "node", "vmid",
        "desired_power_state", "desired_service_state", "active_model_host") if k in h}


def _prune_runtime(rt: dict) -> dict:
    return {k: rt.get(k) for k in (
        "runtime_id", "acms_agent_id", "name", "node", "vmid", "harness",
        "runtime_class", "model_route", "desired_state", "actual_state",
        "recovery_count", "state_sync_health", "last_error",
        "last_reconcile_at", "hermes_state_branch", "created_at") if k in rt}
