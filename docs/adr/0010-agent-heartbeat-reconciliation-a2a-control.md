# ADR-0010: Agent heartbeat, session/work correlation, reconciliation, and A2A live control

**Status:** Accepted (human-approved defaults, combined slice 3+4 plan 2026-09-26 §2.2)
**Date:** 2026-09-26

## Context

ACMS currently records management state (Work Items, primary assignments,
execution tasks, routines) but has no live view of agent connectivity, running
state, session identity, or context telemetry, and no way to deliver work or
steer agents (REQ-031/033/034/035/036/037 remain partially implemented). The
combined slice 3+4 plan (2026-09-26) directs ACMS to become a real workforce
control plane. This ADR records the human-approved defaults and invariants for
that slice.

## Decision

### Approved operational defaults (configuration-backed, not UI-controlled in MVP)

```text
heartbeat interval               60 seconds   (ACMS_HEARTBEAT_INTERVAL_SECONDS)
STALE threshold                 300 seconds   (ACMS_HEARTBEAT_STALE_SECONDS)
stale reconciliation threshold 3600 seconds   (ACMS_STALE_RECONCILE_SECONDS)
fleet-wide reconciliation      86400 seconds   (ACMS_FLEET_RECONCILE_SECONDS)
session-alignment grace        120 seconds   (ACMS_SESSION_ALIGNMENT_GRACE_SECONDS)
context ELEVATED                70 percent   (ACMS_CONTEXT_ELEVATED_PERCENT)
context HIGH                    85 percent   (ACMS_CONTEXT_HIGH_PERCENT)
context CRITICAL                95 percent   (ACMS_CONTEXT_CRITICAL_PERCENT)
telemetry sample interval      300 seconds   (ACMS_TELEMETRY_SAMPLE_SECONDS)
```

### State-dimension invariants (plan §5)

1. **Connectivity** is derived by ACMS from contact recency: `UNKNOWN` (never
   contacted), `HEALTHY` (recent heartbeat/reconciliation), `STALE` (> 5 min
   without contact), `UNREACHABLE` (stale ≥ 1 h AND active reconciliation
   failed). `last_contact_source` records how (heartbeat / reconciliation /
   registration).
2. **`agent_running`** is harness-reported tri-state (`true` / `false` /
   `unknown`): whether the harness has an active agent execution — NOT whether
   the VM/gateway is reachable. `Connectivity: HEALTHY` + `Agent Running: No`
   is a valid, normal combination.
3. **A2A task state** uses the A2A protocol's native task-state model; ACMS
   does not invent parallel names for A2A states.
4. **Management state** (Work Item / Assignment lifecycle) is independent of
   all of the above. Reconciliation may automatically correct observed
   operational state (preserving history, logging discrepancy events), but
   must never silently change approved scope or assignment.
5. **Reconciliation authority:** observed harness facts correct operational
   telemetry automatically without human approval; disagreements are logged as
   events; approved scope/assignment is never silently modified.

### Heartbeat contract

A complete lightweight versioned status snapshot (`acms-heartbeat-v1`) every
60 seconds: identity, observed_at, assignment correlation keys, session
(id/title/timestamps/`agent_running`), model/provider, context
(used/max/utilization with the source recorded; never fabricated), usage
(session vs cumulative, separately named), connected platforms. Context
warning levels: NORMAL < 70% ≤ ELEVATED < 85% ≤ HIGH < 95% ≤ CRITICAL.
Invalid telemetry renders UNKNOWN/INVALID — never fabricated, never treated
as a context-overflow signal (explicit overflow errors are recorded as
`CONTEXT_OVERFLOW` events).

Routine heartbeat receipt does not create semantic events; material changes
do (§11 of the plan: CONNECTIVITY_CHANGED, AGENT_RUNNING_CHANGED,
SESSION_CHANGED, MODEL_CHANGED, CONTEXT_WARNING_CHANGED,
CONTEXT_OVERFLOW, SESSION_ASSIGNMENT_MISMATCH, RECONCILIATION_*, etc.).

### Human-readable correlation keys

Work Items carry immutable `ACMS-WORK-000001`-style keys and assignments carry
`ACMS-ASG-000001` keys (UUIDs remain internal identifiers). Keys are allocated
durably (PostgreSQL sequences), never reused, never derived from row counts.
Dispatch sends the work key in the desired session title
(`[ACMS-WORK-000042] …`) plus structured metadata (`acms_work_key`,
`acms_assignment_key`). External tracker references may coexist but never
replace the ACMS key. ACMS compares the harness-reported session title/keys
against the approved assignment (ALIGNED / UNKNOWN / MISMATCH) on a grace
period of 120 s; persistent mismatch creates a
`SESSION_ASSIGNMENT_MISMATCH` event and may request a structured handoff —
approved scope is never rewritten by alignment.

### A2A/bridge control contract

Canonical ACMS bridge capabilities: `status`, `send_work`, `steer`, `pause`,
`resume`, `interrupt`, `cancel`, `request_handoff`, `set_session_title` —
each advertised per-harness only after tested semantics (plan §13 matrix in
`docs/HARNESS_CONTROL_MAPPING.md`). Required semantics: `pause` = resumable
suspension of the same unit; `interrupt` = stop current run/turn preserving
session; `cancel` = terminal for the A2A execution task. **Unsupported
controls are advertised unsupported, never faked** (never fake pause with
interrupt). Harness lifecycle mappings are researched and live-tested on a
non-critical Hermes agent before coding; the installed Hermes version's actual
behavior is authoritative, not upstream docs.

### Persistence & scheduling

One current-status record per agent (updated every heartbeat) + periodic
telemetry samples (5 min default, plus on material change) + semantic events
per REQ-035. Lightweight in-process scheduling (stale evaluation, 1-hour
reconciliation, 24 h fleet reconciliation, sampling) — no Celery/K8s/event
bus; if ACMS ever runs replicas, leader/lock coordination is required
(documented future work). A2A owns A2A task-state enumerations; ACMS
management scope stays separate from runtime state.

## Consequences

- ACMS gains live fleet awareness and control without collapsing distinct
  state dimensions into one misleading status.
- Heartbeat cadence is a complete snapshot (not deltas) — simple, idempotent.
- Stale/UNREACHABLE states are operational only; they never alter approved
  scope.
- Harnesses without tested capabilities simply lack those controls; UI hides
  unsupported controls rather than offering fake ones.
- Context telemetry is first-class operational data; overflow-classified
  failures become events rather than silent degradation.
- Multi-replica ACMS would need leader election for schedulers (future work).

## References

- Combined slice 3+4 plan (2026-09-26) §2, §5–§13, §16
- ACMS-REQ-031–037 (existing A2A/heartbeat/reconciliation/events),
  ACMS-REQ-052/053/054 (new)
- ADR-0009 (safe-release transaction — Accepted this date)
- ADR-0007 (stack), ADR-0008 (UI)
