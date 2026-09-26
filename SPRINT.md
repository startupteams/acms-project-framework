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

- [ ] Pre-deploy verified PostgreSQL backup (plan §5)
- [ ] Maintenance window during release transactions (plan §6)
- [ ] Safe release transaction `deploy/release.sh` (plan §7)
- [ ] Deterministic rollback `deploy/rollback.sh` Levels 0/1/2 + guards (plan §8/§9)
- [ ] Automated rollback on failed validation (plan §10)
- [ ] Two-stage post-deploy validation (plan §11)
- [ ] Build identity: `/version` + System UI expose Git SHA/build time (plan §4)
- [ ] Release records outside Git (`/opt/acms/releases/`) (plan §4)
- [ ] SHA-tagged images (`acms-app:<git-sha-short>`)
- [ ] ADR-0009 (Proposed)

### 2. Work Management UI (feature slice 1)

**Branch:** `feat/ACMS-007-work-management-ui`
**Plan:** feature-delivery plan §13

- [x] `Home / Agents / Work / System` navigation
- [x] `/ui/work`: list/filter/create/edit Work Items; hierarchy; scope view
- [x] Primary-assignment actions (assign/close/release/suspend) — Administrator
- [x] Assignment status distinct: Recorded / Delivered / Accepted (no A2A yet)
- [x] Role authorization: admin mutate, worker read permitted work, observer read-only
- [x] Execution tasks / handoffs / background routines visible per work item

### 3. Agent Detail + background routines (feature slice 2)

**Branch:** `feat/ACMS-014-agent-detail-routines`
**Plan:** feature-delivery plan §14

- [ ] `/ui/agents/{agent_id}`: identity, trust class, harness, capability manifest
- [ ] Assignment/execution-task history, handoffs, background routines inventory

### 4. Live deployment after human approval

- [ ] Merge PR 0 after human review
- [ ] Human authorizes deployment of the safe-release tooling to CT122
- [ ] Deploy via the new release transaction; verify rollback tooling on the live box
- [ ] Document exact live SHA + release records

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