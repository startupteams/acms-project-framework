# ADR-0015: Model policy hierarchy, preferred-model routing, and local failover

**Status:** Proposed (human direction: STEA-004 execution plan 2026-10-01 §17–§20)
**Date:** 2026-10-01

## Context

Workers currently run whatever their Hermes profile `model.default` says
(`fast` alias → qwen3.6-35b-a3b today). The plan requires preferred-model
selection with local-first policy (qwen3.8-flash-next), verified routes, and
10-minute TTFT failover, without bypassing LLM Manager.

## Decision

### Policy hierarchy (highest wins)

```text
work item override > agent preference > project preference > system default
```

System default (settings-backed, migration-seeded):

```text
inference_policy = local-preferred
preferred_model  = qwen3.8-flash-next
cloud_fallback   = allowed
```

### Implementation shape (ACMS side)

- New tables: `model_policies` (scope: system | agent | project | work_item,
  scope_id nullable, fields: inference_policy, preferred_model,
  cloud_fallback, updated_by/at). One row per scope; effective resolution =
  first non-null up the chain with a `reason` label (work override / agent /
  project / system).
- Dispatch attaches the resolved policy to the assignment envelope AND sets
  the run's model: the dispatch instruction's envelope `model_policy` block is
  authoritative metadata; the ACMS bridge sends `/v1/runs` with
  `"model": "<effective_model>"` when the worker advertises it (Hermes honors
  per-run model from the request `model` field — the api-server applies
  model_routes/per-run override).
- Execution sessions record `requested_policy`, `effective_model`,
  `resolution_reason` (§20 proof block). `MODEL_ROUTE_SELECTED` /
  `MODEL_LOCAL_FAILOVER` / `MODEL_CLOUD_FALLBACK` audit events.
- UI: read-only effective policy on Agent/Work detail + editable controls
  (System settings, Project, Agent, Work Item) per plan §18.

### Local failover (10-minute TTFT)

The run watcher (existing `run_watcher.py`) gains a TTFT guard: if no run
status transition within `ACMS_MODEL_TTFT_TIMEOUT_SECONDS` (default 600) after
dispatch, record `MODEL_LOCAL_FAILOVER`, cancel the run via bridge
`interrupt`, and re-dispatch with the next model in the resolved fallback
chain (LLM Manager `/api/fleet` healthy routable locals, preferred first).
Cloud fallback only when `cloud_fallback=allowed` AND all local attempts
failed; spend caps + logging ride the existing budget/economics gates. For
`local-only`, cloud is never attempted.

### LLM Manager side (separate repo, coordinated PR)

LLM Manager already owns route truth (`model_registry`, `/api/fleet`); ACMS
consumes it — ACMS never maintains its own copy of backend health. The
LLM Manager change is limited to: verify `/api/fleet` exposes
healthy/routable/model name per host sufficient for ACMS chain building
(verified: it does), and agent-preference display (`/admin/agents`) may show
the ACMS-resolved policy read-only.

## Consequences

- Model selection becomes auditable and testable; "Requested/Effective" is
  provable per run.
- Flash-Next is a reasoning model — dispatch must not treat
  `reasoning_content` as the deliverable output; usage/token mapping stays
  with LiteLLM SpendLogs (economics path unchanged).