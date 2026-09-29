# Agent-State Ownership Boundary (Phase D2, 2026-09-29)

**Human direction:** *Server Manager / Agent Runtime Manager owns agent state-sync health; ACMS
should not manage implementation details of Git/cloud backup.*

## Audit result (2026-09-29)

| Data | Where stored today | Authoritative owner | ACMS role |
|---|---|---|---|
| `last_state_commit_sha`, `state_sync_health`, `hermes_state_branch` | Server Manager DB (`agent_runtimes`) | **ARM** | **Consumer only** — `runtime_api.py` reads live from SM API; `agent_detail.html` renders read-only |
| Git backup mechanics (deploy keys, sync timer, secret scan) | VM124 (`hermes-state-sync`) + hermes-base-setup repo | **ARM + worker** | None (never touches) |
| Work-scoped continuity (execution sessions, context packages, checkpoints) | ACMS DB (migration 0006) | **ACMS** | Owner |
| PR/branch economics provenance (`repository`, `branch` on pr_outcomes) | ACMS DB (0008) | ACMS | Owner — work-artifact provenance, NOT state-sync |

**Finding: no duplicate authoritative sync state exists in ACMS** — ACMS already consumes the
summarized signal from ARM at read time. No data migration, no deletion, no code change required.
The only correction needed is this documented contract.

## Contract (documented, non-breaking)

1. **ARM is authoritative** for: state-sync health vocabulary (`PENDING | SYNCING | VERIFIED |
   STALE | FAILED`), last commit sha, branch, and the backup mechanism itself.
2. **ACMS may consume a summarized health signal** for operational display (it does, live).
3. **ACMS must not** implement Git/cloud backup details, hold a second mutable copy of sync state,
   or gate work transitions on backup state (attention surfacing of `STATE_SYNC_FAILED` remains the
   integration point).
4. Secrets: neither system stores backup credentials in the other's DB (deploy keys stay on the
   worker; tokens in each service's own secret store).
