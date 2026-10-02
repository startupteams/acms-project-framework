"""Proxmox sandbox domain adapter (plan §26) — Server Manager is the authority.

The gateway NEVER talks to the PVE API and holds NO PVE credentials. Every
capability here is a thin REST call against Server Manager (:8300 on VM114,
svc-acms bearer identity, scoped), which owns the audited allowlisted PVE shim
(`control/pve_ops.py`) and the ARM provisioning/reconciliation state machine.

Surface (plan §26 — worker sandbox capability + production deployment):

Resources (READ):
  proxmox.sandbox.status — the CALLING agent's own sandbox(es) (SM agent-runtimes)
  proxmox.vm.status      — fleet view (executive/infrastructure_admin only)

Worker tools:
  proxmox.sandbox.create     — SENSITIVE_WRITE: approval-gated; creates an
                               ARM provisioning request for a sandbox-class
                               runtime (TTL'd, tagged, resource-limited)
  proxmox.sandbox.start      — SENSITIVE_WRITE: own sandbox only
  proxmox.sandbox.stop       — SENSITIVE_WRITE: own sandbox only (graceful)
  proxmox.sandbox.extend_ttl — SENSITIVE_WRITE: own sandbox only, bounded
  proxmox.sandbox.destroy_own — SENSITIVE_WRITE: own sandbox only →
                               DESIRED_DESTROYED (API-only flip; ARM reconciles)
  proxmox.deployment.request — SENSITIVE_WRITE: durable approval request; SM
                               validates target/backup/rollback/policy before
                               any narrow deployment path is granted

Hard limits (plan §16/§26/§43):
- NO generic PVE root-shell tool exists. NO delete/destroy of arbitrary VMs.
- Workers NEVER touch MIAM-00133 / MIAM-00147 / LLDAP / PBS / PDU authority.
- Ordinary workers never receive cluster-root access; inside their OWN sandbox
  the OS-level freedoms (install/docker/compile) are the tenant's business.
- Sandbox identity = (agent_name, work_uid); ownership enforced HERE (gateway)
  by runtime name/ownership_meta match, NOT by trusting the caller.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

from .errors import DomainUnavailableError, NotFoundError, ValidationError_


class SandboxError(Exception):
    """Server Manager returned an error for a sandbox operation."""


class SandboxClient:
    """Thin REST client for the Server Manager sandbox/ARM surface (v1)."""

    def __init__(self, base_url: str, token: str, *, timeout_seconds: int = 10):
        if not base_url:
            raise ValueError("Server Manager base URL not configured")
        if not token:
            raise ValueError("Server Manager token not configured")
        self.base_url = base_url.rstrip("/")
        self._token = token
        self.timeout = timeout_seconds

    def request(self, method: str, path: str, body: dict | None = None) -> tuple[int, Any]:
        url = f"{self.base_url}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            url, data=data,
            headers={"Authorization": f"Bearer {self._token}",
                     "Content-Type": "application/json",
                     "User-Agent": "miam-mcp-gateway/0.3"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode()
                return resp.status, (json.loads(raw) if raw else None)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:300]
            try:
                payload = json.loads(detail)
                msg = payload.get("detail") or detail
            except Exception:
                msg = detail
            if exc.code == 404:
                raise NotFoundError(msg) from exc
            raise DomainUnavailableError(
                f"sandbox operation failed (HTTP {exc.code}): {msg}") from exc
        except urllib.error.URLError as exc:
            raise DomainUnavailableError(
                f"Server Manager unreachable: {exc.reason}") from exc

    # ------------------------------------------------------------- reads
    def list_runtimes(self, acms_agent_id: str | None = None) -> list[dict]:
        path = "/api/v1/agent-runtimes"
        if acms_agent_id:
            from urllib.parse import urlencode
            path += "?" + urlencode({"acms_agent_id": acms_agent_id})
        status, body = self.request("GET", path)
        if status != 200 or body is None:
            raise DomainUnavailableError("agent-runtimes list failed")
        # SM v1 wraps: {"runtimes": [...]} (live-verified 2026-10-02); accept a
        # bare list too for robustness.
        if isinstance(body, dict):
            rows = body.get("runtimes", [])
        elif isinstance(body, list):
            rows = body
        else:
            raise DomainUnavailableError("agent-runtimes list: unexpected shape")
        if not isinstance(rows, list):
            raise DomainUnavailableError("agent-runtimes list: unexpected shape")
        return rows

    def get_runtime(self, runtime_id: str) -> dict:
        status, body = self.request("GET", f"/api/v1/agent-runtimes/{runtime_id}")
        if status != 200:
            raise DomainUnavailableError(f"agent-runtime get failed (HTTP {status})")
        return body

    # ------------------------------------------------------------- lifecycle
    def create_sandbox(self, *, acms_agent_id: str, request_id: str, name: str,
                       ttl_hours: int | None) -> dict:
        body: dict[str, Any] = {
            "acms_agent_id": acms_agent_id,
            "request_id": request_id,
            "name": name,
            "runtime_class": "sandbox",
            "desired_state": "DESIRED_RUNNING",
        }
        if ttl_hours is not None:
            body["sandbox_ttl_hours"] = int(ttl_hours)
        status, out = self.request("POST", "/api/v1/agent-runtimes", body)
        if status not in (200, 202):
            raise DomainUnavailableError(f"sandbox create failed (HTTP {status})")
        return out

    def set_desired_state(self, runtime_id: str, desired_state: str, reason: str) -> dict:
        status, out = self.request(
            "POST", f"/api/v1/agent-runtimes/{runtime_id}/desired-state",
            {"desired_state": desired_state, "reason": reason})
        if status != 200:
            raise DomainUnavailableError(f"desired-state failed (HTTP {status})")
        return out

    def extend_ttl(self, runtime_id: str, ttl_hours: int, reason: str) -> dict:
        status, out = self.request(
            "POST", f"/api/v1/agent-runtimes/{runtime_id}/extend-ttl",
            {"ttl_hours": int(ttl_hours), "reason": reason})
        if status != 200:
            raise DomainUnavailableError(f"extend-ttl failed (HTTP {status})")
        return out

    def get_job(self, job_id: str) -> dict:
        status, out = self.request("GET", f"/api/v1/provisioning-jobs/{job_id}")
        if status != 200:
            raise DomainUnavailableError(f"job get failed (HTTP {status})")
        return out


def sandbox_name(agent_name: str, work_uid: str) -> str:
    """Deterministic sandbox name for (agent, work) — §26 tagged/owned.

    PVE VM-name safe: lowercase, alnum + dashes ONLY (no underscores — PVE
    rejects them as invalid DNS names, live-found 2026-10-02), bounded 63.
    """
    raw = f"sbx-{work_uid}-{agent_name}".lower()
    cleaned = "".join(c if (c.isalnum() or c == "-") else "-" for c in raw)
    cleaned = cleaned.lstrip("-") or "sbx-x"  # must start alnum
    return cleaned[:63].rstrip("-") or "sbx-x"
