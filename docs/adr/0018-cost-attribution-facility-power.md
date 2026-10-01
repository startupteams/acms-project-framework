# ADR-0018: Cost attribution and facility power visibility (ACMS + LLM Manager split)

**Status:** Proposed (human direction: STEA-004 execution plan 2026-10-01 §26–§28)
**Date:** 2026-10-01

## Context

Homepage cost is "not yet implemented"; local inference has no per-work
attribution; facility power is collected on VM114 (Emporia adapter live: 57k+
samples, channel map ch1-3 = PDU feeds, ch4 = mini-split) but has no human
view wired to the plan's §27.3/§27.5 mapping.

## Decision

### Responsibility split (boundary-clean)

- **LLM Manager (VM114)** owns ALL facility-power and electricity-cost truth:
  collector, rate model (existing Alliant incremental tables), rollups. It
  already has `/api/facility` (auth'd). This window ADDS the human view
  (dashboard card + `/api/facility` surface verified) showing per plan §28:
  PDU MIAM-00151/152/153 kW, 36k mini-split kW, TOTAL MARION_IA_USA, daily/
  monthly kWh+cost by source, collector metadata (source=Emporia,
  collector=VM114, last sample age, health). Channel map labels get the
  canonical PDU names (ch1→PDU MIAM-00151, ch2→152, ch3→153, ch4→36k 3 Ton
  mini split) with original Emporia names kept as metadata. Stale data renders
  with last-good timestamp, never silently zero.
- **ACMS** owns work-attributed cost: per execution-session cloud spend
  (LiteLLM SpendLogs by api_key → worker key attribution) and local token
  counts (from session telemetry + run usage). ACMS shows:
  - cloud: tokens + actual USD (LiteLLM spend) per session/work/project
  - local: tokens; estimated cost only when a rate is configured, labeled
    `estimated`; unattributed → "not yet attributed", never $0.00
- VM114 remains the single Emporia credential holder
  (`/etc/llm-manager/secrets/emporia_creds`, root:root 0600 — verified present).
  No new credential copies anywhere (workstation copy stays for validation
  only). Total = mini-split + PDU151 + PDU152 + PDU153; cooling never omitted;
  PDU feeds never double-counted.

### Attribution mechanics (ACMS)

- `execution_sessions` already capture model + tokens; extend usage_records
  path: on completion callback/watcher terminal, pull the run's usage from
  bridge run status (input/output tokens) AND query LLM Manager economics
  surface (`/api/v1/economics` on VM114 reports spend by api_key) for cloud
  spend attribution. Worker api_key → agent mapping is static config
  (one key per worker, `agent_key_acms_worker_001`).
- Local cost estimate: tokens × measured facility $/kWh × model-specific
 Wh/token allocation would overstate — plan §27.7 forbids exceeding measured
  PDU cost. MVP: show tokens + host + duration + TTFT; per-model electricity
  allocation ships as `estimated` once the LLM Manager allocation model lands
  (FUTURE_WORK); sum-guard enforced then.

## Consequences

- Homepage cost card shows: today's cloud spend (actual), today's local tokens
  (real), local $ estimate (labeled) — all real data or honest nulls.
- Emporia integration = zero new credentials, zero new collectors; power UI
  lives where the data already lands.