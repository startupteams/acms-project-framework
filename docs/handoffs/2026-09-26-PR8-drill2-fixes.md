# ACMS release tooling — drill-2 fixes (tag stdout purity + health loop-tail)

**Branch:** `fix/ACMS-release-tooling-drill2` · **Base:** `main` @ `332f54d` · **Commit:** `cdab1b8` · **Tests:** 52 passed · **Live evidence:** CT122 drill 2026-09-26 23:08–23:11 UTC

## Context

Second live `release.sh` drill on CT122 (same-tip re-release, PR #6 tooling now on-box). The physical chain again worked end-to-end: verified backup (sha256 `86475ff…`), maintenance cycle correct (ON → re-apply after checkout → OFF, **proxy checks passed post-maintenance-off** — PR #6's directory-mount fix confirmed live), `wait_app_ready` (2s), Level-2 restore with `acms_old` forensics copy, revision check pass. Two residual script defects caused a spurious failure + exit 4; live service was recovered and is healthy at `332f54d` / v0.4.0.

## Fixes

| # | Defect (drill-2 evidence) | Fix |
|---|---|---|
| 1 | `build_release_image`'s own `info()` log line went to **stdout** inside the command substitution → tag polluted again (`invalid tag "acms-app:[2026-09-26T23:08:52Z] INFO Building…332f54d"`) | Absolute rule: **nothing on stdout but the tag** — compose build AND all log lines go to stderr. Also replaced `die()` with `return 1` inside the substituted function (a subshell `die` only kills the subshell, silently losing the failure signal) — the caller's `\|\| rollback_now` path now stays authoritative |
| 2 | `health_repeats`: `[ "$i" -lt 3 ] && sleep 2` as the loop's last statement returns 1 on the final iteration (false condition) even when all 3 healths passed → one false FAIL → false rollback-failure (same class as PR #6's `grep -c` pipefail bug) | Explicit `if` + explicit `return 0` |

## Drill-2 timeline (evidence)

- 23:05Z manual bootstrap hop → `332f54d` live (installs PR #6 tooling + Work UI v0.4.0). One intermediate issue: the proxy container still ran the old single-file spec until explicitly recreated under the new spec — resolved by `up -d reverse-proxy` with the image tag pinned; PR #6's `nginx_reload` + template naming then verified working in the drill.
- 23:08Z drill `release.sh 332f54d`: preflight OK → backup verified → maintenance cycle correct → build → tag pollution (bug 1) → auto-rollback → restore correct → full-stage validation: **11/12 checks passed**, single false FAIL (bug 2) → exit 4.
- 23:11Z recovery: transaction cleared, service verified (`/version` = `332f54d`, health ok, proxy checks pass).

## Reviewer focus

Both fixes are pure shell logic; no behavioral change beyond the defect removal. `return 1` vs `die` is the pattern to double-check in `build_release_image` callers (`release.sh` Phase C wraps with `\|\| rollback_now`; `rollback.sh` rebuild path assigns via command substitution — failure now propagates correctly).

## Validation

- `pytest -q` → 52 passed
- `bash -n` on all 5 deploy scripts
- Live CT122 healthy at `332f54d`, image `acms-app:332f54d`, DB rev `0002`, transaction cleared, ledger updated

## Next

After merge: **drill 3** — run `release.sh 332f54d` once more; with these two fixes it should reach `Release ACCEPTED` end-to-end (same tip, no code change, ~3-min window, auto-rollback armed). Then Work UI is live and every future merge deploys through the proven pipeline.
