# ADR-0011 — Measurable-goal Work lifecycle

- **Status:** Proposed (agent-originated; human acceptance pending)
- **Date:** 2026-09-29
- **Deciders:** STEA-004 (author), Jordan Ulmer (acceptance pending)
- **Related:** ACMS-REQ-055, REQ-013, REQ-057, REQ-059; `docs/architecture/MEASURABLE_GOAL_WORK_LIFECYCLE.md`

## Context

Human direction (2026-09-29): work should be organized as *measurable goal → execute → handoff →
next measurable goal as a new plan/fresh session*. ACMS already supports session continuity inside
a Work Item (REQ-055) and a parent/child work hierarchy, but the default decomposition policy was
never codified.

## Decision (Proposed)

1. **One Work Item = one measurable goal** with explicit acceptance criteria and its own budget.
2. **Sequential goals = child Work Items** under the same parent Feature/Program (`parent_id`;
   no schema change). Lineage convention: reference the predecessor Work key in title/scope.
3. **Execution sessions remain disposable inside a Work Item** (REQ-055/056/057): rotation,
   crash-retry, context-rotation never end the Work.
4. **Executive Agent next-work creation** (where authorized): completed Work + complete handoff →
   next child Work Item under the same parent; provenance recorded; out-of-scope findings become
   proposals, never silent scope changes.
5. **Review findings classify BLOCKING / REQUIRED / OPTIONAL; OPTIONAL never blocks completion;
   review never creates new product scope.** No hard review-loop count.

## Consequences

- No code/schema change required (documentation + convention).
- Budgets stay per-goal (REQ-059) — finer cost attribution per accepted output.
- Conserves REQ-055 semantics; does not prejudge the open Work↔Session boundary (workbook A/B/C).
- If accepted, SPRINT/REQUIREMENTS get a pointer; if rejected, the doc is withdrawn with no code impact.

## Alternatives considered

- **Multi-goal Work Items** (rejected: blurs budget attribution and "definition of done").
- **New lineage schema (successor_work_item_id)** (rejected: over-engineering; event trail +
  naming convention reconstructs lineage without migration risk).
