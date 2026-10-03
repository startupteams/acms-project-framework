"""DKMS adapter (plan P2): knowledge management domain.

Thin REST client for the DKMS service at http://10.0.20.190:30800 (VM117/K3s).
Gateway holds its own service credential (DKMS_API_TOKEN, env-injected — never
hardcoded). No TLS required on the private LAN NodePort; supports TLS if the
base_url starts with https (future DNS/TLS). Follows the AcmsClient pattern.

Surface (plan P2):

Resources (READ):
  dkms.record.get         (URI: dkms://record/{uid})
  dkms.list.recent        (URI: dkms://records/recent?limit={limit})
  dkms.search             (URI: dkms://search?q={query})
  dkms.record.related     (URI: dkms://record/{uid}/related)
  dkms.export.markdown    (URI: dkms://export/markdown?work_uid={work_uid})
  dkms.audit.recent       (URI: dkms://audit/recent)

Worker tools (SAFE_WRITE):
  dkms.ingest_plan           (create plan record)
  dkms.ingest_handoff        (create handoff-source + handoff-summary + link)
  dkms.record_timeline_entry (create implementation record)
  dkms.record_failure        (POST /api/v1/failures)
  dkms.record_recovery_attempt (POST /api/v1/failures/{uid}/attempts)
  dkms.record_lesson         (create lesson_learned — NO own_work requirement)
  dkms.record_decision       (create decision record)
  dkms.mark_training_eligibility (executive/infrastructure_admin only)
"""
from __future__ import annotations

import json
import ssl
import urllib.error
import urllib.request
from typing import Any

from .errors import DomainUnavailableError, NotFoundError, QuarantinedContentError


class DkmsClient:
    """Thin REST client for DKMS. Plain HTTP on the LAN NodePort; TLS when
    base_url starts with https (future DNS/TLS, same pattern as AcmsClient)."""

    def __init__(self, base_url: str, token: str, *, timeout_seconds: int = 15):
        if not base_url:
            raise ValueError("DKMS base URL not configured")
        if not token:
            raise ValueError("DKMS service token not configured")
        self.base_url = base_url.rstrip("/")
        self._token = token
        self.timeout = timeout_seconds
        if self.base_url.startswith("https://"):
            self._ctx: ssl.SSLContext | None = ssl.create_default_context()
        else:
            self._ctx = None

    def _request(self, method: str, path: str, payload: dict[str, Any] | None = None,
                 query: dict[str, str] | None = None, *, auth: bool = True,
                 accept: str = "application/json") -> tuple[int, Any]:
        url = f"{self.base_url}{path}"
        if query:
            from urllib.parse import urlencode
            filtered = {k: v for k, v in query.items() if v is not None and v != ""}
            if filtered:
                url += "?" + urlencode(filtered)
        data = None
        headers: dict[str, str] = {"Accept": accept}
        if auth:
            headers["Authorization"] = f"Bearer {self._token}"
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
            raise DomainUnavailableError(f"DKMS unreachable: {exc.__class__.__name__}") from exc
        if accept == "text/markdown":
            return status, body
        try:
            parsed = json.loads(body) if body else None
        except ValueError:
            parsed = body
        return status, parsed

    def _handle_status(self, status: int, body: Any, resource_desc: str = "resource") -> None:
        """Common status handling: 404 → NotFoundError, 401/403 → auth rejected,
        422 → QuarantinedContentError (secret-like content), other 4xx/5xx → domain error."""
        if status == 404:
            raise NotFoundError(f"DKMS {resource_desc} not found")
        if status in (401, 403):
            raise DomainUnavailableError("DKMS auth rejected")
        if status == 422:
            detail = "content quarantined (secret-like material detected)"
            if isinstance(body, dict) and body.get("detail"):
                detail = str(body["detail"])
            raise QuarantinedContentError(detail)
        if status >= 400:
            raise DomainUnavailableError(f"DKMS request failed (HTTP {status})")

    # ---------------- health (no auth) ----------------

    def health(self) -> dict[str, Any]:
        status, body = self._request("GET", "/health", auth=False)
        if status != 200:
            raise DomainUnavailableError(f"DKMS health failed (HTTP {status})")
        return body if isinstance(body, dict) else {"status": body}

    # ---------------- records ----------------

    def get_record(self, uid: str) -> dict[str, Any]:
        status, body = self._request("GET", f"/api/v1/records/{uid}")
        self._handle_status(status, body, f"record {uid}")
        return body

    def list_records(self, filters: dict[str, Any]) -> list[dict[str, Any]]:
        limit = filters.get("limit")
        if limit is not None:
            limit = int(limit)
            if limit < 1 or limit > 200:
                limit = max(1, min(200, limit))
            filters = {**filters, "limit": str(limit)}
        query = {k: str(v) for k, v in filters.items() if v is not None}
        status, body = self._request("GET", "/api/v1/records", query=query)
        self._handle_status(status, body, "records list")
        return body if isinstance(body, list) else []

    def search(self, q: str, limit: int = 20) -> list[dict[str, Any]]:
        limit = max(1, min(200, limit))
        status, body = self._request("GET", "/api/v1/search",
                                     query={"q": q, "limit": str(limit)})
        self._handle_status(status, body, "search")
        return body if isinstance(body, list) else []

    def related(self, uid: str) -> list[dict[str, Any]]:
        status, body = self._request("GET", f"/api/v1/records/{uid}/related")
        self._handle_status(status, body, f"related records for {uid}")
        return body if isinstance(body, list) else []

    def create_record(self, payload: dict[str, Any]) -> dict[str, Any]:
        status, body = self._request("POST", "/api/v1/records", payload=payload)
        self._handle_status(status, body, "record create")
        return body

    # ---------------- links ----------------

    def create_link(self, payload: dict[str, Any]) -> dict[str, Any]:
        status, body = self._request("POST", "/api/v1/links", payload=payload)
        self._handle_status(status, body, "link create")
        return body

    # ---------------- failures + recovery attempts ----------------

    def create_failure(self, payload: dict[str, Any]) -> dict[str, Any]:
        status, body = self._request("POST", "/api/v1/failures", payload=payload)
        self._handle_status(status, body, "failure create")
        return body

    def create_recovery_attempt(self, failure_uid: str, payload: dict[str, Any]) -> dict[str, Any]:
        status, body = self._request("POST", f"/api/v1/failures/{failure_uid}/attempts",
                                     payload=payload)
        self._handle_status(status, body, f"recovery attempt for {failure_uid}")
        return body

    # ---------------- training eligibility ----------------

    def set_training_eligibility(self, uid: str, payload: dict[str, Any]) -> dict[str, Any]:
        status, body = self._request("POST", f"/api/v1/records/{uid}/training-eligibility",
                                     payload=payload)
        self._handle_status(status, body, f"training eligibility for {uid}")
        return body

    # ---------------- export ----------------

    def export_markdown(self, query: dict[str, str]) -> str:
        status, body = self._request("GET", "/api/v1/export/markdown",
                                     query=query, accept="text/markdown")
        if status == 404:
            raise NotFoundError("DKMS export not found")
        if status in (401, 403):
            raise DomainUnavailableError("DKMS auth rejected")
        if status >= 400:
            raise DomainUnavailableError(f"DKMS export failed (HTTP {status})")
        return body if isinstance(body, str) else ""

    # ---------------- audit ----------------

    def audit(self) -> list[dict[str, Any]]:
        status, body = self._request("GET", "/api/v1/audit")
        self._handle_status(status, body, "audit")
        return body if isinstance(body, list) else []
