# TECH SPIKE — Agent State Local Redundancy (Phase D3, 2026-09-29)

**Question:** the human wants a **local copy** of agent state in addition to cloud Git
(`startupteams/hermes-base-setup`, currently the sole durable copy of worker Hermes state).
**Status:** recommendation only — no service deployed from this spike.

## Current state (measured)

- Worker state sync today: VM124 → branch `agent/22ac1b19…` on `startupteams/hermes-base-setup`
  (deploy key, hourly `hermes-state-sync.timer`, secret-scanning fail-closed; ARM records
  `last_state_commit_sha` + `state_sync_health`).
- Failure modes of the cloud-only design: GitHub outage/billing/lockout; push-protection false
  positives blocking syncs; org-level policy changes; network egress failure — worker state then
  has no local fallback.

## Options compared

| Criterion | A. Bare Git mirror (LAN) | B. Forgejo/Gitea (LAN) | C. Object storage snapshots | D. FS/ZFS snapshots | E. Hybrid (B + A) |
|---|---|---|---|---|---|
| Recovery of a dead worker | good (clone over LAN) | good (clone over LAN) | fair (restore tooling needed) | fair (whole-VM only) | good |
| Version history | full | full | point-in-time only | point-in-time | full |
| Human inspection | git CLI | web UI + git | tools | tools | web UI + git |
| Per-agent isolation | branch/path per agent | repo per agent (clean) | prefix keys | n/a | repo per agent |
| Large files | poor (bloats history) | LFS-ish, still git | good | good | git + LFS |
| Secrets risk | same as git (scan needed) | same + UI exposure | same | same | same |
| Search | grep | built-in code search | object tools | none | built-in |
| Maintenance | cron + fsck | one service (updates, backups) | minio/backup cron | PBS already exists | one service + cron |
| Local/cloud redundancy | yes (mirror of GitHub) | yes (push to both) | separate copies | separate copies | yes |
| New infra to run | none | one LXC (CT-class) | one service | none (PBS exists) | one LXC |

## Recommendation

**E — Hybrid, in this order:**

1. **Now (zero new infra):** nightly bare `git mirror` of `hermes-base-setup` (+ its worker
   branches) onto the PBS-backed host (e.g. MIAM-00147 bulk storage), alongside the existing PBS
   guest backups. A mirror script is ~20 lines (`git clone --mirror` + `git remote update` in a
   systemd timer). This closes the "GitHub unavailable" gap immediately.
2. **When agent count grows (~5+ workers) or humans want a UI:** stand up **Forgejo** in one LXC,
   add it as a **second push remote** in `hermes-state-sync` (push to GitHub AND Forgejo). Repo-per-
   agent keeps isolation and per-agent access control clean; code search helps inspection; the
   mirror of GitHub remains the simple fallback.
3. **Do not use FS/ZFS snapshots as the primary** — they protect whole disks, not per-agent state
   semantics, and PBS already covers VM-level recovery. Keep as the disaster layer it already is.

## Explicitly NOT recommended

- Object storage (MinIO) as canonical agent-state store: no git semantics, extra service to run,
  weaker human inspection.
- Storing secrets in any of these: the state-sync secret scan stays mandatory regardless of target.

## Follow-ups (not executed)

- Mirror script + timer (small PR to hermes-base-setup `scripts/`).
- Forgejo evaluation LXC when triggered (needs human approval for new service).
