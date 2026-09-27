# CO-WORK HANDOFF — ADR-0009 decision + next planned work
**Date:** 2026-09-26 (late) · **Prepared for:** Jordan + agent co-work session
**Repo:** `startupteams/acms-project-framework` · **Prod:** MIAM-00135 CT122 (10.0.20.122)
**Your session:** log in at `https://10.0.20.122/ui/` (LLDAP `jordatech`, Administrator)

---

## 1. Current state (verified live, 2026-09-26 ~23:55Z)

| Item | State |
|---|---|
| Production | `9545123` · **v0.5.0** · healthy, 3 containers, rev `0002_work_orchestration` |
| Release pipeline | **Routine**: merge → `deploy/release.sh <main-sha>` → smoke. 2 clean pipeline deploys (137a008, 9545123) + drills 1–3 |
| Backups | 8 verified dumps (PGDMP + sha256), newest `20260926T234743Z-137a008.dump` |
| Ledger | 14 lines in `/opt/acms/releases/history.jsonl`, no open transaction |
| Tests | 59/59 passing |
| UI live | Work UI (`/ui/work`), Agent Detail (`/ui/agents/{id}`), System (`/ui/system`) |

**Sprint scoreboard:** tooling ✅ live-proven · Slice 1 (Work UI) ✅ live · Slice 2 (Agent Detail) ✅ live · Deploy-gate section ✅ closed. Remaining: Slice 3 (heartbeat, ACMS-side), ADR-0009 acceptance, optional live-rollback demo.

## 2. The decision on the table — ADR-0009

**File:** `docs/adr/0009-safe-release-transaction.md` — **Status: Proposed**

It specifies the mechanism you have been *using* since drill 1: safe-release
transaction (backup → maintenance → migrate → validate → accept), auto-rollback
Levels 0–3 with guards, refuse-to-restore rules, history outside Git.

**Live evidence accumulated since it was drafted:**

| Evidence | Result |
|---|---|
| Drill 1 (PR 0 tooling, live CT122) | 6 bugs found → PR #6 fixed |
| Drill 2 (fixed tooling) | 2 residual bugs → PR #8 fixed; Level-2 auto-rollback **physically correct** both times (checksum-verified restore, `acms_old` forensics copy, data intact) |
| Drill 3 | First clean end-to-end `Release ACCEPTED` |
| Deploys #4, #5 (137a008, 9545123) | Routine pipeline deploys, zero intervention |
| Smoke-test fix (PR #9) | Found in post-drill sweep, not a release gate |

**The decision:** accept ADR-0009 as-is, accept with amendments (e.g. codify
"deploy = human-authorized `release.sh` run" explicitly), or hold. **Nothing
blocks on this** — the mechanism runs either way; acceptance makes it policy
rather than proposal. My recommendation: **accept with one amendment** — add
the evidence table above as the rationale record.

## 3. Next planned work (plan §15) — Slice 3: heartbeat + live status

**Scope as planned:** last contact, liveness states
(`HEALTHY / IDLE / WORKING / STALE / UNREACHABLE / UNKNOWN`), bridge health,
configurable stale thresholds, active reconciliation after missed contact.

**Sequencing nuance (flagged in agents.md):** the *agent-side sender* only
exists once the A2A bridge lands (slice 4). So slice 3 ships ACMS-side only:

1. `POST /api/v1/agents/{id}/heartbeat` (bearer-gated, REQ-039 gating as today)
2. `heartbeats` table (last_contact, state, source) — **schema change** → expand/contract per plan §24, backward-compatible add-only
3. Liveness derivation: UNKNOWN (never seen) → states per §15; STALE via configurable `ACMS_HEARTBEAT_STALE_SECONDS`
4. Agent Detail page: "Last contact" card + derived state — **only when the data exists**; agents that never send stay UNKNOWN, nothing fabricated
5. New tests + smoke check; migration validated old-app→new-migration→restore path per §27

**Slice 4 preview (A2A delivery/steering):** recorded→delivered→accepted
states on assignments, capability-gated controls (send/steer/pause/resume/
interrupt/cancel), bridge protocol-version compatibility per plan §26. This is
the slice where ACMS becomes an actual control plane rather than a management
database.

## 4. How to drive the co-work session

- You provide the plan file (markdown) → we execute it exactly like this
  session: plan → branch → PR → **you merge** → I deploy via `release.sh` → smoke.
- I never self-merge; deploys need your explicit go; failed-validation
  rollbacks stay pre-authorized per plan §3.
- Any surprise/pivot → logged to `agents.md` per the Surprise Protocol.

## 5. Session-checklist (fast catch-up for you)

1. Skim ADR-0009 (`docs/adr/0009-*.md`) + the evidence table in §2 above → decide.
2. Skim plan §15 + my slice-3 sequencing note → confirm or reshape.
3. Drop your markdown plan file for the next execution block.
4. (Optional, ~3 min of 503) Live rollback demo: run `release.sh` on a stale tip and watch auto-rollback self-restore.

---
*Prepared by the ACMS implementation agent. All facts verified live at write time.*
