# ADR-0009: Deterministic safe-release transaction with automated rollback

**Status:** Proposed
**Date:** 2026-09-26

## Context

The feature-delivery plan (2026-09-26) requires rapid, vertical feature slices
deployed to the live internal ACMS on MIAM-00135 CT122. The existing
`update.sh` path records the previous commit but does not create a pre-deploy
database backup, does not isolate writes during migration, and has no
automated rollback: a bad migration would require human diagnosis and manual
repair, which blocks the "live system becomes more useful after nearly every
deployment" cadence.

Production reconnaissance (2026-09-26) confirmed: repo at `/opt/acms/repo`
(detached HEAD `eab493f` = origin/main), Compose project `acms`, no `pg_dump`
on the CT host (must run inside the postgres container), no `curl` in the app
image, nginx allowlist blocks non-VM source IPs, and MARION has no
`home.arpa` DNS (raw IPs are the convention).

## Decision

Deployments run through a **safe release transaction** (`deploy/release.sh`):

1. **Preflight** — target must be an ancestor of approved `origin/main`;
   production tree clean; postgres healthy; current release healthy; disk
   headroom; required secret keys present (names checked, values never
   printed).
2. **Maintenance window** — nginx serves a 503 maintenance page and
   `acms-app` is stopped: no writes between the backup and acceptance.
3. **Verified backup** — `pg_dump -Fc` executed inside the postgres container
   to `/opt/acms/releases/backups/<release-id>.dump`; validated by size floor,
   `PGDMP` magic, and `pg_restore --list` parse; SHA-256 recorded.
4. **Deploy** — checkout the approved SHA, build the image tagged
   `acms-app:<git-sha-short>` with build identity baked in (`ACMS_BUILD_*`),
   `alembic upgrade head`, start the target app.
5. **Two-stage validation** — Stage `app` (direct container checks while
   maintenance is ON), then maintenance OFF and Stage `full` (HTTPS proxy
   path, UI login, auth denials, build-identity match through the proxy).
6. **Accept or auto-rollback** — on any validation failure the transaction
   (still open) authorizes Level 2: app rollback to the previous SHA +
   verified restore of the pre-deploy backup. `deploy/rollback.sh` also
   supports standalone (Level 1, app-only) and `<release-id>` targeting.

Release state (ledger `history.jsonl`, per-release metadata, backups) lives in
`/opt/acms/releases/` outside Git; secrets never enter release metadata. The
UI System page and `/version` report version + Git SHA + build time (plan §4);
a Git SHA is the authoritative deployed code identity.

## Consequences

- **Safer velocity:** every later merge can deploy through the same process;
  a bad release returns to the exact previous state deterministically.
- **Brief internal outage per deploy** (maintenance window) — accepted for MVP
  simplicity over zero-downtime complexity (plan §6).
- **Level-1 rollback limitation:** after a migration, application-only
  rollback runs old app on new schema; safe for additive migrations (plan §24
  expand/contract), dangerous otherwise — hence the transaction guard.
- **No automatic Alembic downgrade, ever** (plan §29); destructive migrations
  require human approval and restore testing (plan §24).
- Operator education: `update.sh` remains as a deprecated wrapper delegating
  to `release.sh`.

## References

- Feature-delivery plan 2026-09-26 §3–§11, §24, §28–§30
- ADR-0007 (MVP stack), ADR-0008 (server-rendered UI)
- ACMS-REQ-039 (authenticated/authorized agent control — release tooling is
  operator-side and never bypasses app authn/authz)
- Branch `ops/ACMS-safe-release-rollback`