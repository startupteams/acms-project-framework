# ADR-0002: Centralized governance with decentralized agent execution

**Status:** Accepted  
**Date:** 2026-09-25

## Context

ACMS must provide a central brain for assigning and governing work without becoming a synchronous bottleneck that approves every low-level action an agent performs.

## Decision

ACMS is authoritative for assignment, scope, budget, permissions, context access, approvals, and audit. Agents execute autonomously inside that approved envelope and do not require ACMS approval for every local tool call, shell command, test, retry, or background routine.

## Consequences

Agents remain autonomous and scalable. ACMS focuses on significant state/events and control boundaries rather than proxying execution. Strong audit/reconciliation mechanisms are still required.

## References

- ACMS-REQ-009
- ACMS-REQ-050
