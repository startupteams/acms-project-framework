# TECH SPIKE — Git-backed Agent State vs Purpose-built Agent-Workspace/State Model (Phase D4, 2026-09-29)

**Human concern:** source code and agent memory may deserve **separate storage semantics**.
**Status:** analysis + recommendation; no implementation.

## What "agent state" actually contains today (worker Hermes, per VM124)

| Content class | Examples | Size/dynamics | Semantics wanted |
|---|---|---|---|
| Conversational history | sessions.db, transcripts | MB–GB, append-heavy, private | durable, searchable, **not** diff-friendly |
| Memory / profile | MEMORY.md, USER.md, SOUL.md | KB, human-edited, opinionated | versioned, reviewable, mergeable |
| Skills / procedures | skill dirs | static-ish, reusable | versioned, reviewable |
| Config / identity | config.yaml (excl. secrets), machine ids | tiny, security-sensitive | versioned, audited |
| Cache/bin/locks | bin/, cache/, locks | regenerable | **not persisted** (already excluded) |

Git is excellent for the middle rows (docs/memory/skills/config) and **poor** for conversation
history (binary-ish DB blobs, merge conflicts, repo bloat) and **irrelevant** for cache.

## Options

**A. All-in Git (current):** one repo, whitelisted paths per profile.
- Pros: one mechanism, full history, PR-reviewable memory edits, already working + secret-scanned.
- Cons: DB files in git (state.db snapshots) are opaque; history bloat; restore = coarse.

**B. Purpose-built durable agent-workspace service:** a small "Agent State Store" (Postgres +
object store) with typed records: `agent_memory`, `skill`, `session_ref`, `checkpoint`.
- Pros: typed semantics, per-record retention, queryable (ACMS could expose memory search), no
  bloat; sessions stay out of git.
- Cons: NEW service to build/operate; loses PR-review of memory changes unless a UI is built;
  duplicates concerns ACMS memory-offload already owns (execution sessions/context packages).

**C. Split semantics (recommended):** git for **reviewable artifacts** (memory, skills, config);
non-git durable store for **volumetric/append data** (session archives) — which already exists:
ACMS memory offload (`execution_sessions`, `context_packages`, `session_checkpoints`, migration 0006)
+ LiteLLM/LLM Manager request metadata. The worker's own `sessions.db` becomes **regenerable-ish**:
its durable essence (checkpoints, handoffs, context packages) is ACMS-owned; raw transcripts can go
to compressed archive storage (PBS/backup tier) rather than git history.

## Recommendation

**C — split semantics, reusing what exists:**

1. Git (hermes-base-setup) keeps: MEMORY/USER/SOUL, skills, sanitized config, identity — the
   "reviewable brain". Unchanged.
2. Sessions/transcripts: stop treating the worker's raw `sessions.db` as the durable copy. Durable
   continuity already lives in **ACMS memory offload** (checkpoints/context packages — REQ-055/056/
   057). Raw archives: optional nightly compressed snapshot to backup storage (NOT git), retention
   per backup policy.
3. A future "Agent State Store" service is **not justified now** — ACMS + git already cover the two
   semantic classes; building the third system would duplicate both. Revisit only if per-record
   memory queries become a product need (e.g. cross-agent skill search at fleet scale).

## Relationship to the ownership boundary (Phase D2)

- **ARM (Server Manager/Agent Runtime Manager)** owns agent state-SYNC health
  (`last_state_commit_sha`, `state_sync_health`) — machine plane.
- **ACMS** owns work-scoped continuity (execution sessions, checkpoints, handoffs) — work plane.
- **Git/hermes-base-setup** owns the reviewable agent brain (memory/skills/config).
- ACMS consumes ARM's summarized health signal for display (already the case: `runtime_api.py`
  reads live from SM; no ACMS-side duplicate authoritative state exists — audit 2026-09-29).
