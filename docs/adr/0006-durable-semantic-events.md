# ADR-0006: Persist significant semantic events, not token-level streaming fragments

**Status:** Accepted  
**Date:** 2026-09-25

## Context

Live SSE streams improve situational awareness, but retaining every token/event fragment would increase storage, privacy exposure, and operational complexity without being necessary for audit or recovery.

## Decision

Persist significant semantic events such as assignment/state changes, messages, decisions, approvals, errors, handoffs, artifacts, scope/budget events, and completion with sequence/correlation identity. Treat fine-grained token streaming as ephemeral. Preserve raw transcripts and structured Markdown handoffs separately where available.

## Consequences

ACMS can reconstruct important state transitions and recover after disconnects without becoming a token-logging system.

## References

- ACMS-REQ-021
- ACMS-REQ-025
- ACMS-REQ-035
