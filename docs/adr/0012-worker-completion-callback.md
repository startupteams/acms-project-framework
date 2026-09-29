# ADR-0012 — Worker Completion Callback

- **Status:** Accepted (Jordan Ulmer, 2026-09-29 — execution plan §1.8)
- **Date:** 2026-09-29
- **Deciders:** Jordan Ulmer (acceptance), STEA-004 (author)
- **Related:** ACMS-REQ-036, REQ-055, REQ-058, REQ-059; ADR-0003 (A2A bridge), ADR-0010 (heartbeat/reconciliation), `acms/session_lifecycle.py`, `acms/completion_callback.py`

## Context

The ExecutionSession lifecycle slice (PR #41) auto-opens a session on dispatch and
closes it on explicit completion, but the worker runtime never called back —
tasks stayed RUNNING until a manual close, and the reconcile sweep was the
interim authority. Execution windows need a **primary immediate completion
signal** from the runtime, with reconciliation kept only as the repair path.

## Decision (Accepted — human decision 2026-09-29)

Canonical completion architecture:

```text
Bridge/worker completion callback = PRIMARY immediate completion signal
ACMS reconciliation sweep         = FALLBACK / repair mechanism
```

### Callback contract

`POST /api/v1/callbacks/execution-completion` (versioned; dedicated scoped
bearer token `ACMS_CALLBACK_TOKEN` — a runtime never holds the admin token).

Carries durable semantic data only:

```text
task/run ID
terminal status: SUCCEEDED | FAILED | CANCELLED
completed_at
result/handoff reference
usage/cost references when available
concise error summary when failed
```

Never includes: full transcript, token stream, verbose reasoning/context.

### Identity verification (fail closed)

The callback binds `task_id + acms_agent_id (+ a2a_run_id when known)`; the
recorded execution task must already belong to the claimed agent. A runtime
cannot close another runtime's task → 403 + durable
`EXECUTION_CALLBACK_REJECTED` audit event. Unknown task id → 404 + audit
event. Unconfigured/missing token → 503/401 (endpoint fails closed).

### Idempotency and authority

```text
duplicate callback        → harmless, idempotent 200 (no duplicate close)
late callback after close → harmless (already_terminal)
unknown task ID           → reject + audit event
wrong agent/runtime       → fail closed + audit event
lost callback             → reconciliation eventually repairs state
```

### On terminal result (atomic)

`execution_tasks` terminal state + `finished_at`; correlated ExecutionSession
CLOSED with `ended_at` (result/handoff references + usage/cost references live
in the semantic event metadata); semantic event
`EXECUTION_COMPLETED` / `EXECUTION_FAILED` / `EXECUTION_CANCELLED`.

Attention behavior: SUCCEEDED → normally no Attention; FAILED → Attention per
existing severity policy (`EXECUTION_FAILED`); CANCELLED → semantic event only,
Attention only if context warrants.

### Reconciliation fallback

The telemetry scheduler sweeps nonterminal execution tasks (rate-limited,
bounded batch) against the authoritative bridge run state and closes tasks
whose A2A run is terminal — durable `EXECUTION_RECONCILED` events mark
reconciler-closed facts. No manual close is required in the steady state.

## Consequences

- Steady-state completion requires no human action and no manual session close.
- Failed runs retain their cost (close never zeroes economic fields — REQ-058).
- The callback adds one scoped credential (`ACMS_CALLBACK_TOKEN`) to the CT122
  env; rotation discipline matches the other ACMS credentials (deferred per the
  standing 2026-09-29 decision unless a threat appears).
- Worker side should call back on terminal state; until every runtime does,
  reconciliation remains authoritative for repair (both paths are idempotent).