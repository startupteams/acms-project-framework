# PR 10 — Feature Slice 2: Agent Detail + Background Routines (v0.5.0)

**Date:** 2026-09-26
**Branch:** `feat/ACMS-014-agent-detail-routines`
**Base:** `main` @ `137a008` (post-PR-9)
**Plan:** feature-delivery plan §14; **Requirements:** ACMS-REQ-005/008/014/023 (+008/036 history)

## 1. What shipped

`/ui/agents/{agent_id}` — read-only agent detail page:

- **Identity card:** ACMS ID, trust class, harness, bridge/protocol versions,
  card URL, registered/updated timestamps.
- **Capability manifest:** all 8 declared flags rendered honestly (declared vs
  not-declared), plus declared `extensions`.
- **Primary assignment card:** current ACTIVE assignment with link to the work
  item; explicit "no active primary assignment" state otherwise; the
  recorded-only delivery notice (same single constant as the Work UI) and an
  explicit heartbeat/liveness "not yet implemented" note (slice 3) — nothing
  fabricated (plan §14/§15; ACMS-REQ-046 discipline).
- **Assignment history** (all statuses, newest first, work-item links).
- **Execution-task history** for the agent (bridge task ids, statuses, times).
- **Handoffs** on every work item the agent has been assigned to.
- **Background routines / cron inventory** (ACMS-REQ-014): name, purpose,
  schedule, enabled, latest report + timestamp — with the standing note that
  ACMS records inventory but never schedules/authorizes executions.

Cross-links both ways: Agents list → detail (`/ui/agents/{id}`); Work detail
assignment table → agent detail. Footer/version rendering now shares
`_base_context` (previously the new page would have shown empty version/build).

## 2. Files

- `acms/ui/agent_routes.py` (new): detail route + view assembly; reuses
  `_base_context`, `DELIVERY_STATE`, `list_agents`, `work_service`.
- `acms/ui/templates/agent_detail.html` (new): cards + 4 sections, established
  badge/status/markdown/table conventions.
- `acms/ui/static/style.css`: `.caps/.cap-yes/.cap-no` manifest styles.
- `acms/ui/routes.py`: mount agent router; Home "not yet implemented" list
  corrected (primary assignment exists since Slice 1 — removed from the list).
- `acms/ui/templates/agents.html` + `work_detail.html`: cross-links.
- `acms/work_service.py`: `list_execution_tasks` gained additive optional
  `agent_id` filter (mirrors the Slice-1 `list_assignments` extension; no
  existing caller affected).
- `acms/__init__.py`: version → **0.5.0**.
- `SPRINT.md`: slices 1+2 checked; tooling + live-deployment sections closed
  with live-drill evidence.
- `tests/test_agent_detail_ui.py` (new, 7 tests).
- This handoff.

## 3. Validation

- **59/59 tests pass** (52 existing + 7 new: login gate, 404, full-history
  render incl. routine/task/handoff/capability honesty strings, idle-agent
  state, worker+observer read access, agents-list link, work-detail link).
- No schema change (no migration); no API behavior change except the additive
  optional filter param. Read-only UI — no mutation routes added.
- `ast`-level lint clean; write-channel corruption artifacts (2) caught and
  repaired via git diff before commit.

## 4. Known limitations (honest, per plan)

- Handoff authorship shows the agent UUID (no denormalized name on
  `work_handoffs`); improvement candidate with the audit backend (slice 6).
- Routine "latest report" is agent-reported free text — no verification.
- Liveness/heartbeat section intentionally absent until slice 3.

## 5. Deploy note

Merge → `deploy/release.sh <new-main-sha>` on CT122 (proven path; drill 3 + the
137a008 deploy). No migration → backup still taken, maintenance window still
applied per the transaction.
