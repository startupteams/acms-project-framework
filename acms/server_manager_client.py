"""Server Manager client for ACMS (JINT-001 paired work, REV4 §12/§13).

ACMS reserves the persistent agent identity (its own agents table) and uses
this client to request runtime provisioning from Server Manager. ACMS never
touches Proxmox/VMID details beyond optional diagnostics (§13).

Contract pinned: Server Manager API v1.0.0 (agent-runtimes + provisioning-jobs).
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from uuid import uuid4

API_CONTRACT_VERSION = "1.0.0"


class ServerManagerError(Exception):
    """Transport/protocol failure talking to Server Manager."""


class ServerManagerConflict(ServerManagerError):
    """409 from Server Manager (e.g. destroy without backing VM)."""


class ServerManagerForbidden(ServerManagerError):
    """403 — includes ownership-guard refusals (§8)."""


@dataclass
class RuntimeHandle:
    runtime_id: str
    acms_agent_id: str
    actual_state: str
    vmid: int | None
    node: str | None
    hermes_state_branch: str | None
    raw: dict

    @classmethod
    def from_api(cls, d: dict) -> "RuntimeHandle":
        return cls(
            runtime_id=d["runtime_id"], acms_agent_id=d["acms_agent_id"],
            actual_state=d["actual_state"], vmid=d.get("vmid"), node=d.get("node"),
            hermes_state_branch=d.get("hermes_state_branch"), raw=d,
        )


@dataclass
class ProvisionJob:
    job_id: str
    state: str
    error: str | None
    steps: list[dict]
    created: bool

    @classmethod
    def from_api(cls, d: dict) -> "ProvisionJob":
        return cls(job_id=d["job"]["job_id"], state=d["job"]["state"],
                   error=d["job"].get("error"), steps=d["job"].get("steps", []),
                   created=d.get("created", False))


class ServerManagerClient:
    """Minimal, dependency-free client (urllib, like acms/bridge.py pattern)."""

    def __init__(self, base_url: str, token: str, timeout: int = 30):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    # ---------------------------------------------------------------- transport
    def _call(self, method: str, path: str, body: dict | None = None,
              expected: tuple[int, ...] = (200, 201, 202)) -> dict:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(f"{self.base_url}{path}", data=data, method=method)
        req.add_header("Authorization", f"Bearer {self.token}")
        if body is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode() or "{}")
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:400]
            if e.code == 403:
                raise ServerManagerForbidden(f"{e.code} {detail}") from None
            if e.code in (409,):
                raise ServerManagerConflict(f"{e.code} {detail}") from None
            raise ServerManagerError(f"{e.code} {detail}") from None
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise ServerManagerError(str(e)) from None

    # ------------------------------------------------------------------- api v1
    def create_runtime(self, acms_agent_id: str, name: str, harness: str = "hermes",
                       runtime_class: str = "software_development_worker",
                       model_route: str | None = None,
                       bridge_profile: dict | None = None,
                       request_id: str | None = None) -> ProvisionJob:
        """POST /api/v1/agent-runtimes — idempotent on request_id (§10/§13)."""
        body = {
            "acms_agent_id": acms_agent_id,
            "request_id": request_id or f"acms-{uuid4()}",
            "name": name,
            "harness": harness,
            "runtime_class": runtime_class,
            "model_route": model_route,
            "bridge_profile": bridge_profile or {},
        }
        return ProvisionJob.from_api(self._call("POST", "/api/v1/agent-runtimes", body))

    def get_job(self, job_id: str) -> ProvisionJob:
        d = self._call("GET", f"/api/v1/provisioning-jobs/{job_id}")
        return ProvisionJob(job_id=d["job_id"], state=d["state"], error=d.get("error"),
                            steps=d.get("steps", []), created=True)

    def get_runtime(self, runtime_id: str) -> RuntimeHandle:
        return RuntimeHandle.from_api(self._call("GET", f"/api/v1/agent-runtimes/{runtime_id}"))

    def list_runtimes(self, acms_agent_id: str | None = None) -> list[RuntimeHandle]:
        path = "/api/v1/agent-runtimes" + (f"?acms_agent_id={acms_agent_id}" if acms_agent_id else "")
        d = self._call("GET", path)
        return [RuntimeHandle.from_api(r) for r in d.get("runtimes", [])]

    def set_desired_state(self, runtime_id: str, desired_state: str, reason: str) -> RuntimeHandle:
        return RuntimeHandle.from_api(self._call(
            "POST", f"/api/v1/agent-runtimes/{runtime_id}/desired-state",
            {"desired_state": desired_state, "reason": reason}))

    def destroy_runtime(self, runtime_id: str) -> dict:
        """Destroy the RUNTIME (VM), never the ACMS persistent agent (§10)."""
        return self._call("DELETE", f"/api/v1/agent-runtimes/{runtime_id}", expected=(200,))

    def capabilities(self) -> dict:
        return self._call("GET", "/api/v1/capabilities")
