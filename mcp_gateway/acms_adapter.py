"""ACMS adapter (plan §18): the FIRST domain adapter.

The gateway calls ACMS REST (the domain authority) over HTTPS with the
gateway's own bearer credential (ACMS_ADMIN_TOKEN scope; later a dedicated
service token). No DB session is opened here in production paths — REST is
the boundary, keeping the gateway independently fail-able (plan §33: gateway
failure never breaks ACMS, and ACMS failure never breaks REST/A2A/SSE).

Surface (plan §18):

Resources (READ):
  acms.work.get            (URI: acms://work/{work_uid_or_id})
  acms.work.current        (URI: acms://assignment/current)
  acms.project.get         (URI: acms://project/{project_id_or_slug})
  acms.product.get         (URI: acms://product/{product_id})
  acms.artifact.get        (URI: acms://artifact/{artifact_uid_or_id})
  acms.agent.self          (URI: acms://agent/self)
  acms.policy.current      (URI: acms://policy/current)
  acms.inbox.current       (URI: acms://inbox/current)
  acms.context.current     (URI: acms://context/current)  <- plan §14 manifest

Worker tools (SAFE_WRITE unless noted):
  acms.work.update_progress     (scope: own assignment)
  acms.work.mark_blocked        (scope: own assignment)
  acms.artifact.create          (scope: own assignment)
  acms.work.submit_handoff      (scope: own assignment)
  acms.inbox.raise              (scope: own assignment; severity-bounded)
  acms.review.request           (scope: own assignment)

Scope rules:
- agent token (worker role): may read Work/Project/Product/Artifacts linked to
  its CURRENT ACTIVE assignment + general non-sensitive metadata; may write
  only within its own assignment (work/assignment/artifact linkage enforced).
- assignment token: narrowed to exactly its Work UID (+ project/jira binding).
- executive/infrastructure_admin: broader reads; still no DESTRUCTIVE ops in W1.
"""
from __future__ import annotations

import base64
import json
import ssl
import urllib.error
import urllib.request
from typing import Any

from .errors import DomainUnavailableError, NotFoundError, OutOfScopeError, ValidationError_
from .models import CallIdentity, RiskClass, Role
from .policy import Capability


class AcmsClient:
    """Thin REST client for ACMS. TLS verification ON by default; CT122 uses a
    self-signed cert, so the gateway trusts the local CA file via env
    (ACMS_TLS_CA) or (explicitly, config-gated) disables verification for the
    internal raw-IP endpoint."""

    def __init__(self, base_url: str, token: str, *, tls_ca: str | None = None,
                 insecure_tls: bool = False, timeout_seconds: int = 15):
        if not base_url:
            raise ValueError("ACMS base URL not configured")
        if not token:
            raise ValueError("ACMS service token not configured")
        self.base_url = base_url.rstrip("/")
        self._token = token
        self.timeout = timeout_seconds
        if insecure_tls:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            self._ctx: ssl.SSLContext | None = ctx
        elif tls_ca:
            self._ctx = ssl.create_default_context(cafile=tls_ca)
        else:
            self._ctx = None  # default verification

    def request(self, method: str, path: str, payload: dict[str, Any] | None = None,
                query: dict[str, str] | None = None) -> tuple[int, Any]:
        url = f"{self.base_url}{path}"
        if query:
            from urllib.parse import urlencode
            url += "?" + urlencode(query)
        data = None
        headers = {"Authorization": f"Bearer {self._token}", "Accept": "application/json"}
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout, context=self._ctx) as resp:
                body = resp.read().decode("utf-8", errors="replace")
                status = resp.status
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            status = exc.code
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise DomainUnavailableError(f"ACMS unreachable: {exc.__class__.__name__}") from exc
        try:
            parsed = json.loads(body) if body else None
        except ValueError:
            parsed = body
        return status, parsed

    # ---------------- lookups ----------------

    def find_work_item(self, ref: str) -> dict[str, Any] | None:
        """Resolve a Work reference: UID, short key, or UUID. None when absent."""
        if not ref:
            return None
        status, body = self.request("GET", f"/api/v1/work/items/{ref}")
        if status == 200:
            return body
        # list + filter by UID/key (the detail route may accept only UUIDs)
        status, body = self.request("GET", "/api/v1/work/items")
        if status != 200 or not isinstance(body, list):
            raise DomainUnavailableError(f"ACMS work list failed (HTTP {status})")
        for item in body:
            if ref in (item.get("work_item_id"), item.get("work_key"), item.get("work_uid")):
                return item
        return None

    def active_assignment(self, agent_id: str) -> dict[str, Any] | None:
        status, body = self.request("GET", "/api/v1/work/assignments", query={"agent_id": agent_id, "status": "ACTIVE"})
        if status != 200 or not isinstance(body, list):
            raise DomainUnavailableError(f"ACMS assignments failed (HTTP {status})")
        return body[0] if body else None

    def get_project(self, ref: str) -> dict[str, Any] | None:
        status, body = self.request("GET", f"/projects/{ref}")
        if status == 200:
            return body
        status, body = self.request("GET", "/projects")
        if status != 200 or not isinstance(body, list):
            raise DomainUnavailableError(f"ACMS projects failed (HTTP {status})")
        for p in body:
            if ref in (p.get("project_id"), p.get("slug"), p.get("name")):
                return p
        return None

    def get_product(self, product_id: str) -> dict[str, Any] | None:
        status, body = self.request("GET", f"/products/{product_id}")
        if status == 200:
            return body
        return None

    def find_artifact(self, ref: str, *, work_item_id: str | None = None) -> dict[str, Any] | None:
        status, body = self.request("GET", f"/artifacts/{ref}")
        if status == 200:
            return body
        # artifact detail route accepts UUID/UID/sha; fall back to list filter
        status, body = self.request("GET", "/artifacts")
        if status != 200 or not isinstance(body, list):
            raise DomainUnavailableError(f"ACMS artifacts failed (HTTP {status})")
        for a in body:
            if ref in (a.get("artifact_id"), a.get("artifact_uid")):
                return a
        return None

    def artifact_content(self, artifact_id: str) -> str | None:
        status, body = self.request("GET", f"/artifacts/{artifact_id}/content")
        if status != 200:
            return None
        if isinstance(body, dict):
            return body.get("content")
        return body if isinstance(body, str) else None

    def get_agent(self, agent_id: str) -> dict[str, Any] | None:
        status, body = self.request("GET", f"/api/v1/agents/{agent_id}")
        if status == 200:
            return body
        status, body = self.request("GET", "/api/v1/agents")
        if status != 200 or not isinstance(body, list):
            raise DomainUnavailableError(f"ACMS agents failed (HTTP {status})")
        for a in body:
            if ref_match(agent_id, a):
                return a
        return None

    # ---------------- scope enforcement helper ----------------

    @staticmethod
    def assert_assignment_scope(identity: CallIdentity, work: dict[str, Any],
                                assignment: dict[str, Any] | None) -> None:
        """Raise OutOfScopeError unless identity may act on this work item."""
        if identity.has_role("executive", "infrastructure_admin"):
            return
        if identity.is_assignment_scoped:
            if identity.work_uid and work.get("work_uid") != identity.work_uid                and work.get("work_key") != identity.work_uid                and work.get("work_item_id") != identity.work_uid:
                raise OutOfScopeError(
                    f"assignment token is bound to {identity.work_uid}; "
                    f"{work.get('work_uid') or work.get('work_item_id')} is out of scope"
                )
            return
        # agent token: needs the work item to be its own ACTIVE assignment
        if assignment is None or assignment.get("agent_id") not in (identity.acms_agent_id,):
            raise OutOfScopeError(
                "SAFE_WRITE requires an ACTIVE assignment for your agent "
                "(or an assignment-scoped token)"
            )


def ref_match(agent_ref: str, agent_obj: dict[str, Any]) -> bool:
    return agent_ref in (
        agent_obj.get("agent_id"),
        agent_obj.get("display_name"),
        agent_obj.get("external_registration_id"),
        agent_obj.get("legacy_name"),
        agent_obj.get("worker_uid") and f"uid-{agent_obj.get('worker_uid')}",
    )
