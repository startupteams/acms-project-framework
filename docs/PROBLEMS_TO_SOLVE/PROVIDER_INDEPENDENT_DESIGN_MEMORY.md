# PROBLEM TO SOLVE: Provider-independent durable design memory

**Status:** OPEN — architecture direction recorded; no implementation in this slice
**Raised:** 2026-09-28 (STEA-004 REV2 plan §6, human plane notes)
**Constraint:** human explicitly does NOT want long-term durable design memory dependent on a single paid frontier provider.

## Problem

Durable design memory (human execution instructions, plans, worker handoffs,
decisions) is currently scattered across:

- a paid frontier provider's session storage (de-facto, unintended dependency);
- Git (worker state/versioning via hermes-base-setup branches + ACMS repos);
- VM-local Markdown handoffs and ACMS DB rows;
- agent-local `~/.hermes` state with ad-hoc sync timers.

No single AgentifyMe-controlled store is authoritative, and one important class
(the provider-hosted history) is both non-exportable-in-full and billable — an
unacceptable dependency for institutional memory.

## Direction (agreed in the worksheet — to be implemented, not yet built)

1. **Important human execution instructions/plans** shall be retained in
   AgentifyMe-controlled storage (ACMS / Git under startupteams), never only in a
   provider's session store.
2. **Worker Markdown handoffs** shall be retained by ACMS with a current target
   of **at least ~90 days** retention unless later changed by policy.
3. **Git is an acceptable current worker-state/version mechanism** but is NOT
   assumed to be the permanent context/memory database.
4. **Markdown stays the human-readable representation**; the backend structured
   storage/search layer may differ (DB rows, object storage, index).
5. **Vector search** shall be evaluated only as a retrieval/index layer — never
   silently adopted as canonical truth. Canonical rows remain queryable without it.
6. **Provider-independent export/summarization** of durable design memory is a
   future architectural need (batch export + normalization into ACMS-owned storage).

## Safety valve

No destructive retention/purge jobs may be implemented until the retention
policy above is finalized by the human. The ~90-day figure is a floor target for
worker handoffs, not a purge trigger.

## Candidate implementation path (for a future slice, NOT approved scope)

- ACMS gains a `design_memory` object class (Markdown body + structured metadata:
  work refs, agent, timestamps, classification) with append-mostly semantics.
- The existing `context_packages` reference model already fits: memory entries are
  referenced, not copied, into session packages.
- Export job: pull provider-side session summaries into ACMS-owned rows on a
  schedule, so the provider copy can be treated as disposable cache.

## Decision needed from the human

1. Confirm the ~90-day floor and whether any classes (e.g. audit events) are permanent.
2. Approve (or reshape) the `design_memory` storage direction before any build.
