# PR 11 — Combined Slice 3+4: Heartbeat, Correlation, Reconciliation, A2A Control (v0.6.0)

**Date:** 2026-09-27 · **Branch:** `feat/ACMS-031-agent-bridge-live-control` · **Merged:** `2f94bbb` · **Deployed:** CT122 via release.sh — `Release ACCEPTED` (alembic `0003_live_telemetry`)

## Plan §26 handoff items

1. **PR/SHA:** PR #11, merged `2f94bbb`, deployed `acms-app:2f94bbb` (2f94bbb3b9fb6ce03da9fb29aa6a0a285b88f46d)
2. **Requirements:** REQ-031..037 clarified (033 heartbeat-snapshot, 034 reconciliation authority); **REQ-052/053/054 added** (keys, alignment, context telemetry)
3. **ADRs:** 0009 → Accepted (amended: live evidence + rollback pre-authorization policy); **0010 Accepted** (60 s heartbeat / 300 s stale / 3600 s reconcile / 86400 s fleet / 120 s grace / 70-85-95 context warnings — config-backed)
4. **Migration:** `0003_live_telemetry` — add-only (agent_status_current, agent_telemetry_samples, agent_events, agent_session_bindings, acms_key_counters + key/dispatch/ack/session columns). Proven upgrade→downgrade→upgrade on real PG16 before merge.
5. **Key scheme:** `ACMS-WORK-000001` / `ACMS-ASG-000001` via transactional counter table; keys never reused; UUIDs stay internal.
6. **Heartbeat schema:** acms-heartbeat-v1 (identity/assignment/session/model/context/usage/platforms) — unknown fields rejected.
7. **Production values:** interval 60 s, stale 300 s, reconcile 3600 s, fleet 86400 s, sample 300 s, grace 120 s (all `ACMS_*`-configurable).
8. **Installed Hermes:** 0.17.0, commit f3d2dfb (2026-06-30), scratch profile `acms-bridge-test` + api-server :8642 (bearer).
9. **Tested mapping** (docs/HARNESS_CONTROL_MAPPING.md): status ✅ send_work ✅ steer ✅ (live mid-run injection) interrupt ✅ (session preserved; same-session follow-up verified) cancel ✅ (terminal at A2A task) request_handoff ✅ (via dispatch) set_session_title ✅ (PATCH).
10. **Pause/Resume: UNSUPPORTED** (404 live) — never faked; listed as unsupported in UI.
11. **Interrupt result:** running → stopping → cancelled; session + messages preserved; follow-up in same session answered with context.
12. **Cancel result:** terminal for the A2A execution task; Hermes session reusable (new execution task binds to it).
13. **Context telemetry source:** run/session token usage exposed by api-server; **context max not exposed** → ACMS records max_source, renders UNKNOWN/INVALID when absent (never fabricated).
14. **Tests:** 72 passing (unit + PG integration + migration loop on real PG16).
15. **Live E2E:** bridge fetch_status → heartbeat ingest → HEALTHY recorded (verified against live Hermes 0.17.0 instance).
16. **Mismatch/handoff primitive:** alignment derivation (ALIGNED/UNKNOWN/MISMATCH) + SESSION_ASSIGNMENT_MISMATCH event type + handoff request path implemented at service layer; UI displays alignment.
17. **Rollback readiness:** pre-deploy backup verified (PGDMP+sha256); pipeline accepted on first pass; no rollback needed.
18. **Unsupported capabilities (honest):** pause/resume (harness), External Agent Gateway (out of scope §25).
19. **Future-work discoveries:** context-max from LLM Manager metadata; multi-replica leader election; per-agent control buttons once bridge targets configured on the deployed host; sticky-session routing for steering (bridge-side).
20. **Next recommended:** wire bridge targets into deployed ACMS env + dispatch buttons; slice 5 (live events SSE); slice 6 (audit/attention).

## Validation
- 72/72 tests; smoke test passed on box; /version reports 2f94bbb; alembic 0003 live.
- E2E chain proven live: Hermes (bridge fetch_status) → TelemetryService.ingest_heartbeat → status HEALTHY.
