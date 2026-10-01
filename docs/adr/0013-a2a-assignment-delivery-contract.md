# ADR-0013: A2A assignment delivery contract (ACK, busy, idempotency, one-task-per-worker)

**Status:** Proposed (human direction: STEA-004 execution plan 2026-10-01 §3/§12/§13)
**Date:** 2026-10-01

## Context

ACMS records assignments but the delivery path stops at the bridge `send_work`
call: there is no worker ACK contract, no deterministic busy rejection, and no
runtime-side assignment envelope. The plan (§12) forbids showing a worker as
active merely because a Work Item was assigned, and §3 forbids a worker holding
more than one active Work Item.

## Decision

### Assignment envelope (`acms-a2a-assignment-v1`)

Dispatch builds one JSON envelope and sends it as the run instruction metadata
block. Required fields: `schema_version`, `assignment_id`, `idempotency_key`,
`agent_id`, `work_item_id`, `execution_session_id`, `jira_issue_key`,
`project` (never null), `product`/`sprint` (nullable), `tags`, `instructions`,
`completion_condition`, `output_artifacts_expected`, `model_policy`,
`priority`, `callback_urls`, `created_at`.

### ACK

The worker bridge returns an ACK (`accepted`, `agent_id`, `work_item_id`,
`execution_session_id`, `runtime_instance_id`, `accepted_at`,
`effective_model_policy`). ACMS marks an execution `RUNNING` only after
ACK-equivalent evidence: the Hermes `POST /v1/runs` 202/200 response carrying
`run_id` IS the ACK for this implementation (Hermes api-server is the bridge
process; a run id proves the harness accepted and started scheduling the run).
`DISPATCHING` = envelope built, send in flight. Send failure ⇒ back to
`PLANNED` + `EXECUTION_FAILED` event + bounded retry.

### Busy (`409 WORKER_BUSY`)

A worker holds at most one active Work Item. The one-task lock is enforced by
the existing one-ACTIVE-primary-per-agent invariant (`work_assignments`, REQ-008)
plus a new scheduler-level check: `dispatch_work` refuses when the target agent
has a RUNNING execution task for a DIFFERENT work item (`409 worker_busy`,
detail carries the blocking `work_item_id` + `task_id`). Priority is scheduler
metadata, never permission to multitask.

### Idempotency & retry

Unchanged from the existing ledger (dedupe key in EXECUTION_DISPATCHED event
metadata → task_id). Retries re-POST the same idempotency key and receive the
original task. After bounded retries without a run id, the item returns to
`PLANNED` with `WORK_DISPATCH_FAILED` recorded and a Human Inbox item created —
never a phantom active item.

## Consequences

- `RUNNING` means real execution (ACK received), eliminating the false-active
  class on the Work page.
- No new transport: delivery stays bridge HTTP; the envelope rides inside the
  run instruction with the machine-readable JSON header, so Hermes 0.19 needs
  zero changes.