"""MCP gateway internal client (plan §12/§13/§23/§24).

ACMS calls the gateway's /internal/* machine API with the shared internal
secret (mirrors the gateway's MCP_GATEWAY_INTERNAL_TOKEN). Boundaries:

- The gateway is an ADDITIVE sidecar (ADR-0019): every call here is
  best-effort. Dispatch NEVER fails because minting failed; the UI NEVER
  breaks because the card fetch failed. All errors degrade to None/honest
  "unavailable" states.
- Raw assignment tokens appear exactly ONCE (in the mint response) and are
  placed into the dispatch envelope — the worker's only copy. They are never
  logged, never stored in ACMS tables, and never echoed into chat.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from .settings import get_settings


class McpGatewayUnavailable(RuntimeError):
    pass


class McpGatewayClient:
    """Thin client for the gateway /internal/* API (fail-soft by design)."""

    def __init__(self, base_url: str | None = None, token: str | None = None,
                 timeout_seconds: int = 10):
        s = get_settings()
        self.base_url = (base_url or s.mcp_gateway_base_url).rstrip("/")
        self._token = token or s.mcp_gateway_internal_token
        self.timeout = timeout_seconds

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self._token)

    def _request(self, method: str, path: str, payload: dict | None = None) -> Any:
        if not self.configured:
            raise McpGatewayUnavailable("MCP gateway integration not configured")
        url = f"{self.base_url}{path}"
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(
            url, data=data, method=method,
            headers={"Authorization": f"Bearer {self._token}",
                     "Accept": "application/json"} |
                    ({"Content-Type": "application/json"} if data else {}))
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return resp.status, json.loads(resp.read().decode() or "null")
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode() or "null")
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise McpGatewayUnavailable(
                f"MCP gateway unreachable: {exc.__class__.__name__}") from exc

    # ---------------- plan §13: dispatch-time auto-mint ----------------

    def mint_assignment(self, *, agent_name: str, work_uid: str, ttl_hours: int,
                        jira_issue_key: str | None = None,
                        project_slug: str | None = None,
                        project_id: str | None = None,
                        repositories: list[str] | None = None) -> dict[str, Any] | None:
        """Mint an assignment token at dispatch time. Returns the gateway's
        response dict (with the raw token) or None on ANY failure — callers
        treat None as 'no MCP token in this dispatch' (graceful degradation)."""
        try:
            status, body = self._request("POST", "/internal/mint-assignment", payload={
                "agent_name": agent_name,
                "work_uid": work_uid,
                "ttl_hours": ttl_hours,
                "jira_issue_key": jira_issue_key,
                "project_slug": project_slug,
                "project_id": project_id,
                "repositories": repositories or [],
            })
        except (McpGatewayUnavailable, ValueError):
            return None
        if status == 201 and isinstance(body, dict) and body.get("token"):
            return body
        return None

    # ---------------- plan §23/§24: capability card + activity ----------------

    def capability_card(self, agent_name: str) -> dict[str, Any] | None:
        try:
            status, body = self._request(
                "GET", f"/internal/capability-card/{agent_name}")
        except (McpGatewayUnavailable, ValueError):
            return None
        return body if status == 200 and isinstance(body, dict) else None

    def activity(self, *, agent_name: str | None = None, work_uid: str | None = None,
                 limit: int = 25) -> list[dict[str, Any]]:
        params: list[str] = [f"limit={int(limit)}"]
        if agent_name:
            params.append(f"agent_name={urllib.parse.quote(agent_name)}")
        if work_uid:
            params.append(f"work_uid={urllib.parse.quote(work_uid)}")
        try:
            status, body = self._request("GET", "/internal/activity?" + "&".join(params))
        except (McpGatewayUnavailable, ValueError):
            return []
        if status == 200 and isinstance(body, dict):
            return body.get("activity", []) or []
        return []
