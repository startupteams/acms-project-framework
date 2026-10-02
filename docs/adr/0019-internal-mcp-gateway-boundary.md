# ADR-0019: Internal MCP Gateway boundary (additive, agent-facing)

**Status:** Proposed
**Date:** 2026-10-02

## Context

Internal agents (ACMS workers, the STEA-004 executive agent) currently reach each
domain service through its own API surface (ACMS REST, A2A bridge, Server/LLM
Manager REST, PDU Manager). The approved 2026-10-02 plan directs a single logical
MCP endpoint (`mcp.miam.home.arpa`) so agents discover and call capabilities
uniformly — without replacing REST/A2A/SSE and without creating one giant
root-access service.

## Decision

1. The gateway is a SEPARATE service (`miam-mcp-gateway`) on VM114, port 8202,
   logically and operationally independent of ACMS. W1 ships the gateway shell +
   the ACMS adapter only.
2. The gateway is code-co-located in the ACMS repository (`mcp_gateway/` package)
   for review/CI simplicity, but it is NOT mounted inside the ACMS FastAPI app
   and shares no runtime process with it. Deployment is its own systemd unit.
3. Authority: domain services keep their authority. The gateway's ACMS adapter
   calls ACMS REST (bearer service credential) — it never opens ACMS DB
   sessions and never re-implements domain logic.
4. Transport: official `mcp` SDK streamable-HTTP, stateless mode, JSON responses
   (verified: session-less `tools/call` works; SSE reserved for future need).
5. Fail-closed: unknown/expired/revoked token => 401; out-of-scope => typed
   denial; DESTRUCTIVE risk denied for ALL callers in W1; SENSITIVE_WRITE
   requires executive/infrastructure-admin roles; every call audited.
6. Existing REST/A2A/SSE remain the authorities for control assignment and live
   events; MCP is additive and independently fail-able (plan §30/§33).

## Consequences

- Agents get one discovery surface; each new domain adapter (W2+: llm, runtime,
  github, proxmox, pdu, power, jira, registry, monitoring) plugs into the same
  policy/audit spine.
- The gateway must never become the sole control path: REST/A2A/SSE keep
  working when it is down; agents report `MCP unavailable` rather than
  inferring resources do not exist.
- Tool-manager keys normalize dots to underscores (SDK constraint); canonical
  dotted names are preserved in tool `_meta.gateway_canonical_name` and the
  policy registry (plan §15 namespace discipline survives at the contract
  layer).

## References

- Requirement: ACMS-REQ-064
- Plan: 2026-10-02 STEA-004 Jira Workflow Recovery and Internal MCP Gateway (§9–§20)
- Related: ADR-0005 (internal/external separation), ADR-0002 (centralized governance)
