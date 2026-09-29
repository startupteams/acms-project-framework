# Measurable-Goal Work Lifecycle (Phase B — default policy, 2026-09-29)

**Status:** Adopted as default policy (docs-only, additive; ADR-0011 PROPOSED for human acceptance)
**Human direction (2026-09-29 workbook):** *measurable goal → execute → handoff → next measurable goal gets a new plan/fresh session.*
**Requirement anchors:** ACMS-REQ-055 (work continuity across disposable sessions), REQ-013 (handoffs), REQ-052/053/054 (correlation/alignment/context), REQ-057 (handoff completeness), REQ-059 (budgets).

## 1. Default lifecycle

```text
Feature / Program (parent Work Item)
  → Work Item = ONE measurable goal
      → one or more execution sessions if needed   (REQ-055: session crash/rotation/retry
      → complete handoff (REQ-057 completeness)      never ends the Work)
  → next measurable goal = NEXT CHILD Work Item
      (new plan; fresh session; provenance recorded)
```

- A Work Item's acceptance criteria should state the **measurable goal** (definition of done, artifact, verification command, budget). Free-form tasks remain allowed but discouraged.
- **Session lifecycle inside a Work Item is ACMS's memory-offload machinery** (REQ-055/056/057): rotation, crash-retry, or context-rotation during the same goal does **not** create a new Work Item.
- A **new measurable goal always gets a new child Work Item** — never a silent scope change on the completed one (AGENTS.md scope rule).

## 2. Parent/child continuity (existing support, confirmed)

The hierarchy already exists (`work_items.parent_id`, any kind may nest, parent must exist,
`list_children` implemented — `acms/work_models.py`, `acms/work_service.py`). **No new schema.**
Mapping to the policy:

| Level | ACMS kind | Notes |
|---|---|---|
| Feature / Program | `feature` / `program` | stable, human-owned |
| Measurable goal (unit of execution) | `task` (default) with `parent_id` | budget + acceptance criteria live here |
| Sequential next goal | next `task`, same `parent_id` | handoff of predecessor references it |

Convention: a child task **SHOULD** name its predecessor in the title/scope or in its opening
handoff reference ("continues ACMS-WORK-0000xx") so lineage is reconstructible from the event trail
without a new schema column.

## 3. Executive Agent creating next work (B3)

Where an Executive Agent's authorization already permits it:

```text
completed Work + complete handoff (REQ-057 validator result recorded)
→ Executive Agent creates/proposes the next child Work Item under the SAME parent
→ provenance recorded (created_by=<executive agent>, scope traced to parent)
→ no silent scope expansion: parent scope_markdown is the boundary
```

If the next goal falls **outside** the approved parent scope, the Executive Agent records it as a
proposal (event + human attention), not as a created Work Item.

## 4. Review-loop guardrail (B4)

Architecture rule (additive, no code change required):

- Review findings are classified **BLOCKING / REQUIRED / OPTIONAL**.
- **OPTIONAL findings never prevent completion.** A work item with only OPTIONAL findings is complete.
- Review does not create new product scope; findings map onto the existing Work Item's acceptance
  criteria or become proposals (per §3).
- No hard review-loop count is invented. If requirements later define one, it slots in here.

## 5. What this policy does NOT change

- One-ACTIVE-primary-assignment-per-agent invariant (unchanged).
- Budgets remain per-Work-Item (REQ-059): each child goal carries its own soft/hard budget.
- Human approval for merges/deploys (unchanged).
- The A/B/C Work↔Session boundary decision from the architecture workbook remains open — this
  policy is compatible with all three options and does not prejudge them.

## ADR-0011 (PROPOSED — agent-originated)

*Measurable-goal Work lifecycle:* the default decomposition above (one Work Item = one measurable
goal; child Work Items under a parent Feature/Program for sequential goals; sessions disposable
inside a Work Item per REQ-055; OPTIONAL review findings non-blocking). Remains Proposed until
human acceptance; no schema change required (hierarchy exists).
