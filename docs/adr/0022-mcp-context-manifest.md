# ADR-0022: MCP context manifest (pointers, not dumps)

**Status:** Proposed
**Date:** 2026-10-02

## Context

Plan §14 requires a per-assignment context manifest so agents fetch exactly the
context they need — "do not dump every document into the initial prompt."

## Decision

1. `mcp://acms/context/current` returns the manifest assembled from the
   identity + durable ACMS lookups (work, project, agent) with:
   agent (name/id/roles), assignment (work_uid/jira/project/product),
   repositories, `required_context` as POINTERS (`mcp://acms/work/{uid}`,
   `mcp://acms/project/{id}`, Jira description pointer), `optional_context`,
   `permissions`, and `approvals`.
2. Content is fetched through the corresponding resources on demand; the
   manifest never inlines full documents in W1.
3. W2 will extend `required_context` with ADR/handoff/deployment-state
   pointers once the GitHub/runtime adapters exist.

## Consequences

- Workers pay token cost only for context they fetch.
- The manifest shape is a stable contract (plan §38) — breaking changes need
  a version bump in the capability card.

## References

- Requirement: ACMS-REQ-064
- Plan: 2026-10-02 §14/§38
- Related: ADR-0019
