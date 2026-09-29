# ADR-0011 — Measurable-goal Work lifecycle

- **Status:** Accepted (Jordan Ulmer, 2026-09-29)
- **Date:** 2026-09-29 (originated 2026-09-29, Proposed; accepted same day per execution plan §1.4)
- **Deciders:** Jordan Ulmer (acceptance), STEA-004 (author)
- **Related:** ACMS-REQ-055, REQ-013, REQ-057, REQ-059; `docs/architecture/MEASURABLE_GOAL_WORK_LIFECYCLE.md`

## Context

Human direction (2026-09-29): work should be organized as *measurable goal → execute → handoff →
next measurable goal as a new plan/fresh session*. ACMS already supports session continuity inside
a Work Item (REQ-055) and a parent/child work hierarchy, but the default decomposition policy was
never codified.

## Decision (Accepted)

1. **One Work Item = one measurable goal** with a measurable deliverable, explicit acceptance
   criteria, and its own budget.
2. **A Work Item may contain** multiple execution sessions, multiple `/new` context rotations,
   multiple tasks, multiple worker agents, multiple commits, and multiple PRs — provided they all
   contribute to the same measurable goal. A `/new` caused by context size, model/session rotation,
   or interruption does **not** create a new Work Item if the measurable goal is unchanged.
   When the measurable goal changes, create a new Work Item.
3. **Sequential goals = child Work Items** under the same parent Feature/Program/Jira issue
   (`parent_id`; no schema change). Lineage convention: reference the predecessor Work key in
   title/scope. Work Items may share a higher-level Jira issue / Project / Feature / Program /
   human business goal.
4. **Execution sessions remain disposable inside a Work Item** (REQ-055/056/057): rotation,
   crash-retry, context-rotation never end the Work.
5. **Creation authority (enforced, not just convention):**
   - Administrator (bearer admin token) → may create Work Items;
   - Executive Agent (delegated scope) → may create Work Items within delegated scope;
   - ordinary Worker agent → may **NOT** create Work Items;
   - Observer → may **NOT** create Work Items.
   Executive identity is configuration-backed via `ACMS_EXECUTIVE_AGENT_IDS` — never inferred
   from a display name. Enforcement lives at the API/service layer
   (`acms/work_creation_guard.py`; worker attempts are rejected 403 and audited as
   `WORK_CREATION_REJECTED`).
6. **Worker recommendation path:** a worker that identifies new work records a
   `proposed_next_work` block in its handoff (summary, reason, dependency, suggested acceptance
   criteria) → the Executive Agent decides whether to create a Work Item. Workers never create
   Work Items silently and never expand scope.
7. **Executive decomposition:** the Executive Agent may decompose an approved higher-level goal
   into multiple Work Items, delegate them, and stay within approved scope/budget. This prevents
   recursive/unbounded work creation.
8. **Review findings classify BLOCKING / REQUIRED / OPTIONAL; OPTIONAL never blocks completion;
   review never creates new product scope.** No hard review-loop count.

## Consequences

- One config addition (`ACMS_EXECUTIVE_AGENT_IDS`, default empty = workers cannot create).
- Budgets stay per-goal (REQ-059) — finer cost attribution per accepted output.
- Conserves REQ-055 semantics; does not prejudge the open Work↔Session boundary (workbook A/B/C).
- `created_by` provenance strings starting with `agent:` are validated against the Executive list,
  so worker creation cannot be laundered through the admin bearer path with a mislabeled claim.

## Alternatives considered

- **Multi-goal Work Items** (rejected: blurs budget attribution and "definition of done").
- **New lineage schema (successor_work_item_id)** (rejected: over-engineering; event trail +
  naming convention reconstructs lineage without migration risk).
- **UI-only hiding of creation controls** (rejected: plan §C3 explicitly requires API/service
  enforcement, not UI cosmetics).
