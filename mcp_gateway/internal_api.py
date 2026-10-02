"""Gateway internal API (plan §13/§23/§24) — machine-to-machine surface for ACMS.

Mounted at ``/internal`` on the SAME port as the MCP transport but BEFORE the
identity middleware (it authenticates with its own shared secret, not MCP
bearer tokens). VM114-private: ACMS (CT122) reaches it over the LAN.

Endpoints (all require ``Authorization: Bearer <MCP_GATEWAY_INTERNAL_TOKEN>``):

- POST /internal/mint-assignment   — dispatch-time assignment-token auto-mint
  (plan §13). Body: {agent_name, work_uid, ttl_hours?, jira_issue_key?,
  project_slug?, repositories?}. The raw token is returned EXACTLY ONCE in the
  response (VM114→CT122 hop is private LAN; ACMS embeds it in the dispatch
  envelope — the worker's only copy).
- GET  /internal/capability-card/{agent_name} — capability card data (plan §23):
  agent-token status, roles/scopes, available resources/tools, last MCP activity.
- GET  /internal/activity?agent_name=&work_uid=&limit= — MCP activity rows
  (plan §24 shape; arguments hashed upstream, no secrets ever).
- GET  /internal/approvals?status=PENDING — approval request queue (plan §36).
- POST /internal/approvals/{id}/decide — executive decision {decision: APPROVED|
  DENIED, decided_by, note?} (also available via CLI; this endpoint exists so
  the ACMS UI can surface a one-click decision later without new gateway auth).

SECURITY:
- Unknown/missing internal token => 401, audited.
- Token compare is constant-time.
- No listing of raw tokens ever; mint returns the raw ONCE (in-memory only).
- The endpoint set is deliberately TINY; no generic proxy or shell capability.
"""
from __future__ import annotations

import hmac
import json
from typing import Any

from .approvals import APPROVED, DENIED, ApprovalStore
from .audit import AuditLog
from .errors import GatewayError, GatewayErrorType
from .tokens import TokenStore

# canonical dotted names exported to capability cards, per domain (plan §23:
# "resources available / tools available" — mirrors the registry, but the
# registry holds policy objects; the card exports STABLE names + risk only).
CARD_RESOURCES = [
    {"name": "acms.work.get", "risk": "READ"},
    {"name": "acms.work.current", "risk": "READ"},
    {"name": "acms.project.get", "risk": "READ"},
    {"name": "acms.product.get", "risk": "READ"},
    {"name": "acms.artifact.get", "risk": "READ"},
    {"name": "acms.agent.self", "risk": "READ"},
    {"name": "acms.policy.current", "risk": "READ"},
    {"name": "acms.inbox.current", "risk": "READ"},
    {"name": "acms.context.current", "risk": "READ"},
    {"name": "llm.models.list", "risk": "READ"},
    {"name": "llm.model_health", "risk": "READ"},
    {"name": "llm.loaded_models", "risk": "READ"},
    {"name": "llm.host_capacity", "risk": "READ"},
    {"name": "llm.route_status", "risk": "READ"},
    {"name": "llm.usage", "risk": "READ"},
    {"name": "runtime.self", "risk": "READ"},
]
CARD_TOOLS = [
    {"name": "acms.work.update_progress", "risk": "SAFE_WRITE"},
    {"name": "acms.work.mark_blocked", "risk": "SAFE_WRITE"},
    {"name": "acms.artifact.create", "risk": "SAFE_WRITE"},
    {"name": "acms.work.submit_handoff", "risk": "SAFE_WRITE"},
    {"name": "acms.inbox.raise", "risk": "SAFE_WRITE"},
    {"name": "acms.review.request", "risk": "SAFE_WRITE"},
    {"name": "llm.request_model", "risk": "SAFE_WRITE"},
    {"name": "llm.request_fallback", "risk": "SAFE_WRITE"},
    {"name": "runtime.restart_self", "risk": "SENSITIVE_WRITE"},
]
CARD_RESOURCES += [
    {"name": "runtime.fleet", "risk": "READ"},
    {"name": "llm.model.get", "risk": "READ"},
]


def _json(status: int, body: dict[str, Any] | list[Any]):
    return status, [
        (b"content-type", b"application/json"),
    ], json.dumps(body, default=str).encode()


class InternalApi:
    """Tiny ASGI app for /internal/* (shared-secret auth, audited)."""

    def __init__(self, *, tokens: TokenStore, audit: AuditLog, approvals: ApprovalStore,
                 internal_token: str):
        self.tokens = tokens
        self.audit = audit
        self.approvals = approvals
        self._expected = (internal_token or "").encode()

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self._send(send, *_json(404, {"error": "not found"}))
            return
        path = scope.get("path", "")
        method = scope.get("method", "GET")
        # ---- auth (constant-time) ----
        headers = {
            k.decode("latin-1").lower(): v.decode("latin-1")
            for k, v in scope.get("headers", [])
        }
        auth = headers.get("authorization", "")
        provided = auth.removeprefix("Bearer ").strip().encode() if auth.startswith("Bearer ") else b""
        ok = bool(self._expected) and len(provided) == len(self._expected) and \
            hmac.compare_digest(provided, self._expected)
        caller = "acms-internal"
        if not ok:
            self._audit(caller, f"{method} {path}", "denied", "UNAUTHENTICATED",
                        "missing/invalid internal token")
            await self._send(send, *_json(401, {"error": "UNAUTHENTICATED"}))
            return

        try:
            status, hdrs, body = await self._route(method, path, scope, receive)
        except GatewayError as exc:
            self._audit(caller, f"{method} {path}", "error", exc.error_type.value, exc.detail)
            status, hdrs, body = _json(400, {"error": exc.error_type.value, "detail": exc.detail})
        except (ValueError, TypeError, KeyError) as exc:
            self._audit(caller, f"{method} {path}", "error", "VALIDATION", str(exc)[:200])
            status, hdrs, body = _json(422, {"error": "VALIDATION", "detail": str(exc)[:200]})
        await self._send(send, status, hdrs, body)

    # ---------------- routing ----------------

    async def _route(self, method, path, scope, receive):
        if path == "/internal/mint-assignment" and method == "POST":
            body = await self._read_json(scope, receive)
            return await self.mint_assignment(body)
        if path.startswith("/internal/capability-card/") and method == "GET":
            agent_name = path.rsplit("/", 1)[-1]
            return _json(200, self.capability_card(agent_name))
        if path == "/internal/activity" and method == "GET":
            params = self._query(scope)
            rows = self.audit.recent(
                limit=min(int(params.get("limit", "25")), 200),
                agent_name=params.get("agent_name") or None,
                work_uid=params.get("work_uid") or None,
                denied_only=params.get("denied") == "1",
            )
            return _json(200, {"activity": rows})
        if path == "/internal/approvals" and method == "GET":
            params = self._query(scope)
            return _json(200, {"approvals": self.approvals.list(
                status=params.get("status") or None, limit=100)})
        if path.startswith("/internal/approvals/") and path.endswith("/decide") and method == "POST":
            req_id = path[len("/internal/approvals/"):-len("/decide")]
            body = await self._read_json(scope, receive)
            return self.decide_approval(req_id, body)
        if path == "/internal/health":
            return _json(200, {"status": "ok"})
        return _json(404, {"error": "not found"})

    # ---------------- handlers ----------------

    async def mint_assignment(self, body: dict[str, Any]):
        agent_name = (body.get("agent_name") or "").strip()
        work_uid = (body.get("work_uid") or "").strip()
        if not agent_name or not work_uid:
            raise GatewayError("agent_name and work_uid are required (VALIDATION)")
        ttl_hours = int(body.get("ttl_hours") or 8)
        if not (1 <= ttl_hours <= 72):
            raise GatewayError("ttl_hours must be 1..72 (VALIDATION)")
        # verify the agent actually holds an active gateway identity (fail-closed
        # against minting tokens for unknown agents)
        agent_tokens = [t for t in self.tokens.list_agent_tokens(include_revoked=False)
                        if t.agent_name == agent_name and t.is_active()]
        if not agent_tokens:
            self._audit("acms-internal", "POST /internal/mint-assignment", "denied",
                        "OUT_OF_SCOPE", f"no active agent token for {agent_name}")
            return _json(404, {"error": "OUT_OF_SCOPE",
                               "detail": f"no active agent token for {agent_name}"})
        rec, raw = self.tokens.mint_assignment_token(
            agent_name=agent_name,
            work_uid=work_uid,
            agent_token_hash=agent_tokens[0].token_hash,  # operator path: hash-authenticated mint
            ttl_hours=ttl_hours,
            project_id=body.get("project_id"),
            project_slug=body.get("project_slug"),
            jira_issue_key=body.get("jira_issue_key"),
            repositories=list(body.get("repositories") or []),
        )
        self._audit("acms-internal", "POST /internal/mint-assignment", "ok",
                    detail=f"assignment token for {agent_name}/{work_uid} ttl={ttl_hours}h")
        return _json(201, {
            "token_id": rec.token_id,
            "agent_name": rec.agent_name,
            "work_uid": rec.work_uid,
            "project_slug": rec.project_slug,
            "jira_issue_key": rec.jira_issue_key,
            "expires_at": rec.expires_at.isoformat() if rec.expires_at else None,
            "token": raw,  # RAW SHOWN ONCE — travels to the worker via the dispatch envelope
        })

    def capability_card(self, agent_name: str) -> dict[str, Any]:
        agent_tokens = [t for t in self.tokens.list_agent_tokens()
                        if t.agent_name == agent_name]
        active = [t for t in agent_tokens if t.is_active()]
        asg = [t for t in self.tokens.list_assignment_tokens(agent_name=agent_name)]
        active_asg = [t for t in asg if t.is_active()]
        activity = self.audit.recent(limit=1, agent_name=agent_name)
        return {
            "agent_name": agent_name,
            "gateway": "miam-mcp-gateway",
            "mcp_connected": bool(active),
            "agent_token": {
                "active": bool(active),
                "count_total": len(agent_tokens),
                "roles": sorted({r for t in active for r in t.roles}),
                "scopes": sorted({s for t in active for s in t.scopes}),
                "expires_at": active[0].expires_at.isoformat() if active and active[0].expires_at else None,
            },
            "assignment_token": {
                "active_count": len(active_asg),
                "work_uids": sorted({t.work_uid for t in active_asg}),
                "latest_expires_at": (active_asg[0].expires_at.isoformat()
                                      if active_asg and active_asg[0].expires_at else None),
            },
            "resources_available": CARD_RESOURCES,
            "tools_available": CARD_TOOLS,
            "tools_requiring_approval": [t["name"] for t in CARD_TOOLS
                                         if t["risk"] == "SENSITIVE_WRITE"],
            "last_mcp_activity": activity[0] if activity else None,
        }

    def decide_approval(self, request_id: str, body: dict[str, Any]):
        decision = (body.get("decision") or "").strip().upper()
        decided_by = (body.get("decided_by") or "acms-internal").strip()
        if decision not in (APPROVED, DENIED):
            raise GatewayError("decision must be APPROVED or DENIED (VALIDATION)")
        out = self.approvals.decide(request_id, decision=decision, decided_by=decided_by,
                                    note=body.get("note") or "")
        if out is None:
            return _json(404, {"error": "not found"})
        self._audit("acms-internal", f"POST /internal/approvals/{request_id}/decide", "ok",
                    detail=f"{decision} by {decided_by}")
        return _json(200, {"approval": out})

    # ---------------- plumbing ----------------

    def _audit(self, caller: str, name: str, status: str, error_type: str | None = None,
               detail: str = ""):
        from .models import AuditEntry, RiskClass

        self.audit.record(AuditEntry(
            agent_name=caller, token_id="internal", token_kind="internal",
            domain="gateway", name=name, entry_kind="internal",
            risk_class=RiskClass.READ, status=status,
            error_type=error_type, detail=detail,
        ))

    @staticmethod
    async def _read_json(scope, receive) -> dict[str, Any]:
        body = b""
        while True:
            message = await receive()
            if message["type"] == "http.request":
                body += message.get("body", b"")
                if not message.get("more_body"):
                    break
            elif message["type"] == "http.disconnect":
                break
        if not body:
            return {}
        try:
            parsed = json.loads(body.decode("utf-8"))
            return parsed if isinstance(parsed, dict) else {}
        except (ValueError, UnicodeDecodeError):
            return {}

    @staticmethod
    def _query(scope) -> dict[str, str]:
        from urllib.parse import parse_qs

        raw = scope.get("query_string", b"").decode("latin-1")
        return {k: v[0] for k, v in parse_qs(raw).items() if v}

    @staticmethod
    async def _send(send, status, headers, body):
        await send({"type": "http.response.start", "status": status,
                    "headers": [(k, v if isinstance(v, bytes) else v.encode()) for k, v in headers]})
        await send({"type": "http.response.body", "body": body})
