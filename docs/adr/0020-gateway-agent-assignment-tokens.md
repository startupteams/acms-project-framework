# ADR-0020: Gateway agent-token + assignment-token model

**Status:** Proposed
**Date:** 2026-10-02

## Context

MCP calls need an identity model that is (a) narrow and revocable per agent
(plan §12), (b) short-lived and work-bound per assignment (plan §13), and
(c) completely independent of ACMS schema so the gateway stays additive
(no ACMS migration in W1).

## Decision

1. Two token kinds, both opaque random bearer strings (`mcp_…` agent /
   `mcpt_…` assignment). Raw values are shown exactly once at mint time and
   stored ONLY as SHA-256 hashes in the gateway's own SQLite store
   (`/var/lib/miam-mcp-gateway/tokens.sqlite3`).
2. Agent tokens: roles (worker|reviewer|project_manager|executive|
   infrastructure_admin|observer) + scopes (`acms.read`, `acms.write` in W1),
   optional expiry (default 365d), revocation with reason, optional source-IP
   allowlist. Elevated names may also be granted executive via gateway config.
3. Assignment tokens: minted ONLY with proof of the agent's raw token (or its
   hash, operator path), bound to exactly one Work UID + optional project/
   Jira/repo bindings, default TTL 24h.
4. Identity resolution happens in an ASGI middleware on EVERY `/mcp` request;
   the resolved `CallIdentity` travels to handlers via a contextvar
   (verified: per-call isolation across interleaved identities).
5. W1 minting is operator-driven (`python -m mcp_gateway.cli mint-*`);
   ACMS-dispatch auto-minting arrives with the W2 provisioning integration.

## Consequences

- Compromised assignment tokens expire quickly and cannot read other work
  (enforced resource-side).
- No ACMS DB migration; the gateway store is self-contained and can be
  destroyed/rebuilt without touching ACMS.
- Raw tokens never transit logs or the audit trail (hash-only storage; the
  CLI prints the raw value once to stdout, masked in listings).

## References

- Requirement: ACMS-REQ-064
- Plan: 2026-10-02 §12/§13
- Related: ADR-0019
