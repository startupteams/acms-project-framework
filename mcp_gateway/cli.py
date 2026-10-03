"""Operator CLI for the MIAM MCP gateway.

Usage:
  python -m mcp_gateway.cli serve                 # run the gateway (systemd ExecStart)
  python -m mcp_gateway.cli mint-agent <agent_name> [--acms-agent-id <uuid>] [--executive]
  python -m mcp_gateway.cli mint-assignment <agent_name> <work_uid> [--hours 24]       [--project-slug <slug>] [--jira <KEY>] [--repo owner/name ...]
  python -m mcp_gateway.cli list-tokens
  python -m mcp_gateway.cli revoke <token_id> <reason>
  python -m mcp_gateway.cli activity [--limit 20] [--denied]

SECURITY: raw tokens are printed EXACTLY ONCE to stdout, masked in later
listings. The mint output is the operator's only chance to capture the token;
it is intended to be piped into the target agent's credential file (0600) —
never into logs, handoffs, or chat messages.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone

from . import __version__
from .audit import AuditLog
from .models import utcnow
from .server import GatewayConfig, GatewayServer
from .tokens import TokenStore


def _config_from_env() -> GatewayConfig:
    return GatewayConfig(
        host=os.environ.get("MCP_GATEWAY_HOST", "127.0.0.1"),
        port=int(os.environ.get("MCP_GATEWAY_PORT", "8202")),
        acms_base_url=os.environ.get("ACMS_BASE_URL", ""),
        acms_token=os.environ.get("ACMS_SERVICE_TOKEN", ""),
        acms_tls_ca=os.environ.get("ACMS_TLS_CA") or None,
        acms_insecure_tls=os.environ.get("ACMS_INSECURE_TLS", "0") == "1",
        tokens_path=os.environ.get("MCP_GATEWAY_TOKENS_PATH",
                                   "/var/lib/miam-mcp-gateway/tokens.sqlite3"),
        audit_path=os.environ.get("MCP_GATEWAY_AUDIT_PATH",
                                  "/var/lib/miam-mcp-gateway/activity.sqlite3"),
        audit_mirror=os.environ.get("MCP_GATEWAY_AUDIT_MIRROR") or None,
        public_base_url=os.environ.get("MCP_GATEWAY_PUBLIC_URL", ""),
        executive_agent_names=os.environ.get("MCP_GATEWAY_EXECUTIVE_AGENTS", ""),
        allowed_hosts=os.environ.get("MCP_GATEWAY_ALLOWED_HOSTS", ""),
        llm_base_url=os.environ.get("MCP_GATEWAY_LLM_BASE_URL", ""),
        llm_token=os.environ.get("MCP_GATEWAY_LLM_TOKEN", ""),
        internal_token=os.environ.get("MCP_GATEWAY_INTERNAL_TOKEN", ""),
        approvals_path=os.environ.get("MCP_GATEWAY_APPROVALS_PATH",
                                      "/var/lib/miam-mcp-gateway/approvals.sqlite3"),
        github_token=os.environ.get("MCP_GATEWAY_GITHUB_TOKEN", ""),
        github_repos=os.environ.get("MCP_GATEWAY_GITHUB_REPOS", ""),
        # W6 (plan §28)
        jira_base_url=os.environ.get("MCP_GATEWAY_JIRA_BASE_URL", ""),
        jira_email=os.environ.get("MCP_GATEWAY_JIRA_EMAIL", ""),
        jira_api_token=os.environ.get("MCP_GATEWAY_JIRA_API_TOKEN", ""),
        jira_projects=os.environ.get("MCP_GATEWAY_JIRA_PROJECTS", ""),
        jira_status_mutation_enabled=(
            os.environ.get("MCP_GATEWAY_JIRA_STATUS_MUTATION_ENABLED", "0") == "1"),
        # W7 (plan §29)
        registry_base_url=os.environ.get("MCP_GATEWAY_REGISTRY_BASE_URL", ""),
        registry_token=os.environ.get("MCP_GATEWAY_REGISTRY_TOKEN", ""),
        kuma_url=os.environ.get("MCP_GATEWAY_KUMA_URL", ""),
        kuma_user=os.environ.get("MCP_GATEWAY_KUMA_USER", ""),
        kuma_pass=os.environ.get("MCP_GATEWAY_KUMA_PASS", ""),
        # P2 (plan P2)
        dkms_base_url=os.environ.get("MCP_GATEWAY_DKMS_BASE_URL", ""),
        dkms_token=os.environ.get("MCP_GATEWAY_DKMS_TOKEN", ""),
    )


def _mask(raw: str) -> str:
    return raw[:6] + "…" + raw[-4:] if len(raw) > 12 else "…"


def cmd_serve(_args) -> int:
    cfg = _config_from_env()
    tokens = TokenStore(cfg.tokens_path)
    audit = AuditLog(cfg.audit_path,
                     mirror_jsonl=cfg.audit_mirror and __import__("pathlib").Path(cfg.audit_mirror))
    server = GatewayServer(cfg, tokens=tokens, audit=audit)
    import uvicorn

    uvicorn.run(server.app, host=cfg.host, port=cfg.port, log_level="info")
    return 0


def cmd_mint_agent(args) -> int:
    store = TokenStore(_config_from_env().tokens_path)
    roles = ["worker"]
    if args.executive:
        roles = ["executive", "infrastructure_admin"]
    # W5 live-found: pass scopes=None so the store's full-domain default scope
    # set applies (acms/llm/runtime/proxmox/power). Hardcoding acms-only here
    # made CLI-minted tokens SCOPE_REQUIRED on every non-acms domain — same
    # class as the W2 gap (PR #80). Scope widening is metadata; risk classes +
    # roles still gate every capability.
    rec, raw = store.mint_agent_token(
        args.agent_name, acms_agent_id=args.acms_agent_id or None,
        roles=roles, scopes=None,
        ttl_days=args.ttl_days,
    )
    print(json.dumps({
        "token_id": rec.token_id,
        "agent_name": rec.agent_name,
        "roles": rec.roles,
        "scopes": rec.scopes,
        "expires_at": rec.expires_at.isoformat() if rec.expires_at else None,
        # raw appears here ONCE — capture immediately, store 0600 on the agent
        "token": raw,
    }, indent=2))
    print(f"# NOTE: raw token shown ONCE. Store at 0600 on the agent; never log it.",
          file=sys.stderr)
    return 0


def cmd_mint_assignment(args) -> int:
    cfg = _config_from_env()
    store = TokenStore(cfg.tokens_path)
    kwargs: dict = {}
    if args.project_slug:
        kwargs["project_slug"] = args.project_slug
    if args.jira:
        kwargs["jira_issue_key"] = args.jira
    if args.repo:
        kwargs["repositories"] = list(args.repo)
    rec, raw = store.mint_assignment_token(
        agent_name=args.agent_name, work_uid=args.work_uid,
        agent_token_raw=args.agent_token,
        agent_token_hash=args.agent_token_hash,
        ttl_hours=args.hours, **kwargs,
    )
    print(json.dumps({
        "token_id": rec.token_id,
        "agent_name": rec.agent_name,
        "work_uid": rec.work_uid,
        "expires_at": rec.expires_at.isoformat() if rec.expires_at else None,
        "token": raw,
    }, indent=2))
    print("# NOTE: raw token shown ONCE.", file=sys.stderr)
    return 0


def cmd_list_tokens(_args) -> int:
    store = TokenStore(_config_from_env().tokens_path)
    rows = []
    for t in store.list_agent_tokens():
        rows.append({
            "kind": "agent", "token_id": t.token_id, "agent_name": t.agent_name,
            "acms_agent_id": t.acms_agent_id, "roles": t.roles, "scopes": t.scopes,
            "created_at": t.created_at.isoformat(),
            "expires_at": t.expires_at.isoformat() if t.expires_at else None,
            "revoked": t.revoked_at is not None,
            "active": t.is_active(),
        })
    for t in store.list_assignment_tokens():
        rows.append({
            "kind": "assignment", "token_id": t.token_id, "agent_name": t.agent_name,
            "work_uid": t.work_uid, "project_slug": t.project_slug,
            "jira": t.jira_issue_key,
            "expires_at": t.expires_at.isoformat() if t.expires_at else None,
            "revoked": t.revoked_at is not None,
            "active": t.is_active(),
        })
    print(json.dumps(rows, indent=2))
    return 0


def cmd_revoke(args) -> int:
    cfg = _config_from_env()
    store = TokenStore(cfg.tokens_path)
    ok = store.revoke_agent_token(args.token_id, args.reason) or         store.revoke_assignment_token(args.token_id, args.reason)
    print(json.dumps({"token_id": args.token_id, "revoked": ok, "reason": args.reason}))
    return 0 if ok else 1


def cmd_grant_scopes(args) -> int:
    """W2 migration path: union scopes into every ACTIVE token of an agent
    (agent tokens AND assignment tokens — W3: mint-assignment defaults lack
    github.*)."""
    cfg = _config_from_env()
    store = TokenStore(cfg.tokens_path)
    scopes = [s.strip() for s in args.scopes.split(",") if s.strip()]
    n = store.grant_agent_scopes(args.agent_name, scopes)
    n += store.grant_assignment_scopes(args.agent_name, scopes)
    print(json.dumps({"agent_name": args.agent_name, "scopes": scopes, "tokens_updated": n}))
    return 0 if n else 1


def cmd_activity(args) -> int:
    cfg = _config_from_env()
    audit = AuditLog(cfg.audit_path)
    rows = audit.recent(limit=args.limit, denied_only=args.denied)
    print(json.dumps(rows, indent=2, default=str))
    return 0


def cmd_approval_list(args) -> int:
    cfg = _config_from_env()
    from .approvals import ApprovalStore

    store = ApprovalStore(cfg.approvals_path)
    rows = store.list(status=args.status, limit=args.limit)
    print(json.dumps(rows, indent=2, default=str))
    return 0


def cmd_approval_decide(args) -> int:
    cfg = _config_from_env()
    from .approvals import ApprovalStore

    store = ApprovalStore(cfg.approvals_path)
    out = store.decide(args.request_id, decision=args.decision.upper(),
                       decided_by=args.decided_by, note=args.note or "")
    if out is None:
        print(json.dumps({"request_id": args.request_id, "error": "not found"}))
        return 1
    print(json.dumps(out, indent=2, default=str))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="mcp_gateway", description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("serve", help="run the gateway (systemd ExecStart)")
    p.set_defaults(fn=cmd_serve)

    p = sub.add_parser("mint-agent", help="mint an agent-scoped token (raw shown once)")
    p.add_argument("agent_name")
    p.add_argument("--acms-agent-id", default=None)
    p.add_argument("--executive", action="store_true", help="elevated roles (STEA-004)")
    p.add_argument("--ttl-days", type=int, default=365)
    p.set_defaults(fn=cmd_mint_agent)

    p = sub.add_parser("mint-assignment", help="mint an assignment-scoped token (raw shown once)")
    p.add_argument("agent_name")
    p.add_argument("work_uid")
    p.add_argument("--agent-token", default=None, help="the agent's raw token (authenticates the mint)")
    p.add_argument("--agent-token-hash", default=None, help="operator path: the agent token's sha256 hash")
    p.add_argument("--hours", type=int, default=24)
    p.add_argument("--project-slug", default=None)
    p.add_argument("--jira", default=None)
    p.add_argument("--repo", action="append", default=None)
    p.set_defaults(fn=cmd_mint_assignment)

    p = sub.add_parser("list-tokens", help="list tokens (masked; no raw values)")
    p.set_defaults(fn=cmd_list_tokens)

    p = sub.add_parser("revoke", help="revoke a token by id")
    p.add_argument("token_id")
    p.add_argument("reason")
    p.set_defaults(fn=cmd_revoke)

    p = sub.add_parser("grant-scopes", help="union scopes into every ACTIVE token of an agent (W2)")
    p.add_argument("agent_name")
    p.add_argument("scopes", help="comma-separated scope names, e.g. llm.read,llm.write,runtime.write")
    p.set_defaults(fn=cmd_grant_scopes)

    p = sub.add_parser("activity", help="show recent MCP activity")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--denied", action="store_true")
    p.set_defaults(fn=cmd_activity)

    p = sub.add_parser("approval", help="approval-request operations (plan §16/§36)")
    approval_sub = p.add_subparsers(dest="approval_cmd", required=True)
    pl = approval_sub.add_parser("list", help="list approval requests")
    pl.add_argument("--status", default=None, choices=["PENDING", "APPROVED", "DENIED", "USED", "EXPIRED"])
    pl.add_argument("--limit", type=int, default=50)
    pl.set_defaults(fn=cmd_approval_list)
    pd = approval_sub.add_parser("decide", help="decide an approval request")
    pd.add_argument("request_id")
    pd.add_argument("decision", choices=["APPROVED", "DENIED"])
    pd.add_argument("--decided-by", default="operator")
    pd.add_argument("--note", default=None)
    pd.set_defaults(fn=cmd_approval_decide)

    args = parser.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
