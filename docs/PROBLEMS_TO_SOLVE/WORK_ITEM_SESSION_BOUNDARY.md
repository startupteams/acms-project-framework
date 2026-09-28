# PROBLEM TO SOLVE: Work Item ↔ Session boundary semantics

**Status:** OPEN — human decision required
**Raised:** 2026-09-28 (STEA-004 REV2 plan §5, from the human worksheet)
**Blocks:** eventual hardening of REQ-055; no production behavior is changed by this document.

## The conflict

The live multi-session proof (2026-09-28, prod) treats **one Work Item as the durable
unit that spans many disposable execution sessions**: Session 1 closes, Session 2
opens with a compact reconstructed context package, and the same Work Item stays
ACTIVE with its assignment intact. This is a literal reading of ACMS-REQ-055
("continuity across disposable sessions").

The human's clarified preferred operating workflow (2026-09-28 worksheet) is:

```text
measurable goal → fresh session → handoff → create a NEW plan → /new → next measurable goal
```

…which semantically maps better to: **each fresh session gets a NEW Work Item**, with
the durable continuity carried by a parent (Feature/Program) rather than by one
long-lived Work Item. The human explicitly does NOT want the already-working
multi-session capability removed.

## Options

### A. One Work Item spans many sessions (current implementation)

- Work = long-lived unit of accountability; sessions = disposable reasoning contexts.
- Budgets (REQ-059) naturally attach to the whole multi-session effort.
- Matches the shipped Phase 8 proof; no migration of concepts.
- Risk: Work Items accumulate unrelated sessions; "done" is per-session handoffs,
  so Work-level completion criteria get fuzzy.

### B. Parent Feature/Program spans many Work Items; each new session normally gets a new Work Item

- Work Item ≈ one measurable goal/execution sprint; parent carries continuity.
- Clean 1:1 mapping between session runs and Work completion; budgets attach at
  either level (parent = program envelope, child = sprint envelope).
- Requires: convention (or enforcement) that rotation closes the child Work and
  creates a sibling under the same parent, plus rollups that aggregate children
  into the parent.
- Risk: more bookkeeping; the Phase 8 proof flow becomes the "exception" path.

### C. Support both; B is the default

- Keep A working (additive, already deployed), make B the recommended pattern.
- Requires the parent-child rollups of B plus documentation; no destructive change.
- Risk: two patterns can coexist confusingly without clear guidance about when
  to use each.

## What is NOT in question

- Sessions stay disposable; checkpoints/handoffs remain the continuity mechanism.
- Budgets and telemetry stay per-Work and per-session as implemented in v0.9.0.
- No retention/purge behavior changes.

## Recommendation (agent view, non-binding)

Option C: the deployed implementation is additive and correct for long-running
efforts (e.g. this infrastructure program itself), while B matches the
human's daily build-measure-rotate loop. Decide which pattern the A2A delivery
slice should *enforce* vs merely *permit*.

## Decision needed from the human

1. Which option is canonical (A / B / C-with-B-default)?
2. If B: should session rotation auto-close the child Work Item and open a sibling,
   or leave that to the operator?
