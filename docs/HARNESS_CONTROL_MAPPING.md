# Harness Control Mapping (ACMS-REQ-031/033/036; combined slice 3+4 plan §13–§14)

**Rule (ADR-0010):** no mapping is declared supported without a live test
against the actual installed harness version. Unsupported controls are
advertised unsupported — never faked (never fake `pause` with `interrupt`).

**Harness under test:** Hermes (Nous Research), **installed version 0.17.0**
(commit `f3d2dfb`, 2026-06-30) on VM906 (this workstation), profile
`acms-bridge-test` (scratch profile, no messaging platforms), gateway
api-server platform on `127.0.0.1:8642` (bearer auth). All tests below were
executed live on 2026-09-26/27 against that instance.

## Capability matrix — Hermes 0.17.0 (api-server transport)

| ACMS capability | Required semantics | Harness mapping (tested) | Tested version | Result |
|---|---|---|---|---|
| status | full current status | `GET /api/sessions/{id}` (id, title, model, message_count, input/output tokens, api_call_count) + `GET /v1/runs/{run_id}` (status, last_event, usage, output) + `GET /api/sessions` (last_active per session) | 0.17.0 f3d2dfb | **SUPPORTED** (live-tested) |
| send_work | dispatch controlled work | `POST /v1/runs` (input, metadata) or `POST /api/sessions/{id}/chat` + `X-Hermes-Session-Id` header for session-bound dispatch; returns run_id/session_id immediately (202 semantics) | 0.17.0 | **SUPPORTED** (live-tested) |
| steer | inject direction into active run | `POST /api/sessions/{id}/chat` with `X-Hermes-Session-Id` while run is active — message lands in-session (verified: "STEER: stop at 10" → agent acknowledged mid-run "Stopping there… Summary of what I was doing") | 0.17.0 | **SUPPORTED** (live-tested) |
| pause | resumable suspension of same unit | no HTTP endpoint (`POST /v1/runs/{id}/pause` → 404); only cron-job pause exists (`/api/jobs/{id}/pause`) — different domain | 0.17.0 | **UNSUPPORTED** (404 live) — never fake with interrupt |
| resume | continue same paused unit | n/a (no pause) | 0.17.0 | **UNSUPPORTED** |
| interrupt | stop current run/turn, preserve session | `POST /v1/runs/{id}/stop` → `agent.interrupt("Stop requested via API")`; status running→stopping→cancelled; **session preserved** (verified: messages intact, follow-up message in same session answered with full context "the run was interrupted before any output reached me") | 0.17.0 | **SUPPORTED** (live-tested) |
| cancel | terminal A2A task cancellation | same endpoint as interrupt (`/stop`); the A2A execution task terminates (status `cancelled`); the Hermes *session* survives, so continuing the Work Item later = new execution task bound to the same session | 0.17.0 | **SUPPORTED** (live-tested; terminal at A2A-task level) |
| request_handoff | request Markdown handoff | no dedicated endpoint; implemented as a dispatched instruction via `send_work` ("produce a Markdown handoff…") with the reply captured from run output/messages | 0.17.0 | **SUPPORTED (via send_work)** — degrade gracefully if run fails |
| set_session_title | set work-linked title | `PATCH /api/sessions/{id}` `{"title": "[ACMS-WORK-…] …"}` (verified live; allowed fields exactly `title`, `end_reason`) | 0.17.0 | **SUPPORTED** (live-tested) |

## Other live-verified facts (plan §14 test log)

| # | Test | Result |
|---|---|---|
| 1 | Version/commit | Hermes 0.17.0, commit f3d2dfbec670 (2026-06-30) |
| 2 | Status before execution | `/api/sessions` + `/v1/runs/{id}` give full inventory |
| 3 | Dispatch ACMS-keyed task | `POST /v1/runs` with metadata accepted (202, run_id immediate) |
| 4 | Title + session id | `session_id == run_id` for run submissions; title set via PATCH stuck ("[ACMS-WORK-000042] Bridge control live test") |
| 5 | Steering | Works into a running session via session chat (verified mid-run) |
| 6 | Stop/interrupt + continue | `/stop` → `cancelled`; same-session follow-up works; agent knows it was interrupted |
| 7 | True Pause/Resume | **NOT AVAILABLE** at run level (404). `/busy` queue/steer/interrupt exists; `/platform pause` is messaging-only. Do not fake. |
| 8 | `/new` behavior | `POST /api/sessions` creates explicit new sessions (verified `acms-explicit-session-01`) |
| 9 | Terminal cancel mapping | run-level `/stop` = terminal for the run; session reusable |
| 10 | Context telemetry sources | run status `usage` (input/output/total per run); session record `input_tokens/output_tokens/cache_*/api_call_count`; **context-window max NOT exposed** — ACMS must get max from LLM Manager model metadata or configured fallback (plan §6: never fabricate) |

## Notes / gaps for architecture review

- No native Pause/Resume at the agent-turn level (plan §5.2 respected:
  reported as unsupported rather than faked).
- `agent_running` is not directly exposed by api-server; derive from run
  status (`running` → true, else false) plus session `last_active` freshness.
- Heartbeat context fields: prefer run/session token accounting
  (`input_tokens` of latest turn) with `max_source` recorded; unknown max →
  `UNKNOWN/INVALID` per REQ-054.
- The installed stack is 0.17.0 (2026-06-30) — older than upstream docs I
  consulted; behavior verified against the installed build per plan §14.
- Upstream research inputs (unverified for this version) that matched the
  installed tree: `/status` prints Session ID/Title/Model/Tokens/Agent
  Running; `/busy steer` exists as a CLI-level steering path; gateway
  `agent.interrupt()` preserves session (code path verified in
  `run_agent.py` `interrupt()` + live stop test).
