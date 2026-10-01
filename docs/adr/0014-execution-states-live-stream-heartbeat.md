# ADR-0014: Work execution state model, live activity streaming, and heartbeat ingestion

**Status:** Proposed (human direction: STEA-004 execution plan 2026-10-01 §4/§14/§15/§16)
**Date:** 2026-10-01

## Context

Work Items show `active` without real execution; the homepage honestly reports
"not yet implemented" for agent status / last contact / cost; heartbeat
ingestion exists (`POST /api/v1/fleet/agents/{id}/heartbeat`) but no worker
pushes it, so `agent_status_current` is empty. Hermes 0.19.0's api-server
exposes `GET /v1/runs/{id}/events` (SSE: run.*, tool.*, message.delta,
reasoning.available) and `/health/detailed` — verified live on acms-worker-001.

## Decision

### 1. Work execution states (plan §4)

Work Items keep `PLANNED | ACTIVE | BLOCKED | IN_REVIEW | COMPLETED | CANCELLED`
as management states (ADR-0011 boundary), and **execution tasks** carry the
fine-grained transport/execution states:

```text
DISPATCHING -> RUNNING -> (SUCCEEDED | FAILED | CANCELLED)
```

Rules:
- `RUNNING` is written ONLY when the bridge send returned a run id (ACK).
- `ACTIVE` on a Work Item means: has an open execution session or a RUNNING
  task — never assignment alone. The Work board renders `RUNNING` only from
  execution-task state, never from assignment status.
- A stale RUNNING task with no bridge heartbeat for > `stale` threshold is
  reconciled by the watcher/reconcile sweep as today.

### 2. Heartbeat ingestion (plan §16)

ACMS-side **status poller** (telemetry scheduler extension): every 60 s, for
each agent with a bridge target, poll `GET /health/detailed` + top session via
`GET /api/sessions`, and ingest through the existing
`TelemetryService.ingest_heartbeat` as `CONTACT_SOURCE_RECONCILIATION`
heartbeats. This makes the homepage live WITHOUT requiring a worker-side
pusher (the worker stays a plain Hermes install). Payload mapping:

- `active_agents`, `gateway_busy`, `gateway_state` → `agent_running` tri-state
- top session `id/title/last_active` → session correlation + alignment
- session `model`, token counts → model + usage telemetry
- platform state/error → connectivity + harness facts

Stale = 300 s, reconcile ≥ 3600 s, daily fleet sweep — ADR-0010 defaults
unchanged. Human-visible states on cards: HEALTHY/STALE/UNREACHABLE (existing)
+ `DISPATCHING/RUNNING/WAITING_FOR_HUMAN` derived from execution state.

### 3. Live activity streaming (plan §15)

For a RUNNING execution task, ACMS runs a **session-event pump**: background
asyncio task per active run that GETs the worker's
`/v1/runs/{run_id}/events` SSE stream, filters/maps events to the ACMS
`execution_events` store (new table: `execution_session_id`, `seq`, `event_type`,
`ts`, `payload` JSON, capped preview lengths), and the browser subscribes to
ACMS-only SSE `GET /api/v1/execution/{task_id}/stream` with `Last-Event-ID`
resume. The browser NEVER connects to worker VMs. Pumps are idempotent per
(run_id), stopped on terminal status, and never persist secrets (payload
allow-list: event, tool, duration, delta preview ≤ 500 chars, output preview
≤ 2000 chars, usage tokens; `reasoning.available` text dropped — plan §14
forbids exposing non-user-visible reasoning).

## Consequences

- Homepage "not yet implemented" panel retires; fleet + agent cards show real
  status/last contact.
- Live stream survives worker restarts (reconnect → pump re-subscribes from
  last seq stored per task).
- Polling adds ≤1 req/60s per agent to worker bridges — negligible.