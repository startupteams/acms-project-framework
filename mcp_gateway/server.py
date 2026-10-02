"""Gateway shell: FastMCP server + auth middleware + ACMS adapter wiring.

Transport: streamable HTTP at ``/mcp`` (stateless mode, JSON responses).
Auth: Bearer token on EVERY HTTP request (agent token or assignment token).
Identity propagation: ASGI middleware resolves the token and sets a
contextvar; tool/resource handlers read it (verified pattern for the official
SDK under anyio). Fail-closed: unresolvable token => 401 JSON-RPC error before
any handler runs; every request is audited (plan §24).
"""
from __future__ import annotations

import contextvars
import logging
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.tools.base import Tool
from mcp.server.transport_security import TransportSecuritySettings

from . import __version__
from .acms_adapter import AcmsClient
from .approvals import ApprovalStore
from .audit import AuditLog
from .errors import (
    ApprovalRequiredError,
    ConflictError,
    DomainUnavailableError,
    ExpiredAgentTokenError,
    ExpiredAssignmentTokenError,
    GatewayError,
    GatewayErrorType,
    NotFoundError,
    OutOfScopeError,
    RevokedAgentTokenError,
    UnauthenticatedError,
    ValidationError_,
)
from .internal_api import InternalApi
from .github_adapter import GithubClient, branch_owner, owned_branch
from .llm_adapter import ServerManagerClient
from .manifest import build_context_manifest
from .models import (
    AuditEntry,
    CallIdentity,
    RiskClass,
    TokenKind,
    hash_args,
)
from .policy import (
    Capability,
    PolicyRegistry,
    _TEMPLATE_PARAM as _TEMPLATE_PARAM_SEARCH,
    dotted,
    match_uri_template,
)
from .tokens import TokenStore

log = logging.getLogger("mcp_gateway")

CURRENT_IDENTITY: contextvars.ContextVar[CallIdentity | None] = contextvars.ContextVar(
    "gateway_identity", default=None
)

# Health/capabilities endpoints are UNAUTHENTICATED (metadata only, no secrets);
# everything else requires a token.
_PUBLIC_PATHS = {"/", "/health", "/healthz"}


@dataclass
class GatewayConfig:
    host: str = "127.0.0.1"
    port: int = 8202
    acms_base_url: str = ""           # e.g. https://10.0.20.122
    acms_token: str = ""              # gateway's ACMS credential (env-injected)
    acms_tls_ca: str | None = None
    acms_insecure_tls: bool = False   # explicit config gate for the raw-IP endpoint
    tokens_path: str = "/var/lib/miam-mcp-gateway/tokens.sqlite3"
    audit_path: str = "/var/lib/miam-mcp-gateway/activity.sqlite3"
    audit_mirror: str | None = None   # optional JSONL mirror path
    public_base_url: str = ""         # e.g. https://mcp.miam.home.arpa
    executive_agent_names: str = ""   # comma-separated agent names w/ elevated roles
    # Host-header allowlist (DNS-rebinding protection). Empty disables protection
    # (default SDK behavior); prod config SHOULD list the gateway host.
    allowed_hosts: str = ""
    # ---- W2 (plan §21/§13/§23) ----
    llm_base_url: str = ""            # Server Manager machine API, e.g. http://127.0.0.1:8300
    llm_token: str = ""               # Server Manager scoped service identity
    internal_token: str = ""          # shared secret for /internal/* (ACMS <-> gateway)
    approvals_path: str = "/var/lib/miam-mcp-gateway/approvals.sqlite3"
    # ---- W3 (plan §25) ----
    github_token: str = ""            # GitHub PAT (authority = token's own permissions)
    github_repos: str = ""            # comma-separated owner/name allowlist for agent tokens


def _parse_list(raw: str) -> list[str]:
    return [x.strip() for x in (raw or "").split(",") if x.strip()]


class IdentityMiddleware:
    """Resolve bearer -> CallIdentity per HTTP request; fail closed (401).
    Audits denials at the transport layer. Sets CURRENT_IDENTITY for handlers."""

    def __init__(self, app: Any, *, tokens: TokenStore, audit: AuditLog,
                 executive_names: set[str] | None = None):
        self.app = app
        self.tokens = tokens
        self.audit = audit
        self.executive_names = executive_names or set()

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("path") not in ("/mcp",):
            await self.app(scope, receive, send)
            return
        headers = {
            k.decode("latin-1").lower(): v.decode("latin-1")
            for k, v in scope.get("headers", [])
        }
        auth = headers.get("authorization", "")
        identity: CallIdentity | None = None
        error: GatewayError | None = None
        if not auth.startswith("Bearer "):
            error = UnauthenticatedError("missing bearer token")
        else:
            raw = auth.removeprefix("Bearer ").strip()
            agent_rec = self.tokens.resolve_agent_token(raw)
            if agent_rec is not None:
                if agent_rec.revoked_at is not None:
                    error = RevokedAgentTokenError("agent token revoked")
                elif not agent_rec.is_active():
                    error = ExpiredAgentTokenError("agent token expired")
                else:
                    roles = list(agent_rec.roles)
                    if agent_rec.agent_name in self.executive_names and "executive" not in roles:
                        roles.append("executive")
                    identity = CallIdentity(
                        token_kind=TokenKind.AGENT,
                        token_id=agent_rec.token_id,
                        agent_name=agent_rec.agent_name,
                        acms_agent_id=agent_rec.acms_agent_id,
                        roles=roles,
                        scopes=list(agent_rec.scopes),
                    )
            else:
                asg_rec = self.tokens.resolve_assignment_token(raw)
                if asg_rec is not None:
                    if asg_rec.revoked_at is not None:
                        error = RevokedAgentTokenError("assignment token revoked")
                    elif not asg_rec.is_active():
                        error = ExpiredAssignmentTokenError("assignment token expired")
                    else:
                        identity = CallIdentity(
                            token_kind=TokenKind.ASSIGNMENT,
                            token_id=asg_rec.token_id,
                            agent_name=asg_rec.agent_name,
                            roles=["worker"],
                            scopes=list(asg_rec.scopes),
                            work_uid=asg_rec.work_uid,
                            project_id=asg_rec.project_id,
                            project_slug=asg_rec.project_slug,
                            jira_issue_key=asg_rec.jira_issue_key,
                            repositories=list(asg_rec.repositories),
                        )
                else:
                    error = UnauthenticatedError("unknown token")

        if identity is None:
            entry = AuditEntry(
                agent_name="(unauthenticated)",
                token_id="",
                token_kind="agent",
                domain="gateway",
                name=f"transport.{scope.get('path','/mcp')}",
                entry_kind="transport",
                risk_class=RiskClass.READ,
                status="denied",
                http_status=401,
                error_type=error.error_type.value if error else "UNAUTHENTICATED",
                detail=error.detail if error else "unauthenticated",
            )
            self.audit.record(entry)
            # 401 as a JSON-RPC error body (SDK surfaces HTTP status; clients see auth failure)
            import json as _json

            body = _json.dumps({
                "jsonrpc": "2.0", "id": None,
                "error": {"code": -32001,
                          "message": (f"{error.error_type.value}: {error.detail}"
                                      if error else "UNAUTHENTICATED")},
            }).encode()
            await send({
                "type": "http.response.start",
                "status": 401,
                "headers": [(b"content-type", b"application/json")],
            })
            await send({"type": "http.response.body", "body": body})
            return

        token = CURRENT_IDENTITY.set(identity)
        try:
            await self.app(scope, receive, send)
        finally:
            CURRENT_IDENTITY.reset(token)


class GatewayServer:
    """Owns the FastMCP instance + registry + adapters; exposes the ASGI app."""

    def __init__(self, config: GatewayConfig, *, tokens: TokenStore, audit: AuditLog,
                 acms_client: AcmsClient | None = None,
                 llm_client: ServerManagerClient | None = None,
                 github_client: GithubClient | None = None,
                 approvals: ApprovalStore | None = None):
        self.config = config
        self.tokens = tokens
        self.audit = audit
        self.approvals = approvals or ApprovalStore(config.approvals_path)
        self.registry = PolicyRegistry()
        self.acms = acms_client or AcmsClient(
            config.acms_base_url,
            config.acms_token,
            tls_ca=config.acms_tls_ca,
            insecure_tls=config.acms_insecure_tls,
        )
        self.llm: ServerManagerClient | None = llm_client or (
            ServerManagerClient(config.llm_base_url, config.llm_token)
            if config.llm_base_url and config.llm_token else None
        )
        self.github: GithubClient | None = github_client or (
            GithubClient(config.github_token,
                         repos_allowlist=_parse_list(config.github_repos))
            if config.github_token else None
        )
        self.internal = InternalApi(tokens=tokens, audit=audit, approvals=self.approvals,
                                    internal_token=config.internal_token)
        self.executive_names = set(_parse_list(config.executive_agent_names))

        transport_security = TransportSecuritySettings(
            enable_dns_rebinding_protection=bool(config.allowed_hosts),
            allowed_hosts=_parse_list(config.allowed_hosts),
        ) if config.allowed_hosts else TransportSecuritySettings(
            enable_dns_rebinding_protection=False
        )

        self.mcp = FastMCP(
            "miam-mcp-gateway",
            instructions=(
                "MIAM internal MCP gateway. Internal Startup Teams agents only. "
                "Auth via Bearer agent/assignment token on every request. "
                "Domains: acms.* in W1; llm/runtime/github/proxmox/pdu/power/jira/registry later."
            ),
            stateless_http=True,
            json_response=True,
            transport_security=transport_security,
            host=config.host,
            port=config.port,
        )

        self._build_acms_capabilities()
        self._build_llm_runtime_capabilities()
        self._build_github_capabilities()
        self.app = self._wrap_with_identity(self.mcp.streamable_http_app())

    # ------------- public ASGI app (adds /health + /internal) -------------

    def _wrap_with_identity(self, mcp_asgi):
        outer_app = self  # for health closure

        class _HealthShim:
            def __init__(self, inner, internal_api=None):
                self.inner = inner
                self.internal_api = internal_api

            async def __call__(self, scope, receive, send):
                if scope["type"] == "http" and scope.get("path") in _PUBLIC_PATHS:
                    import json as _json
                    body = _json.dumps({
                        "status": "ok",
                        "gateway": "miam-mcp-gateway",
                        "version": __version__,
                        "domains": ["acms", "llm", "runtime"]
                                   + (["github"] if outer_app.github else []),
                    }).encode()
                    await send({
                        "type": "http.response.start",
                        "status": 200,
                        "headers": [(b"content-type", b"application/json")],
                    })
                    await send({"type": "http.response.body", "body": body})
                    return
                if scope["type"] == "http" and scope.get("path", "").startswith("/internal"):
                    # /internal/* = own shared-secret auth; runs BEFORE the MCP
                    # identity middleware (which only handles /mcp anyway).
                    await self.internal_api(scope, receive, send)
                    return
                await self.inner(scope, receive, send)

        return IdentityMiddleware(_HealthShim(mcp_asgi, self.internal),
                                  tokens=self.tokens,
                                  audit=self.audit, executive_names=self.executive_names)

    # ------------- capability registration -------------

    def _register_tool(self, cap: Capability) -> None:
        """Register a policy-annotated tool. Handler is wrapped with:
        identity gating -> audit -> typed error mapping."""
        handler = cap.handler

        async def guarded(ctx: Context, _cap: Capability = cap, _handler=handler):
            identity = CURRENT_IDENTITY.get()
            entry = AuditEntry(
                agent_name=identity.agent_name if identity else "(none)",
                agent_id=identity.acms_agent_id if identity else None,
                token_id=identity.token_id if identity else "",
                token_kind=identity.token_kind.value if identity else "agent",
                work_uid=identity.work_uid if identity else None,
                domain=_cap.domain,
                name=_cap.name,
                entry_kind="tool",
                risk_class=_cap.risk,
                status="ok",
                approval=None,
                args_hash=hash_args(ARGS_HOLDER.get()),
            )
            start = time.monotonic()
            try:
                if identity is None:
                    raise UnauthenticatedError("no identity bound to this request")
                try:
                    _cap.authorize(identity)
                except ApprovalRequiredError:
                    # worker + SENSITIVE_WRITE: a one-time TTL-bounded approved
                    # grant (exact agent+capability match) authorizes THIS call
                    # and is consumed atomically (ADR-0021 W2 semantics).
                    if identity.agent_name and self.approvals.check_and_consume_grant(
                            identity.agent_name, _cap.name):
                        entry.approval = "one-time-grant"
                    else:
                        raise
                result = await _handler(identity, ctx)
                entry.duration_ms = int((time.monotonic() - start) * 1000)
                self.audit.record(entry)
                return result
            except GatewayError as exc:
                entry.duration_ms = int((time.monotonic() - start) * 1000)
                entry.status = "denied" if exc.error_type in {
                    GatewayErrorType.ROLE_REQUIRED,
                    GatewayErrorType.SCOPE_REQUIRED,
                    GatewayErrorType.OUT_OF_SCOPE,
                    GatewayErrorType.DESTRUCTIVE_DENY,
                    GatewayErrorType.APPROVAL_REQUIRED,
                } else "error"
                entry.error_type = exc.error_type.value
                entry.detail = exc.detail
                self.audit.record(entry)
                exc.args = (f"{exc.error_type.value}: {exc.detail}",)
                raise

        self._register_custom_tool(cap, guarded)

    def _register_custom_tool(self, cap: Capability, guarded) -> None:
        """Register a custom Tool whose input schema comes from the capability's
        declared schema and whose run() reads the JSON-RPC arguments into a
        contextvar holder, then invokes guarded(ctx) (identity + audit inside)."""
        from mcp.server.fastmcp.tools.base import Tool as _Tool

        input_schema = dict(cap.meta.get("input_schema") or {"type": "object", "properties": {}})
        if "type" not in input_schema:
            input_schema["type"] = "object"

        async def _placeholder_fn() -> None:  # never called; GatewayTool.run overrides
            raise RuntimeError("gateway tool ran without its run() override")

        from mcp.server.fastmcp.utilities.func_metadata import func_metadata

        class GatewayTool(_Tool):
            """SDK Tool with an overridden run(): reads JSON-RPC arguments into
            ARGS_HOLDER, then invokes guarded(ctx) (identity gate + audit)."""

            async def run(self, arguments: dict[str, Any], context: Context | None = None,
                          convert_result: bool = False) -> Any:
                ARGS_HOLDER.set(dict(arguments or {}))
                return await guarded(context)

        custom = GatewayTool(
            name=dotted(cap.name),
            title=None,
            description=cap.description,
            parameters=input_schema,
            fn=_placeholder_fn,
            fn_metadata=func_metadata(_placeholder_fn, structured_output=False),
            is_async=True,
            context_kwarg=None,
            annotations=None,
            icons=None,
            meta={"gateway_canonical_name": cap.name, "risk": cap.risk.value},
        )
        self.mcp._tool_manager._tools[dotted(cap.name)] = custom
        self.registry.register(cap)

    # ------------- ACMS capability construction (plan §18) -------------

    def _build_acms_capabilities(self) -> None:
        self._register_resources()
        self._register_worker_tools()

    def _register_resources(self) -> None:
        """Register resources via the SDK's template mechanism.

        Each template's function takes the URI params (+ optional Context) and
        internally: resolves CURRENT_IDENTITY, authorizes via the Capability,
        audits, then calls the resolver. Failures raise typed GatewayErrors,
        which the SDK surfaces as resource-read errors (isError on read).
        """
        acms = self.acms

        def _guard(cap, resolver):
            async def guarded(**kwargs):
                identity = CURRENT_IDENTITY.get()
                entry = AuditEntry(
                    agent_name=identity.agent_name if identity else "(none)",
                    agent_id=identity.acms_agent_id if identity else None,
                    token_id=identity.token_id if identity else "",
                    token_kind=identity.token_kind.value if identity else "agent",
                    work_uid=getattr(identity, "work_uid", None),
                    domain=cap.domain,
                    name=cap.name,
                    entry_kind="resource",
                    risk_class=cap.risk,
                    status="ok",
                    args_hash=hash_args(kwargs),
                )
                start = time.monotonic()
                try:
                    if identity is None:
                        raise UnauthenticatedError("no identity bound to this request")
                    cap.authorize(identity)
                    outcome = resolver(kwargs, identity)
                    result = await outcome if hasattr(outcome, "__await__") else outcome
                    entry.duration_ms = int((time.monotonic() - start) * 1000)
                    self.audit.record(entry)
                    return result
                except GatewayError as exc:
                    entry.duration_ms = int((time.monotonic() - start) * 1000)
                    entry.status = "denied" if exc.error_type in {
                        GatewayErrorType.ROLE_REQUIRED,
                        GatewayErrorType.SCOPE_REQUIRED,
                        GatewayErrorType.OUT_OF_SCOPE,
                        GatewayErrorType.DESTRUCTIVE_DENY,
                        GatewayErrorType.APPROVAL_REQUIRED,
                    } else "error"
                    entry.error_type = exc.error_type.value
                    entry.detail = exc.detail
                    self.audit.record(entry)
                    # Resource reads double-wrap into ValueError chains that LOSE
                    # the type; prefix the type into the message so clients can
                    # classify denials without parsing wrap layers.
                    exc.args = (f"{exc.error_type.value}: {exc.detail}",)
                    raise

            return guarded

        from mcp.server.fastmcp.resources.templates import FunctionResource

        def make_read_resource(name, uri_template, description, resolver):
            cap = Capability(
                name=name, domain="acms", risk=RiskClass.READ,
                roles={"worker", "reviewer", "project_manager", "executive",
                       "infrastructure_admin", "observer"},
                scopes={"acms.read"},
                description=description,
                kind="resource",
                uri_template=uri_template,
            )
            guarded = _guard(cap, resolver)
            if _TEMPLATE_PARAM_SEARCH.search(uri_template) is None:
                # STATIC resource: register CONCRETELY (the SDK template lookup
                # skips zero-param templates: `if params := matches()` is falsy
                # for {} — SDK walrus bug; concrete registration dodges it).
                async def static_fn() -> dict:
                    return await guarded()

                resource = FunctionResource.from_function(
                    static_fn, uri=uri_template, name=dotted(name),
                    description=description, mime_type="application/json",
                )
                self.mcp._resource_manager.add_resource(resource)
            else:
                # Parametrized: SDK template (URI params flow into guarded kwargs).
                self.mcp._resource_manager.add_template(
                    guarded,
                    uri_template=uri_template,
                    name=dotted(name),
                    description=description,
                    mime_type="application/json",
                )
            self.registry.register(cap)

        def resolve_work(uri_params, identity):
            ref = uri_params.get("work_uid_or_id", "")
            work = acms.find_work_item(ref)
            if work is None:
                raise NotFoundError(f"work item not found: {ref}")
            if identity.is_assignment_scoped and identity.work_uid:
                if not self._same_work(identity.work_uid, work):
                    raise OutOfScopeError(f"assignment token bound to {identity.work_uid}")
            return {"work": _prune_work(work)}

        def resolve_assignment_current(uri_params, identity):
            agent_id = identity.acms_agent_id
            assignment = None
            work = None
            if agent_id:
                assignment = acms.active_assignment(agent_id)
                if assignment:
                    work = acms.find_work_item(assignment.get("work_item_id", ""))
            if identity.is_assignment_scoped and identity.work_uid:
                work = acms.find_work_item(identity.work_uid)
            return {
                "assignment": assignment,
                "work": _prune_work(work) if work else None,
                "work_uid": identity.work_uid or (work or {}).get("work_uid"),
            }

        def resolve_project(uri_params, identity):
            ref = uri_params.get("project_ref", "")
            project = acms.get_project(ref)
            if project is None:
                raise NotFoundError(f"project not found: {ref}")
            if identity.is_assignment_scoped and identity.project_id:
                if project.get("project_id") != identity.project_id and                    ref not in (identity.project_id, identity.project_slug):
                    raise OutOfScopeError(
                        f"assignment token is bound to project {identity.project_slug or identity.project_id}"
                    )
            return {"project": _prune_project(project)}

        def resolve_product(uri_params, identity):
            ref = uri_params.get("product_id", "")
            product = acms.get_product(ref)
            if product is None:
                raise NotFoundError(f"product not found: {ref}")
            return {"product": _prune_product(product)}

        def resolve_artifact(uri_params, identity):
            ref = uri_params.get("artifact_ref", "")
            artifact = acms.find_artifact(ref)
            if artifact is None:
                raise NotFoundError(f"artifact not found: {ref}")
            if identity.is_assignment_scoped and identity.work_uid and                not identity.has_role("executive", "infrastructure_admin"):
                wid = artifact.get("work_item_id")
                work = acms.find_work_item(identity.work_uid)
                if work is None or (wid and work.get("work_item_id") != wid):
                    raise OutOfScopeError(
                        f"artifact belongs to a different work item than {identity.work_uid}"
                    )
            content = acms.artifact_content(artifact.get("artifact_id", ""))
            out = _prune_artifact(artifact)
            out["content"] = content
            return {"artifact": out}

        def resolve_agent_self(uri_params, identity):
            agent = None
            if identity.acms_agent_id:
                agent = acms.get_agent(identity.acms_agent_id)
            return {"agent": _prune_agent(agent) if agent else None,
                    "identity": identity.model_dump(mode="json")}

        def resolve_policy_current(uri_params, identity):
            return {
                "identity": identity.model_dump(mode="json"),
                "risk_classes": {r.name: r.value for r in RiskClass},
                "denied_forever": [
                    "proxmox.vm.delete (DESTRUCTIVE)",
                    "pdu.outlet.power_off (DESTRUCTIVE)",
                    "generic root-shell capability (never offered)",
                ],
                "worker_denials": [
                    "pause another worker", "restart another agent",
                    "cancel another Work Item", "create worker",
                    "delete Work Item", "change another agent's policy",
                ],
            }

        def resolve_inbox_current(uri_params, identity):
            agent_id = identity.acms_agent_id
            if not agent_id:
                raise NotFoundError("agent token not linked to an ACMS agent; inbox unavailable")
            status, body = acms.request("GET", "/inbox")
            if status != 200 or not isinstance(body, list):
                raise DomainUnavailableError(f"ACMS inbox failed (HTTP {status})")
            if identity.has_role("executive", "infrastructure_admin"):
                mine = body
            else:
                mine = [i for i in body if i.get("agent_id") in (agent_id, None)]
            return {"inbox": [_prune_inbox(i) for i in mine[:25]]}

        def resolve_context_current(uri_params, identity):
            agent_id = identity.acms_agent_id
            assignment = None
            work = None
            project = None
            agent = None
            if agent_id:
                agent = acms.get_agent(agent_id)
                assignment = acms.active_assignment(agent_id)
                if assignment:
                    work = acms.find_work_item(assignment.get("work_item_id", ""))
            if identity.is_assignment_scoped and identity.work_uid:
                work = acms.find_work_item(identity.work_uid)
            if work and work.get("project_id"):
                project = acms.get_project(work["project_id"])
            manifest = build_context_manifest(
                identity, work=_prune_work(work) if work else None,
                project=_prune_project(project) if project else None,
                agent=_prune_agent(agent) if agent else None,
                acms_base_url=self.config.acms_base_url,
            )
            return {"manifest": manifest}

        resources = [
            ("acms.work.get", "acms://work/{work_uid_or_id}", resolve_work,
             "Read one ACMS Work Item by UID/short key/UUID (scope-enforced)."),
            ("acms.work.current", "acms://assignment/current", resolve_assignment_current,
             "Read the caller's current ACTIVE assignment + linked Work."),
            ("acms.project.get", "acms://project/{project_ref}", resolve_project,
             "Read one ACMS Project by id/slug (assignment-scoped tokens limited to bound project)."),
            ("acms.product.get", "acms://product/{product_id}", resolve_product,
             "Read one ACMS Product by id."),
            ("acms.artifact.get", "acms://artifact/{artifact_ref}", resolve_artifact,
             "Read one canonical Artifact (metadata + content) by UID/UUID/sha."),
            ("acms.agent.self", "acms://agent/self", resolve_agent_self,
             "Read the caller's own ACMS agent identity (no other agents)."),
            ("acms.policy.current", "acms://policy/current", resolve_policy_current,
             "Read the caller's effective gateway policy (roles/scopes/denials)."),
            ("acms.inbox.current", "acms://inbox/current", resolve_inbox_current,
             "Read inbox rows relevant to the caller (workers: own rows only)."),
            ("acms.context.current", "acms://context/current", resolve_context_current,
             "Read the assignment context manifest (plan §14)."),
        ]
        for (name, template, resolver, desc) in resources:
            make_read_resource(name, template, desc, resolver)

    @staticmethod
    def _same_work(uid_or_key: str, work: dict) -> bool:
        return uid_or_key in (work.get("work_uid"), work.get("work_key"), work.get("work_item_id"))

    def _register_worker_tools(self) -> None:
        acms = self.acms

        async def own_work(identity: CallIdentity) -> tuple[dict, dict | None, dict]:
            """Resolve the caller's own work item + assignment (SAFE_WRITE basis)."""
            if identity.is_assignment_scoped and identity.work_uid:
                work = acms.find_work_item(identity.work_uid)
                if work is None:
                    raise NotFoundError(f"work item not found: {identity.work_uid}")
                return work, None, identity
            if not identity.acms_agent_id:
                raise OutOfScopeError("agent token lacks ACMS agent binding")
            assignment = acms.active_assignment(identity.acms_agent_id)
            if assignment is None:
                raise OutOfScopeError("no ACTIVE assignment; SAFE_WRITE requires one")
            work = acms.find_work_item(assignment.get("work_item_id", ""))
            if work is None:
                raise NotFoundError("linked work item missing")
            return work, assignment, identity

        async def update_progress(identity, ctx, note: str):
            work, assignment, _ = await own_work(identity)
            # Progress notes ride the durable A2A execution-event log (ADR-0014):
            # they attach to the RUNNING execution task for this work item.
            task = _running_task(acms, work)
            if task is None:
                raise ConflictError(
                    "no RUNNING execution task for this work item; progress notes "
                    "require an in-flight A2A run (update_progress is for run-time notes)"
                )
            status, body = acms.request(
                "POST", f"/execution/{task['task_id']}/events",
                payload={"kind": "PROGRESS_NOTE", "text": note},
            )
            if status not in (200, 202):
                raise DomainUnavailableError(f"ACMS progress event failed (HTTP {status})")
            return {"recorded": True, "work_uid": work.get("work_uid"),
                    "task_id": task.get("task_id")}

        async def mark_blocked(identity, ctx, reason: str):
            work, assignment, _ = await own_work(identity)
            status, body = acms.request(
                "PATCH", f"/api/v1/work/items/{work['work_item_id']}",
                payload={"status": "blocked"},
            )
            if status != 200:
                raise DomainUnavailableError(f"ACMS status update failed (HTTP {status})")
            if assignment:
                # keep assignment but ACMS policy: blocked work keeps assignment ACTIVE
                pass
            return {"marked": "blocked", "work_uid": work.get("work_uid")}

        async def create_artifact(identity, ctx, title: str, content_markdown: str,
                                  artifact_type: str = "handoff"):
            work, assignment, _ = await own_work(identity)
            payload = {
                "work_item_id": work["work_item_id"],
                "agent_id": identity.acms_agent_id,
                "artifact_type": artifact_type,
                "title": title,
                "content": content_markdown,
            }
            status, body = acms.request("POST", "/artifacts", payload=payload)
            if status != 201:
                raise DomainUnavailableError(f"ACMS artifact create failed (HTTP {status})")
            return {"artifact_uid": body.get("artifact_uid"),
                    "artifact_id": body.get("artifact_id")}

        async def submit_handoff(identity, ctx, title: str, body_markdown: str):
            work, assignment, _ = await own_work(identity)
            payload = {
                "work_item_id": work["work_item_id"],
                "agent_id": identity.acms_agent_id,
                "title": title,
                "body_markdown": body_markdown,
            }
            status, body = acms.request("POST", "/api/v1/work/handoffs", payload=payload)
            if status not in (200, 201):
                raise DomainUnavailableError(f"ACMS handoff failed (HTTP {status})")
            return {"handoff_id": body.get("handoff_id"),
                    "note": "canonical artifact generation happens at completion (ADR-0012)"}

        async def raise_inbox(identity, ctx, title: str, summary: str,
                              severity: str = "medium"):
            work, assignment, _ = await own_work(identity)
            if severity not in ("low", "medium", "high", "critical"):
                raise ValidationError_("severity must be low|medium|high|critical")
            payload = {
                "title": title, "summary": summary, "severity": severity,
                "item_class": "ACTION_REQUIRED",
                "agent_id": identity.acms_agent_id,
                "work_item_id": work["work_item_id"],
                "correlation_id": None,
            }
            # W1: inbox creation via the a2a API path (agent-facing)
            status, body = acms.request("POST", "/inbox", payload=payload)
            if status not in (200, 201):
                raise DomainUnavailableError(f"ACMS inbox create failed (HTTP {status})")
            return {"inbox_item_id": body.get("item_id")}

        async def request_review(identity, ctx, note: str = ""):
            work, assignment, _ = await own_work(identity)
            status, body = acms.request(
                "PATCH", f"/api/v1/work/items/{work['work_item_id']}",
                payload={"status": "in_review"},
            )
            if status != 200:
                raise DomainUnavailableError(f"ACMS status update failed (HTTP {status})")
            if assignment:
                acms.request("POST", f"/api/v1/work/assignments/{assignment['assignment_id']}/close",
                             payload={"new_status": "COMPLETED"})
            return {"work_uid": work.get("work_uid"), "status": "in_review", "note": note}

        for cap_spec in [
            ("acms.work.update_progress", "Append a progress note to the running execution (requires an in-flight A2A run).",
             {"type": "object", "properties": {"note": {"type": "string", "minLength": 1}},
              "required": ["note"]},
             update_progress, RiskClass.SAFE_WRITE),
            ("acms.work.mark_blocked", "Mark the assigned Work Item BLOCKED with a reason.",
             {"type": "object", "properties": {"reason": {"type": "string", "minLength": 1}},
              "required": ["reason"]},
             mark_blocked, RiskClass.SAFE_WRITE),
            ("acms.artifact.create", "Create an ACMS Artifact (markdown) linked to the assigned Work.",
             {"type": "object",
              "properties": {"title": {"type": "string", "minLength": 1},
                             "content_markdown": {"type": "string", "minLength": 1},
                             "artifact_type": {"type": "string", "default": "handoff"}},
              "required": ["title", "content_markdown"]},
             create_artifact, RiskClass.SAFE_WRITE),
            ("acms.work.submit_handoff", "Submit a handoff markdown to the assigned Work (canonical artifact generated at completion).",
             {"type": "object",
              "properties": {"title": {"type": "string", "minLength": 1},
                             "body_markdown": {"type": "string", "minLength": 1}},
              "required": ["title", "body_markdown"]},
             submit_handoff, RiskClass.SAFE_WRITE),
            ("acms.inbox.raise", "Raise a bounded inbox item (ACTION_REQUIRED) for humans.",
             {"type": "object",
              "properties": {"title": {"type": "string", "minLength": 1},
                             "summary": {"type": "string", "minLength": 1},
                             "severity": {"type": "string", "default": "medium"}},
              "required": ["title", "summary"]},
             raise_inbox, RiskClass.SAFE_WRITE),
            ("acms.review.request", "Request review: mark the assigned Work IN REVIEW and close the assignment COMPLETED.",
             {"type": "object", "properties": {"note": {"type": "string"}},
              "required": []},
             request_review, RiskClass.SAFE_WRITE),
        ]:
            name, desc, schema, fn, risk = cap_spec
            wrapped = _bind_args(fn)
            cap = Capability(
                name=name, domain="acms", risk=risk, roles={"worker"},
                scopes={"acms.write"}, description=desc, timeout_seconds=10,
                side_effect="external", meta={"input_schema": schema},
            )
            cap.handler = wrapped
            self._register_tool(cap)

    # ------------- W2: llm.* + runtime.* (plan §21) -------------

    def _build_llm_runtime_capabilities(self) -> None:
        sm = self.llm
        acms = self.acms
        approvals = self.approvals

        def _require_llm():
            if sm is None:
                raise DomainUnavailableError(
                    "llm domain not configured (LLM_BASE_URL/LLM_TOKEN missing)")
            return sm

        def _runtime_self_acms_id(identity: CallIdentity) -> str:
            if not identity.acms_agent_id:
                raise NotFoundError(
                    "agent token lacks an ACMS agent binding; runtime.self unavailable")
            return identity.acms_agent_id

        def _register_llm_resource(name, uri, resolver, description, *,
                                   roles=None, scopes=None):
            cap = Capability(
                name=name, domain="llm" if name.startswith("llm.") else "runtime",
                risk=RiskClass.READ,
                roles=roles or {"worker", "reviewer", "project_manager", "executive",
                                "infrastructure_admin", "observer"},
                scopes=scopes or {"llm.read"},
                description=description, kind="resource", uri_template=uri,
            )

            from mcp.server.fastmcp.resources.templates import FunctionResource

            if _TEMPLATE_PARAM_SEARCH.search(uri) is None:
                async def static_fn():
                    identity = CURRENT_IDENTITY.get()
                    entry = AuditEntry(
                        agent_name=identity.agent_name if identity else "(none)",
                        token_id=identity.token_id if identity else "",
                        token_kind=identity.token_kind.value if identity else "agent",
                        domain=cap.domain, name=cap.name, entry_kind="resource",
                        risk_class=cap.risk, status="ok",
                    )
                    start = time.monotonic()
                    try:
                        if identity is None:
                            raise UnauthenticatedError("no identity bound to this request")
                        cap.authorize(identity)
                        result = resolver(identity)
                        entry.duration_ms = int((time.monotonic() - start) * 1000)
                        self.audit.record(entry)
                        return result
                    except GatewayError as exc:
                        entry.duration_ms = int((time.monotonic() - start) * 1000)
                        entry.status = "denied" if exc.error_type in {
                            GatewayErrorType.ROLE_REQUIRED, GatewayErrorType.SCOPE_REQUIRED,
                            GatewayErrorType.OUT_OF_SCOPE, GatewayErrorType.DESTRUCTIVE_DENY,
                            GatewayErrorType.APPROVAL_REQUIRED} else "error"
                        entry.error_type = exc.error_type.value
                        entry.detail = exc.detail
                        self.audit.record(entry)
                        exc.args = (f"{exc.error_type.value}: {exc.detail}",)
                        raise

                resource = FunctionResource.from_function(
                    static_fn, uri=uri, name=dotted(name), description=description,
                    mime_type="application/json")
                self.mcp._resource_manager.add_resource(resource)
            else:
                # Parametrized: URI params flow into resolver(identity, uri_params)
                async def tpl_fn(**kwargs):
                    identity = CURRENT_IDENTITY.get()
                    entry = AuditEntry(
                        agent_name=identity.agent_name if identity else "(none)",
                        token_id=identity.token_id if identity else "",
                        token_kind=identity.token_kind.value if identity else "agent",
                        domain=cap.domain, name=cap.name, entry_kind="resource",
                        risk_class=cap.risk, status="ok",
                        args_hash=hash_args(kwargs),
                    )
                    start = time.monotonic()
                    try:
                        if identity is None:
                            raise UnauthenticatedError("no identity bound to this request")
                        cap.authorize(identity)
                        result = resolver(identity, kwargs)
                        entry.duration_ms = int((time.monotonic() - start) * 1000)
                        self.audit.record(entry)
                        return result
                    except GatewayError as exc:
                        entry.duration_ms = int((time.monotonic() - start) * 1000)
                        entry.status = "denied" if exc.error_type in {
                            GatewayErrorType.ROLE_REQUIRED, GatewayErrorType.SCOPE_REQUIRED,
                            GatewayErrorType.OUT_OF_SCOPE, GatewayErrorType.DESTRUCTIVE_DENY,
                            GatewayErrorType.APPROVAL_REQUIRED} else "error"
                        entry.error_type = exc.error_type.value
                        entry.detail = exc.detail
                        self.audit.record(entry)
                        exc.args = (f"{exc.error_type.value}: {exc.detail}",)
                        raise

                self.mcp._resource_manager.add_template(
                    tpl_fn, uri_template=uri, name=dotted(name),
                    description=description, mime_type="application/json")
            self.registry.register(cap)

        # ---- resources (READ; all need the SM client; scopes: llm.read) ----

        def r_models_list(identity):
            routes = _require_llm().model_routes()
            from .llm_adapter import _prune_route
            return {"models": [_prune_route(n, b) for n, b in sorted(routes.items())]}

        def r_model_get(identity, uri_params):
            from .llm_adapter import _prune_route
            model = uri_params.get("model", "")
            routes = _require_llm().model_routes()
            if model not in routes:
                raise NotFoundError(f"unknown model route: {model}")
            return _prune_route(model, routes[model])

        def r_model_health(identity):
            routes = _require_llm().model_routes()
            return {"models": {
                n: [{"health": b.get("health"), "routable": b.get("routable")}
                    for b in bs] for n, bs in sorted(routes.items())}}

        def r_loaded_models(identity):
            active = _require_llm().active_hosts()
            return {"loaded": active}

        def r_host_capacity(identity):
            hosts = _require_llm().hosts()
            from .llm_adapter import _prune_host
            return {"hosts": [_prune_host(h) for h in hosts]}

        def r_route_status(identity, uri_params):
            from .llm_adapter import _prune_route
            ref = uri_params.get("model", "")
            routes = _require_llm().model_routes()
            target = ref if ref in routes else None
            if target is None:
                # alias resolution: find a route whose alias_target matches
                aliases = {n: [b.get("alias_target") for b in bs
                               if b.get("is_alias")] for n, bs in routes.items()}
                target = next((n for n, tg in aliases.items() if ref in tg), None)
            if target is None:
                raise NotFoundError(f"unknown model/alias: {ref}")
            return _prune_route(target, routes[target])

        def r_usage(identity):
            body = _require_llm().usage(24)
            models = body.get("models", [])[:25]
            return {"window_hours": body.get("window_hours", 24), "models": models}

        def r_runtime_self(identity):
            agent_id = _runtime_self_acms_id(identity)
            status, body = acms.request(
                "GET", f"/api/v1/fleet/agents/{agent_id}/runtime")
            if status != 200:
                raise DomainUnavailableError(f"ACMS fleet runtime failed (HTTP {status})")
            return {"runtime": body}

        def r_runtime_fleet(identity):
            if not identity.has_role("executive", "infrastructure_admin"):
                raise OutOfScopeError("runtime.fleet requires executive/infrastructure_admin")
            runtimes = _require_llm().agent_runtimes()
            from .llm_adapter import _prune_runtime
            return {"runtimes": [_prune_runtime(rt) for rt in runtimes], "count": len(runtimes)}

        _register_llm_resource("llm.models.list", "llm://models", r_models_list,
                               "Routable model catalog with per-backend health/context (SM authority).")
        _register_llm_resource("llm.model.get", "llm://models/{model}", r_model_get,
                               "One model route's backends (health, context_limit, alias info).")
        _register_llm_resource("llm.model_health", "llm://model-health", r_model_health,
                               "Health/routability summary for every known model.")
        _register_llm_resource("llm.loaded_models", "llm://loaded-models", r_loaded_models,
                               "Currently active model-serving hosts (active_hosts).")
        _register_llm_resource("llm.host_capacity", "llm://host-capacity", r_host_capacity,
                               "Physical host inventory: GPUs, node, desired power/service state.")
        _register_llm_resource("llm.route_status", "llm://route-status/{model}", r_route_status,
                               "Resolve a model name or alias to its concrete route.")
        _register_llm_resource("llm.usage", "llm://usage", r_usage,
                               "Token usage aggregate (last 24h, LiteLLM SpendLogs via SM).")
        _register_llm_resource("runtime.self", "runtime://self", r_runtime_self,
                               "The calling agent's own runtime/VM/bridge state (ACMS fleet view).")
        _register_llm_resource("runtime.fleet", "runtime://fleet", r_runtime_fleet,
                               "Broad runtime fleet view (executive/infrastructure_admin only).")

        # ---- tools ----

        def _own_work_item_id(identity: CallIdentity) -> tuple[str, str]:
            """(work_item_id, work_uid) of the caller's own assignment."""
            if identity.is_assignment_scoped and identity.work_uid:
                work = acms.find_work_item(identity.work_uid)
                if work is None:
                    raise NotFoundError(f"work item not found: {identity.work_uid}")
                return work.get("work_item_id", ""), work.get("work_uid", "")
            if not identity.acms_agent_id:
                raise OutOfScopeError("agent token lacks ACMS agent binding")
            assignment = acms.active_assignment(identity.acms_agent_id)
            if assignment is None:
                raise OutOfScopeError("no ACTIVE assignment; model policy requires one")
            work = acms.find_work_item(assignment.get("work_item_id", ""))
            if work is None:
                raise NotFoundError("linked work item missing")
            return work.get("work_item_id", ""), work.get("work_uid", "")

        async def request_model(identity, ctx, model: str, reason: str = ""):
            model = (model or "").strip()
            if not model:
                raise ValidationError_("model is required")
            # validate against the SM catalog BEFORE any ACMS write (fail fast)
            routes = _require_llm().model_routes()
            if model not in routes:
                known = ", ".join(sorted(routes)[:20])
                raise ValidationError_(f"unknown model {model!r}; known: {known}")
            work_item_id, work_uid = _own_work_item_id(identity)
            status, body = acms.request(
                "PUT", "/model-policy",
                payload={"scope": "work_item", "scope_id": work_item_id,
                         "inference_policy": "local-preferred", "preferred_model": model,
                         "cloud_fallback": False, "updated_by": identity.agent_name},
            )
            if status != 200:
                raise DomainUnavailableError(f"ACMS model-policy update failed (HTTP {status})")
            return {"work_uid": work_uid, "preferred_model": model, "scope": "work_item",
                    "note": "policy takes effect at next dispatch/run (ADR-0015)"}

        async def request_fallback(identity, ctx, reason: str = ""):
            work_item_id, work_uid = _own_work_item_id(identity)
            if not reason:
                raise ValidationError_("reason is required for cloud fallback requests")
            status, body = acms.request(
                "PUT", "/model-policy",
                payload={"scope": "work_item", "scope_id": work_item_id,
                         "inference_policy": "any", "preferred_model": None,
                         "cloud_fallback": True, "updated_by": identity.agent_name},
            )
            if status != 200:
                raise DomainUnavailableError(f"ACMS model-policy update failed (HTTP {status})")
            return {"work_uid": work_uid, "inference_policy": "any",
                    "cloud_fallback": True,
                    "note": "bounded by ACMS cloud_fallback_max_usd (ADR-0015)"}

        async def restart_self(identity, ctx, reason: str = ""):
            if not reason:
                raise ValidationError_("reason is required for a self restart")
            agent_id = _runtime_self_acms_id(identity)
            status, body = acms.request(
                "POST", f"/api/v1/fleet/agents/{agent_id}/runtime/restart")
            if status not in (200, 202, 303):
                raise DomainUnavailableError(f"ACMS runtime restart failed (HTTP {status})")
            return {"restarted": "self", "agent_id": agent_id,
                    "note": "runtime restart via ACMS fleet API (Server Manager authority)"}

        async def request_elevated(identity, ctx, capability: str, reason: str):
            """Record a durable SENSITIVE_WRITE approval request (plan §16/§36)."""
            if not capability:
                raise ValidationError_("capability is required")
            args_hash = hash_args({"capability": capability, "reason": reason})
            out = approvals.create(agent_name=identity.agent_name,
                                   capability=capability, reason=reason,
                                   args_hash=args_hash)
            return {"approval_request_id": out["request_id"], "status": out["status"],
                    "note": "executive decision required (CLI 'approval decide' or "
                            "ACMS internal API); the grant is ONE-TIME and TTL-bounded"}

        def _llm_tool(name, desc, schema, fn, risk, *, roles=None):
            wrapped = _bind_args(fn)
            cap = Capability(
                name=name, domain="llm" if name.startswith("llm.") else "runtime",
                risk=risk, roles=roles if roles is not None else {"worker"},
                scopes={"llm.write"} if name.startswith("llm.") else {"runtime.write"},
                description=desc, timeout_seconds=15, side_effect="external",
                meta={"input_schema": schema},
            )
            cap.handler = wrapped
            self._register_tool(cap)

        _llm_tool("llm.request_model",
                  "Request a preferred model for YOUR current work item (validated against the live catalog).",
                  {"type": "object",
                   "properties": {"model": {"type": "string", "minLength": 1},
                                  "reason": {"type": "string"}},
                   "required": ["model"]},
                  request_model, RiskClass.SAFE_WRITE)
        _llm_tool("llm.request_fallback",
                  "Request cloud fallback for YOUR current work item (reason required; bounded by ACMS budget caps).",
                  {"type": "object",
                   "properties": {"reason": {"type": "string", "minLength": 1}},
                   "required": ["reason"]},
                  request_fallback, RiskClass.SAFE_WRITE)
        # runtime.restart_self: SENSITIVE_WRITE — executives pass directly;
        # workers get a durable approval-request path (one-time, TTL-bounded grant).
        _llm_tool("runtime.restart_self",
                  "Restart YOUR OWN runtime (SENSITIVE_WRITE; reason required; workers need executive approval).",
                  {"type": "object",
                   "properties": {"reason": {"type": "string", "minLength": 1}},
                   "required": ["reason"]},
                  restart_self, RiskClass.SENSITIVE_WRITE, roles={"worker"})
        _llm_tool("runtime.request_elevated",
                  "Request executive approval for a sensitive capability (durable request; one-time TTL-bounded grant).",
                  {"type": "object",
                   "properties": {"capability": {"type": "string", "minLength": 1},
                                  "reason": {"type": "string", "minLength": 1}},
                   "required": ["capability", "reason"]},
                  request_elevated, RiskClass.SAFE_WRITE)


    # ------------- GitHub capability construction (plan §25) -------------

    def _build_github_capabilities(self) -> None:
        gh = self.github
        acms = self.acms
        if gh is None:
            return  # github domain absent when unconfigured (health omits it)

        def _require_github():
            if gh is None:
                raise DomainUnavailableError(
                    "github domain not configured (MCP_GATEWAY_GITHUB_TOKEN missing)")
            return gh

        # ---- scope/ownership helpers (§25 policy + §31 security tests) ----

        def _repo_in_scope(identity: CallIdentity, repo: str) -> None:
            repo = (repo or "").strip().strip("/")
            if not repo or repo.count("/") != 1:
                raise ValidationError_('repo must be "owner/name"')
            if identity.token_kind == TokenKind.ASSIGNMENT:
                allowed = [r.lower() for r in (identity.repositories or [])]
            else:
                allowed = [r.lower() for r in gh.repos_allowlist] if gh else []
            if repo.lower() not in allowed:
                raise OutOfScopeError(
                    f"repository not in assignment scope: {repo}")

        def _caller_work_uid(identity: CallIdentity) -> str:
            """The work_uid this identity may write code against."""
            if identity.is_assignment_scoped and identity.work_uid:
                return identity.work_uid
            if identity.acms_agent_id:
                assignment = acms.active_assignment(identity.acms_agent_id)
                if assignment:
                    work = acms.find_work_item(assignment.get("work_item_id", ""))
                    if work and work.get("work_uid"):
                        return work.get("work_uid", "")
            raise OutOfScopeError(
                "no assignment-bound work item; github writes require one")

        def _own_branch(identity: CallIdentity) -> str:
            return owned_branch(_caller_work_uid(identity), identity.agent_name)

        def _assert_own_branch(identity: CallIdentity, branch: str) -> None:
            expected = _own_branch(identity)
            if (branch or "") != expected:
                owner = branch_owner(branch) or ("", "")
                raise OutOfScopeError(
                    f"branch not owned by this agent/work: {branch!r} "
                    f"(expected {expected!r}; branch prefix "
                    f"{owner[0] or 'unknown'})")

        # ---- resources (READ) ----

        def _register_github_resource(name, uri, resolver, description, *,
                                      scopes=None):
            cap = Capability(
                name=name, domain="github", risk=RiskClass.READ,
                roles={"worker", "reviewer", "project_manager", "executive",
                       "infrastructure_admin", "observer"},
                scopes=scopes or {"github.read"},
                description=description, kind="resource", uri_template=uri,
            )

            from mcp.server.fastmcp.resources.templates import FunctionResource

            if _TEMPLATE_PARAM_SEARCH.search(uri) is None:
                def _static_resolver(identity):
                    return resolver(identity)

                cap.handler = lambda ident, ctx=None, _r=_static_resolver: _async_result(_r(ident))

                async def _static():
                    identity = CURRENT_IDENTITY.get()
                    entry = AuditEntry(
                        agent_name=identity.agent_name if identity else "(none)",
                        token_id=identity.token_id if identity else "",
                        token_kind=identity.token_kind.value if identity else "agent",
                        work_uid=getattr(identity, "work_uid", None),
                        domain=cap.domain, name=cap.name, entry_kind="resource",
                        risk_class=cap.risk, status="ok",
                    )
                    start = time.monotonic()
                    try:
                        if identity is None:
                            raise UnauthenticatedError("no identity bound")
                        cap.authorize(identity)
                        result = resolver(identity)
                        entry.duration_ms = int((time.monotonic() - start) * 1000)
                        self.audit.record(entry)
                        return result
                    except GatewayError as exc:
                        entry.duration_ms = int((time.monotonic() - start) * 1000)
                        entry.status = "denied" if exc.error_type in {
                            GatewayErrorType.ROLE_REQUIRED,
                            GatewayErrorType.SCOPE_REQUIRED,
                            GatewayErrorType.OUT_OF_SCOPE,
                            GatewayErrorType.DESTRUCTIVE_DENY,
                            GatewayErrorType.APPROVAL_REQUIRED} else "error"
                        entry.error_type = exc.error_type.value
                        entry.detail = exc.detail
                        self.audit.record(entry)
                        exc.args = (f"{exc.error_type.value}: {exc.detail}",)
                        raise
                self.mcp._resource_manager.add_resource(
                    FunctionResource.from_function(_static, uri=uri, name=dotted(name),
                                                   description=description,
                                                   mime_type="application/json"))
            else:
                async def tpl_fn(**kwargs):
                    identity = CURRENT_IDENTITY.get()
                    entry = AuditEntry(
                        agent_name=identity.agent_name if identity else "(none)",
                        token_id=identity.token_id if identity else "",
                        token_kind=identity.token_kind.value if identity else "agent",
                        work_uid=getattr(identity, "work_uid", None),
                        domain=cap.domain, name=cap.name, entry_kind="resource",
                        risk_class=cap.risk, status="ok",
                        args_hash=hash_args(kwargs),
                    )
                    start = time.monotonic()
                    try:
                        if identity is None:
                            raise UnauthenticatedError("no identity bound")
                        cap.authorize(identity)
                        result = resolver(identity, kwargs)
                        entry.duration_ms = int((time.monotonic() - start) * 1000)
                        self.audit.record(entry)
                        return result
                    except GatewayError as exc:
                        entry.duration_ms = int((time.monotonic() - start) * 1000)
                        entry.status = "denied" if exc.error_type in {
                            GatewayErrorType.ROLE_REQUIRED,
                            GatewayErrorType.SCOPE_REQUIRED,
                            GatewayErrorType.OUT_OF_SCOPE,
                            GatewayErrorType.DESTRUCTIVE_DENY,
                            GatewayErrorType.APPROVAL_REQUIRED} else "error"
                        entry.error_type = exc.error_type.value
                        entry.detail = exc.detail
                        self.audit.record(entry)
                        exc.args = (f"{exc.error_type.value}: {exc.detail}",)
                        raise
                self.mcp._resource_manager.add_template(
                    tpl_fn, uri_template=uri, name=dotted(name),
                    description=description, mime_type="application/json")

            # direct-call handle for tests/registry consumers:
            # handler(identity, uri_params) with the same authorize+audit path
            async def _direct(identity, params=None, _resolver=resolver,
                              _cap=cap):
                entry = AuditEntry(
                    agent_name=identity.agent_name if identity else "(none)",
                    token_id=identity.token_id if identity else "",
                    token_kind=identity.token_kind.value if identity else "agent",
                    work_uid=getattr(identity, "work_uid", None),
                    domain=_cap.domain, name=_cap.name, entry_kind="resource",
                    risk_class=_cap.risk, status="ok",
                    args_hash=hash_args(params or {}),
                )
                start = time.monotonic()
                try:
                    if identity is None:
                        raise UnauthenticatedError("no identity bound")
                    _cap.authorize(identity)
                    result = _resolver(identity, params or {})
                    entry.duration_ms = int((time.monotonic() - start) * 1000)
                    self.audit.record(entry)
                    return result
                except GatewayError as exc:
                    entry.duration_ms = int((time.monotonic() - start) * 1000)
                    entry.status = "denied" if exc.error_type in {
                        GatewayErrorType.ROLE_REQUIRED,
                        GatewayErrorType.SCOPE_REQUIRED,
                        GatewayErrorType.OUT_OF_SCOPE,
                        GatewayErrorType.DESTRUCTIVE_DENY,
                        GatewayErrorType.APPROVAL_REQUIRED} else "error"
                    entry.error_type = exc.error_type.value
                    entry.detail = exc.detail
                    self.audit.record(entry)
                    exc.args = (f"{exc.error_type.value}: {exc.detail}",)
                    raise
            cap.handler = _direct
            self.registry.register(cap)

        def r_repo(identity, uri_params):
            repo = uri_params.get("repo", "")
            _repo_in_scope(identity, repo)
            return _require_github().get_repo(repo)

        def r_branch(identity, uri_params):
            repo = uri_params.get("repo", "")
            branch = uri_params.get("branch", "")
            _repo_in_scope(identity, repo)
            return _require_github().get_branch(repo, branch)

        def r_commit(identity, uri_params):
            repo = uri_params.get("repo", "")
            ref = uri_params.get("ref", "")
            _repo_in_scope(identity, repo)
            return _require_github().get_commit(repo, ref)

        def r_pr(identity, uri_params):
            repo = uri_params.get("repo", "")
            num_raw = uri_params.get("number", "")
            try:
                number = int(num_raw)
            except ValueError:
                raise ValidationError_("PR number must be an integer")
            _repo_in_scope(identity, repo)
            return _require_github().get_pr(repo, number)

        def r_checks(identity, uri_params):
            repo = uri_params.get("repo", "")
            ref = uri_params.get("ref", "")
            _repo_in_scope(identity, repo)
            return _require_github().get_checks(repo, ref)

        def r_issue(identity, uri_params):
            repo = uri_params.get("repo", "")
            num_raw = uri_params.get("number", "")
            try:
                number = int(num_raw)
            except ValueError:
                raise ValidationError_("issue number must be an integer")
            _repo_in_scope(identity, repo)
            return _require_github().get_issue(repo, number)

        _register_github_resource(
            "github.repo.get", "github://repos/{repo}", r_repo,
            "Repository metadata (default branch, visibility, caller permissions).")
        _register_github_resource(
            "github.branch.get", "github://repos/{repo}/branches/{branch}", r_branch,
            "One branch: head sha + protected flag.")
        _register_github_resource(
            "github.commit.get", "github://repos/{repo}/commits/{ref}", r_commit,
            "One commit: message, author, files touched.")
        _register_github_resource(
            "github.pr.get", "github://repos/{repo}/pulls/{number}", r_pr,
            "One pull request: state, head/base, mergeability.")
        _register_github_resource(
            "github.checks.get", "github://repos/{repo}/checks/{ref}", r_checks,
            "Check runs for a ref (commit sha or branch name).")
        _register_github_resource(
            "github.issue.get", "github://repos/{repo}/issues/{number}", r_issue,
            "One issue: state, title, labels (is_pr flag when it is a PR).")

        # ---- tools (SAFE_WRITE; branch-ownership enforced) ----

        async def branch_create(identity, ctx, repo: str, base: str = ""):
            _repo_in_scope(identity, repo)
            g = _require_github()
            branch = _own_branch(identity)
            base_ref = (base or "").strip() or g.get_repo(repo)["default_branch"]
            # base may not be inside another agent's branch namespace (§25:
            # no two agents share a writable branch; also blocks chain-branching)
            if (base_ref or "").startswith("agent/"):
                _assert_own_branch(identity, base_ref)
            out = g.create_branch(repo, branch, base_ref)
            return {**out, "note": "branch owned by this agent/work; foreign "
                                   "agents cannot write it through the gateway"}

        _STAGED_BLOBS: dict[str, dict[str, dict[str, str]]] = {}
        # key: (agent_name, work_uid, repo) -> {path: {blob_sha, content_b64}}
        # In-process staging area; gateway is a single-process service (systemd
        # unit). Staged blobs are durable on GitHub ONLY after commit.create.

        def _staging_key(identity: CallIdentity, repo: str) -> str:
            return f"{identity.agent_name}|{_caller_work_uid(identity)}|{repo}"

        async def file_write(identity, ctx, repo: str, path: str, content: str,
                             encoding: str = "utf-8"):
            _repo_in_scope(identity, repo)
            if not path or path.startswith("/") or ".." in path.split("/"):
                raise ValidationError_("path must be a repo-relative path without traversal")
            branch = _own_branch(identity)
            # the target branch must EXIST and be the caller's own (prevents
            # staging against a branch the agent cannot push)
            g = _require_github()
            try:
                g.get_branch(repo, branch)
            except NotFoundError:
                raise ValidationError_(
                    f"branch {branch!r} does not exist yet; run github.branch.create first")
            blob_sha = g.create_blob(repo, content, encoding=encoding)
            key = _staging_key(identity, repo)
            _STAGED_BLOBS.setdefault(key, {})[path] = {
                "blob_sha": blob_sha, "encoding": encoding}
            return {"path": path, "blob_sha": blob_sha, "branch": branch,
                    "staged": True,
                    "note": "staged in gateway memory; durable only after "
                            "github.commit.create"}

        async def commit_create(identity, ctx, repo: str, message: str,
                                paths: list[str] | None = None,
                                author_name: str = "", author_email: str = ""):
            _repo_in_scope(identity, repo)
            if not (message or "").strip():
                raise ValidationError_("commit message is required")
            key = _staging_key(identity, repo)
            staged = dict(_STAGED_BLOBS.get(key, {}))
            if paths:
                wanted = set(paths)
                staged = {p: v for p, v in staged.items() if p in wanted}
                if not staged:
                    raise ValidationError_("no staged files match paths")
            g = _require_github()
            branch = _own_branch(identity)
            parent_sha = g.get_commit_sha(repo, branch)
            staged_list = [{"path": p, "blob_sha": v["blob_sha"]}
                           for p, v in sorted(staged.items())]
            tree_sha, _base_tree = g.build_tree_from_base(
                repo, parent_sha, staged_list)
            name = (author_name or "").strip() or identity.agent_name
            email = (author_email or "").strip() or                 f"{identity.agent_name}@agents.startupteams.co"
            out = g.create_commit(repo, branch, message, tree_sha, parent_sha,
                                  name, email)
            if not paths:
                _STAGED_BLOBS.pop(key, None)  # full commit consumes staging
            else:
                for p in staged:
                    _STAGED_BLOBS.get(key, {}).pop(p, None)
            return {**out, "files": sorted(staged.keys())}

        async def pr_create(identity, ctx, repo: str, title: str, body: str = "",
                            base: str = "", draft: bool = False):
            _repo_in_scope(identity, repo)
            if not (title or "").strip():
                raise ValidationError_("PR title is required")
            g = _require_github()
            head = _own_branch(identity)
            base_ref = (base or "").strip() or g.get_repo(repo)["default_branch"]
            if base_ref == head:
                raise ValidationError_("head and base are the same branch")
            if base_ref.startswith("agent/"):
                _assert_own_branch(identity, base_ref)
            out = g.create_pr(repo, head, base_ref, title, body, draft=draft)
            return {**out, "head": head, "base": base_ref}

        async def pr_comment(identity, ctx, repo: str, number: int, body: str):
            _repo_in_scope(identity, repo)
            if not (body or "").strip():
                raise ValidationError_("comment body is required")
            try:
                number = int(number)
            except (TypeError, ValueError):
                raise ValidationError_("PR number must be an integer")
            return _require_github().add_pr_comment(repo, number, body)

        async def review_comment(identity, ctx, repo: str, number: int,
                                 commit_sha: str, path: str, body: str,
                                 line: int | None = None):
            _repo_in_scope(identity, repo)
            if not (body or "").strip():
                raise ValidationError_("comment body is required")
            if not (commit_sha or "").strip():
                raise ValidationError_("commit_sha is required (the commit the comment anchors to)")
            if not (path or "").strip():
                raise ValidationError_("path is required")
            try:
                number = int(number)
            except (TypeError, ValueError):
                raise ValidationError_("PR number must be an integer")
            # COMMENT-only reviews: the gateway never APPROVES or REQUESTS
            # CHANGES — review verdicts stay human (plan §25).
            return _require_github().add_review_comment(
                repo, number, body, commit_sha, path, line=line)

        def _gh_tool(name, desc, schema, fn):
            wrapped = _bind_args(fn)
            cap = Capability(
                name=name, domain="github", risk=RiskClass.SAFE_WRITE,
                roles={"worker"},
                scopes={"github.write"},
                description=desc, timeout_seconds=30, side_effect="external",
                meta={"input_schema": schema},
            )
            cap.handler = wrapped
            self._register_tool(cap)

        _gh_tool("github.branch.create",
                 "Create YOUR OWN agent branch (agent/<work_uid>-<agent>) in a scoped repo.",
                 {"type": "object",
                  "properties": {"repo": {"type": "string", "minLength": 3},
                                 "base": {"type": "string"}},
                  "required": ["repo"]},
                 branch_create)
        _gh_tool("github.file.write",
                 "Stage a file write on YOUR OWN agent branch (durable only after github.commit.create).",
                 {"type": "object",
                  "properties": {"repo": {"type": "string", "minLength": 3},
                                 "path": {"type": "string", "minLength": 1},
                                 "content": {"type": "string"},
                                 "encoding": {"type": "string"}},
                  "required": ["repo", "path", "content"]},
                 file_write)
        _gh_tool("github.commit.create",
                 "Commit staged file writes to YOUR OWN agent branch (fast-forward only; never force-push).",
                 {"type": "object",
                  "properties": {"repo": {"type": "string", "minLength": 3},
                                 "message": {"type": "string", "minLength": 1},
                                 "paths": {"type": "array",
                                           "items": {"type": "string"}},
                                 "author_name": {"type": "string"},
                                 "author_email": {"type": "string"}},
                  "required": ["repo", "message"]},
                 commit_create)
        _gh_tool("github.pr.create",
                 "Open a pull request from YOUR OWN agent branch to a base branch.",
                 {"type": "object",
                  "properties": {"repo": {"type": "string", "minLength": 3},
                                 "title": {"type": "string", "minLength": 1},
                                 "body": {"type": "string"},
                                 "base": {"type": "string"},
                                 "draft": {"type": "boolean"}},
                  "required": ["repo", "title"]},
                 pr_create)
        _gh_tool("github.pr.comment",
                 "Comment on a pull request in a scoped repository.",
                 {"type": "object",
                  "properties": {"repo": {"type": "string", "minLength": 3},
                                 "number": {"type": "integer", "minimum": 1},
                                 "body": {"type": "string", "minLength": 1}},
                  "required": ["repo", "number", "body"]},
                 pr_comment)
        _gh_tool("github.review.comment",
                 "Add a line-anchored review COMMENT on a PR (COMMENT-only; approval verdicts stay human).",
                 {"type": "object",
                  "properties": {"repo": {"type": "string", "minLength": 3},
                                 "number": {"type": "integer", "minimum": 1},
                                 "commit_sha": {"type": "string", "minLength": 1},
                                 "path": {"type": "string", "minLength": 1},
                                 "body": {"type": "string", "minLength": 1},
                                 "line": {"type": "integer", "minimum": 1}},
                  "required": ["repo", "number", "commit_sha", "path", "body"]},
                 review_comment)

# ---------------- argument plumbing helpers ----------------

def _async_result(value):
    """Wrap a sync resolver result into an awaitable for resource handlers."""
    import asyncio

    async def _go():
        return value
    return _go()


ARGS_HOLDER: contextvars.ContextVar[dict[str, Any]] = contextvars.ContextVar(
    "gateway_tool_args", default={}
)


def _bind_args(fn):
    """Adapt a handler(identity, ctx, **args) into handler(identity, ctx) that
    reads its arguments from ARGS_HOLDER (set by the custom Tool.run)."""
    import inspect

    sig = inspect.signature(fn)

    async def bound(identity, ctx):
        args = dict(ARGS_HOLDER.get())
        kwargs = {k: v for k, v in args.items() if k in sig.parameters}
        return await fn(identity, ctx, **kwargs)

    return bound


def _running_task(acms: AcmsClient, work: dict) -> dict | None:
    status, body = acms.request(
        "GET", "/api/v1/work/tasks",
        query={"work_item_id": work.get("work_item_id", "")},
    )
    if status != 200 or not isinstance(body, list):
        return None
    for t in body:
        if t.get("status") == "RUNNING":
            return t
    return None


# ---------------- response pruners (honest minimal projections) ----------------

def _prune_work(w: dict | None) -> dict | None:
    if not w:
        return None
    keys = ["work_item_id", "work_key", "work_uid", "parent_id", "kind", "title",
            "status", "project_id", "jira_issue_key", "jira_last_status",
            "created_by", "scope_markdown", "created_at", "updated_at"]
    return {k: w.get(k) for k in keys if k in w}


def _prune_project(p: dict | None) -> dict | None:
    if not p:
        return None
    keys = ["project_id", "name", "slug", "product_id", "jira_project_key",
            "repos", "created_at"]
    return {k: p.get(k) for k in keys if k in p}


def _prune_product(p: dict | None) -> dict | None:
    if not p:
        return None
    keys = ["product_id", "name", "slug", "description", "created_at"]
    return {k: p.get(k) for k in keys if k in p}


def _prune_artifact(a: dict | None) -> dict:
    keys = ["artifact_id", "artifact_uid", "work_item_id", "agent_id", "project_id",
            "product_id", "jira_issue_key", "artifact_type", "title", "bluf",
            "mime_type", "sha256", "created_at", "created_by"]
    return {k: a.get(k) for k in keys if k in a}


def _prune_agent(a: dict | None) -> dict | None:
    if not a:
        return None
    keys = ["agent_id", "display_name", "legacy_name", "worker_uid", "trust_class",
            "harness", "bridge_version", "protocol_version", "created_at"]
    return {k: a.get(k) for k in keys if k in a}


def _prune_inbox(i: dict) -> dict:
    keys = ["item_id", "item_class", "severity", "title", "summary", "status",
            "agent_id", "work_item_id", "created_at"]
    return {k: i.get(k) for k in keys if k in i}
