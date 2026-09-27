# Current Sprint — ACMS useful for human work management + safe releases

## Sprint Goal

Make the live ACMS useful for human work management while establishing a
deterministic, AI-agent-executable rollback mechanism.

(Replaces the foundation sprint, which completed through PR #4 and the first
live deployment on MIAM-00135 CT122, 2026-09-26.)

## Features in Scope

### 1. Safe release / rollback tooling (PR 0 — infrastructure)

**Branch:** `ops/ACMS-safe-release-rollback`
**Plan:** feature-delivery plan 2026-09-26 §3–§12, §24, §28–§30

- [x] Pre-deploy verified PostgreSQL backup (plan §5)
- [x] Maintenance window during release transactions (plan §6)
- [x] Safe release transaction `deploy/release.sh` (plan §7)
- [x] Deterministic rollback `deploy/rollback.sh` Levels 0/1/2 + guards (plan §8/§9)
- [x] Automated rollback on failed validation (plan §10)
- [x] Two-stage post-deploy validation (plan §11)
- [x] Build identity: `/version` + System UI expose Git SHA/build time (plan §4)
- [x] Release records outside Git (`/opt/acms/releases/`) (plan §4)
- [x] SHA-tagged images (`acms-app:<git-sha-short>`)
- [x] ADR-0009 (Proposed)

(Live-proven on CT122 2026-09-26: drills 1–3; drill 3 = first clean end-to-end
`Release ACCEPTED`; Level-2 auto-rollback chain exercised twice on real data.)

### 2. Work Management UI (feature slice 1)

**Branch:** `feat/ACMS-007-work-management-ui`
**Plan:** feature-delivery plan §13

- [x] `Home / Agents / Work / System` navigation
- [x] `/ui/work`: list/filter/create/edit Work Items; hierarchy; scope view
- [x] Primary-assignment actions (assign/close/release/suspend) — Administrator
- [x] Assignment status distinct: Recorded / Delivered / Accepted (no A2A yet)
- [x] Role authorization: admin mutate, worker read permitted work, observer read-only
- [x] Execution tasks / handoffs / background routines visible per work item

(Live on CT122 since 2026-09-26 — Work UI serving at `/ui/work`.)

### 3. Agent Detail + background routines (feature slice 2)

**Branch:** `feat/ACMS-014-agent-detail-routines`
**Plan:** feature-delivery plan §14

- [x] `/ui/agents/{agent_id}`: identity, trust class, harness, capability manifest
- [x] Assignment/execution-task history, handoffs, background routines inventory

### 4. Live deployment after human approval

- [x] Merge PR 0 after human review
- [x] Human authorizes deployment of the safe-release tooling to CT122
- [x] Deploy via the new release transaction; verify rollback tooling on the live box
- [x] Document exact live SHA + release records

(CT122 reached via bootstrap hops to 650d6f8 / 332f54d / 363cf3c, then the first
full through-the-pipeline deploy of 137a008 ended in `Release ACCEPTED`. Every
future merge deploys through `deploy/release.sh`.)

## Explicitly out of scope for this sprint

- A2A delivery/steering control (slice 4) unless the above complete cleanly.
- Heartbeat/liveness (slice 3), live events (slice 5), audit/attention (slice 6).
- React/Next.js UI replacement (ADR-0008 stays; plan §22).
- Cost/performance integration (slice 9; needs LLM Manager metric contracts).

## Definition of Done

- [ ] Requirement IDs traced for every PR (`ACMS-REQ-###`).
- [ ] Tests/checks pass, including migration-compatibility validation where schema changes.
- [ ] No secrets committed; release records stay outside Git.
- [ ] Markdown handoff per PR with live smoke results where deployed.
- [ ] Human review before every merge; agents do not self-merge.
- [ ] Deployment only after explicit human authorization; failed-validation
      rollbacks are pre-authorized (plan §3).

## Risks / Blockers / Human Decisions Needed

- ADR-0009 (safe release transaction) needs human acceptance.
- Level-2 DB restore policy is validated only by review + live drill after
  deployment authorization (plan §31 recommends verifying the rollback tooling).
- ADR-0008 UI stays Jinja2 while slices land quickly.

## Sprint Handoff

Record shipped requirements, validation results, accepted/rejected ADR
choices, unresolved work, and the recommended next PR at sprint completion.
---

## Combined Slice 3+4 — heartbeat, correlation, reconciliation, A2A control (2026-09-26 plan)

**Branch:** `feat/ACMS-031-agent-bridge-live-control` · **ADR-0010 (Accepted)** · ADR-0009 accepted with amendments

- [x] ADR-0009 Accepted (amended with live evidence + deployment authority policy)
- [x] ADR-0010 (heartbeat/reconciliation/control defaults) Accepted
- [x] ACMS-REQ-052 human-readable Work/Assignment keys (durable counter allocation)
- [x] Heartbeat ingestion (acms-heartbeat-v1, complete snapshot per 60 s contract)
- [x] Connectivity derivation (UNKNOWN/HEALTHY/STALE/UNREACHABLE; 5 min / 1 h / 24 h thresholds, config-backed)
- [x] Semantic event log (ordered, correlation-bearing, change-only)
- [x] Context telemetry + warnings (70/85/95 configurable; invalid → UNKNOWN/INVALID, never fabricated)
- [x] Hermes harness research + live control tests (HARNESS_CONTROL_MAPPING.md — pause/resume verified UNSUPPORTED, never faked)
- [x] Hermes Agent Bridge module (status/send_work/steer/interrupt/cancel/handoff/title) + live E2E
- [x] UI: Agent Detail live status + events + capability-honest controls; Work keys in views
- [ ] Bridge dispatch/control UI buttons wired to live bridge targets (deploy-side config)
