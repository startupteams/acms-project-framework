# ADR-0003: Use A2A as the semantic protocol foundation through an ACMS Agent Bridge

**Status:** Accepted  
**Date:** 2026-09-25

## Context

ACMS must manage heterogeneous agent harnesses while benefiting from the evolving Agent2Agent standard and avoiding a proprietary protocol fork.

## Decision

Use A2A as the primary semantic foundation for agent communication. Each managed harness is exposed through a standardized ACMS Agent Bridge. ACMS-specific behavior is added through versioned extensions/profile metadata rather than by forking A2A. The MVP should implement the simplest practical A2A HTTP-based binding and SSE streaming supported by the chosen implementation.

## Consequences

Harness-specific logic is isolated behind adapters and future A2A updates can be adopted. ACMS still owns its internal work/context/audit data model independently of A2A wire details.

## References

- ACMS-REQ-003
- ACMS-REQ-004
- ACMS-REQ-031
- ACMS-REQ-032
