# ACMS PR 0 — safe release transaction + automated rollback (plan §3–§11)

**Branch:** `ops/ACMS-safe-release-rollback` · **Base:** `main` @ `eab493f` · **Commit:** `650d6f8` · **Tests:** 42 passed (38 pre-existing + 4 new) · **Secret scan:** clean

Infrastructure only — no product features. Implements PR 0 of the
2026-09-26 feature-delivery plan so every later merge can deploy through a
process that a bad release cannot strand.

## What ships

| Area | Change |
|---|---|
| `deploy/common.sh` (new) | Verified pre-deploy backups (`pg_dump -Fc` run inside the postgres container; size floor + `PGDMP` magic + `pg_restore --list` parse + SHA-256), maintenance mode (nginx 503 page + app stop, checkout-safe re-apply), release ledger `history.jsonl` + per-release metadata outside Git, preflight (approved-main ancestry, clean tree, health, disk, secret-key presence — names only), image build tagged `acms-app:<git-sha-short>` with baked build identity |
| `deploy/release.sh` (new) | Safe release transaction: Phase A preflight → Phase B preserve (backup + transaction record) → Phase C deploy (checkout approved main SHA, `alembic upgrade head`, start SHA-tagged image) → Phase D two-stage validation; **any failure auto-invokes `rollback.sh --in-flight`** (plan §28 decision tree) |
| `deploy/rollback.sh` (new) | Level 1 standalone (application-only; never touches DB, never runs downgrade) and Level 2 in-flight (transaction-guarded app+DB restore; restore goes to `acms_restore_tmp` first, live DB kept as `acms_old` for forensics; refuses unknown releases, failed checksums, restores outside an open transaction) |
| `deploy/validate-release.sh` (new) | Repeated `/health` (×3, direct), DB connectivity, Alembic revision check, restart count, log scan, auth denials, build-identity match; `--stage app` runs under maintenance, `--stage full` via HTTPS proxy; optional `--feature-smoke` (plan §11) |
| `deploy/smoke-test.sh` | Rewritten for live reality: raw-IP base URL (MARION has no home.arpa DNS), `-k` for self-signed TLS, direct + proxy + build-identity + Docker health checks |
| `deploy/update.sh` | Deprecated wrapper → delegates to `release.sh` (same muscle memory, safe path) |
| Build identity (plan §4) | `acms/build_info.py` + `/version` returns `{version, git_sha, git_sha_short, build_time, reported_at}`; System UI page shows Git SHA + build time + release-records paths; Home card + footer show short SHA; `unknown` never fabricated |
| `deploy/Dockerfile` + `compose.yaml` | `ACMS_BUILD_GIT_SHA`/`ACMS_BUILD_TIME` build args; `image: acms-app:${ACMS_APP_IMAGE_TAG:-local}` |
| `nginx.maintenance.conf` (new) | Same allowlist, every request → 503 maintenance page |
| Docs | **ADR-0009 (Proposed)** — safe release transaction; `SPRINT.md` replaced per plan §23; FW-015 (feature flags) + FW-016 (maintenance watchdog) in FUTURE_WORK.md; handoff `docs/handoffs/2026-09-26-PR0-safe-release-rollback.md` |
| Tests | `tests/test_build_info.py` (4): /version identity, unknown-fallback, health unchanged, System-page context. Suite: **42 passed** |

## Live-validated design constraints (plan §12 sync check, 2026-09-26)

Recon of CT122 confirmed **production == origin/main == `eab493f`** (DB rev `0002_work_orchestration`, clean detached HEAD) — no sync deploy needed before this PR. Recon also fixed the tooling's assumptions: repo lives at `/opt/acms/repo`; no `pg_dump` on the CT host (exec inside the postgres container); no `curl` in the app image (python urllib instead); nginx allowlist blocks non-VM source IPs (validation uses the VM's own IP); MARION has no home.arpa DNS (raw IPs). **No deploy was performed in this PR.**

## Reviewer focus

1. **ADR-0009** — accept the safe-release transaction + maintenance-window model (plan §3: rollback on failed validation is pre-authorized; anything beyond it stays human-gated).
2. **Level-2 restore sequence** — `pg_restore` to `acms_restore_tmp`, swap with rename keeping the old DB as `acms_old`, only then start the previous app. Confirm you're comfortable with the drop/rename swap under maintenance mode.
3. **Standalone rollback is app-only** — DB restore is *refused* outside an open transaction even if a human runs `rollback.sh` manually (plan §8 guards); human-directed DB restore needs explicit instruction. Confirm this matches your intent.
4. **Maintenance window** — every deploy has a brief 503 window (plan §6 accepts this over zero-downtime).

## Validation (this workstation; no deployment)

- `pytest -q` → **42 passed** (38 pre-existing + 4 new) in `.venv-acms` (Python 3.12)
- `bash -n` on all 5 deploy scripts → OK; compose YAML parsed; maintenance conf carries the `ACMS maintenance` marker
- Secret scan of the full diff → clean (placeholders only)
- Docker is unavailable on this workstation (known env constraint): scripts are validated for syntax + logic only; the live drill (plan §31) happens after merge + your deployment authorization

## Risks

- First live run is the real test of the backup/restore path (deliberate drill recommended — plan §31).
- Level-1 rollback after a schema-changing release runs old app on new schema — acceptable under plan §24 expand/contract, and Level 2 covers the rest.
- If a transaction aborts between maintenance-on and maintenance-off (power loss), ACMS stays in maintenance until a human or the FW-016 watchdog intervenes.

## Next after merge + deployment authorization

Deploy this to CT122 via `deploy/release.sh`, drill the rollback, then Feature Slice 1 — Work Management UI (`feat/ACMS-007-work-management-ui`, plan §13).