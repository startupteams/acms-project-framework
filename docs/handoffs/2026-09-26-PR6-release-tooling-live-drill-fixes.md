# ACMS release tooling — 6 fixes from the first live drill

**Branch:** `fix/ACMS-release-tooling-live-drill` · **Base:** `main` @ `266d0ed` · **Commit:** `48c444e` · **Tests:** 42 passed · **Live evidence:** CT122 drill 2026-09-26 22:11–22:31 UTC

## Context

PR #5's tooling was deployed to CT122 via a manual bootstrap hop (verified backup → stop → checkout → build → migrate → validate → record, ~2 min window). The first real `release.sh` run then **failed correctly and auto-rolled-back** — proving the Level-2 chain live (checksum-verified backup → DB restore to `acms_restore_tmp` → swap with `acms_old` forensics copy → image swap → revision check) — but exposed 6 tooling defects. Live service was fully recovered to `650d6f8` and healthy; this PR fixes the defects so the release transaction itself works.

## Fixes

| # | Defect (live) | Fix |
|---|---|---|
| 1 | `build_release_image` command-substitution captured the compose **build log** into the tag → `invalid tag "acms-app:<68-line log>"` → spurious deploy failure | Build stdout → stderr; tag computed, never substituted |
| 2 | No readiness wait: validation ran ~1s after container start → false `repeated /health` FAIL → false rollback-failure | `wait_app_ready()` (60s cap, `ACMS_APP_READY_SECONDS`) gates Phase C and both rollback paths |
| 3 | `app_log_error_scan` pipefail false-positive (`grep -c` exits 1 on zero matches) | Count captured explicitly, numeric compare |
| 4 | **nginx single-file bind mount pinned the inode**: after `maintenance_off` restored the file on disk, HUP kept serving the stale 503 config indefinitely | Directory mount `./reverse-proxy:/etc/nginx/conf.d` (in-place edits visible to container); maintenance conf renamed `*.template` (excluded from conf.d include glob); `nginx -t` guard before every reload; fallback = single-container restart, never `up -d` (depends_on once recreated acms-app with the stale `:local` image — caught and reverted); release.sh now reconciles proxy spec after maintenance-off |
| 5 | rollback.sh Level-2 (re)started the app before the DB swap (writes-during-restore hazard) | Strict order: stop → terminate connections → restore temp DB → swap (`acms_old` forensics) → checkout previous SHA → start previous image → `wait_app_ready` → validate |
| 6 | (hardening) maintenance re-apply across checkouts | Verified live during checkout hop; template path constant updated |

## Live-drill timeline (evidence)

- 22:11Z dry-run: pg_dump 15.8 KB, magic/parse OK, restore drill into temp DB OK (1 agent row), app exec helper 200 — zero production impact.
- 22:15Z bootstrap hop → PR 0 live at `650d6f8`: `/version` reports full build identity; all proxy checks green.
- 22:16Z first real `release.sh 266d0ed` → **build-tag bug → auto-rollback invoked** → backup verified (sha256 `09c8cc…`) → DB restored → previous image up → **false validation failures** (bugs #2/#3/#4) → script exited 4 "requesting human direction".
- 22:19–22:31Z diagnosis + recovery: proxy recreated (inode fix confirmed), transaction cleared, app re-pinned to `acms-app:650d6f8` (the `depends_on` recreation had briefly put back `:local` — caught via `/version` identity check), DB verified intact (rev `0002`, 1 agent, 0 work items).

## Reviewer focus

1. Compose volume change is a **recreate-requiring change** — it lands on the next release (this PR, once merged + authorized), which is exactly when release.sh reconciles the proxy.
2. `wait_app_ready` default 60s — acms-app boots in ~3s; generous but finite; if exceeded the transaction stays open and rollback paths remain available.
3. Level-2 restore still never runs Alembic downgrade and never serves writes during restore.

## Validation

- `pytest -q` → 42 passed (`.venv-acms`, Python 3.12)
- `bash -n` all scripts OK; compose YAML parsed OK
- No secrets in diff; production untouched since recovery (live = `650d6f8`, healthy, correct identity)

## Next

After merge + deployment authorization: run the fixed `release.sh 266d0ed` (proxy spec lands), verify acceptance, then Feature Slice 1 — Work Management UI.
