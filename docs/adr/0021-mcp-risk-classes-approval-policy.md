# ADR-0021: MCP risk classes and approval policy

**Status:** Proposed
**Date:** 2026-10-02

## Context

The plan §16 defines four risk classes with distinct approval semantics. W1
must implement the enforcement spine before any high-risk domain adapter lands.

## Decision

1. Every capability declares `risk` ∈ READ | SAFE_WRITE | SENSITIVE_WRITE |
   DESTRUCTIVE, plus required roles/scopes, idempotency, timeout, side-effect
   class, and audit flag (plan §19 contract metadata).
2. READ: any authenticated caller with the matching scope. No approval.
3. SAFE_WRITE: worker role + `acms.write` scope, always audited, scoped to the
   caller's OWN assignment (or the assignment token's bound Work UID).
4. SENSITIVE_WRITE: executive/infrastructure_admin only in W1 (the approval
   workflow does not exist yet — fail closed with a typed
   APPROVAL_REQUIRED denial for workers; W2+ adds the Inbox approval loop).
5. DESTRUCTIVE: DENIED for every caller at the gateway in W1 (the audited
   emergency path for executives is a later window; the plan's stop condition
   "ordinary worker can power off PDU / delete arbitrary VM" is enforced by
   construction — such capabilities are not even registered in W1).
6. Every ok/denied/error call lands in the durable MCP activity log with
   status, typed error, duration, and hashed arguments.

## Consequences

- Adding a destructive capability later requires a deliberate registry change
  + ADR amendment, not just an adapter PR.
- Workers are never blocked mid-task by the gateway for READ/SAFE_WRITE;
  sensitive paths fail loudly instead of silently.

## References

- Requirement: ACMS-REQ-064
- Plan: 2026-10-02 §16/§19/§43
- Related: ADR-0019, ADR-0020
