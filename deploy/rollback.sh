#!/usr/bin/env bash
# rollback.sh — ACMS deterministic rollback (feature-delivery plan §8/§9/§29).
#
# Usage:
#   deploy/rollback.sh                 # back to immediately previous known-good
#   deploy/rollback.sh <release-id>    # to a specific recorded release
#   deploy/rollback.sh --in-flight     # internal: release.sh auto-rollback path
#
# Levels (plan §8) — the script chooses the lowest reliable level:
#   0  service restart (transient failure, same code/schema)
#   1  application-only rollback (previous image/SHA; no downgrade)
#   2  application + database restore from the verified pre-deploy backup
#      (used when the transaction is still open, i.e. validation failed)
#   3  PBS disaster recovery — out of scope here; see deploy/README.md.
#
# Guards (plan §8/§29): refuse without a checksum-verified backup belonging to
# the current transaction; never serve writes during restore; never guess;
# never blind-downgrade; never hide a failed rollback.

set -euo pipefail

DEPLOY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$DEPLOY_DIR/common.sh"

IN_FLIGHT=0
TARGET_RELEASE=""
case "${1:-}" in
  --in-flight) IN_FLIGHT=1; shift ;;
  "") ;;
  *) TARGET_RELEASE="$1" ;;
esac
[ $# -eq 0 ] || die "unexpected arguments after $1"

# ---------- argument sanity ----------
if [ -n "${TARGET_RELEASE:-}" ]; then
  [ -f "$(release_meta_path "$TARGET_RELEASE")" ] || die "unknown release id: $TARGET_RELEASE (plan §29: never guess)"
fi

# ---------- discover source of truth ----------
CURRENT_SHA="$(repo_git_sha)"
CURRENT_REV="$(db_alembic_revision || true)"
PREV_RELEASE="$(previous_release_id)"
PREV_META_EXISTS=0
if [ -n "$PREV_RELEASE" ] && [ -f "$(release_meta_path "$PREV_RELEASE")" ]; then
  PREV_META_EXISTS=1
fi

info "=== ACMS rollback ==="
info "current:  sha=$CURRENT_SHA alembic=${CURRENT_REV:-unknown} release=$(current_release_id)"
info "previous: release=${PREV_RELEASE:-none}"

# ---------- Level selection ----------
# In-flight transaction (validation failed mid-release) → Level 2 is mandatory:
# the transaction guards prove the backup belongs to THIS deployment attempt.
if [ "$IN_FLIGHT" -eq 1 ]; then
  transaction_active || die "--in-flight but no transaction record — refuse (plan §29)"
  BACKUP_PATH="$(sed -n 's/.*"backup_path"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$ACMS_TRANSACTION_FILE" | head -1)"
  BACKUP_SHA="$(sed -n 's/.*"backup_sha256"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$ACMS_TRANSACTION_FILE" | head -1)"
  PREV_SHA="$(sed -n 's/.*"previous_git_sha"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$ACMS_TRANSACTION_FILE" | head -1)"
  OLD_REV="$(sed -n 's/.*"old_alembic_rev"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$ACMS_TRANSACTION_FILE" | head -1)"
  [ -n "$PREV_SHA" ] || die "transaction record missing previous_git_sha — refuse"
  info "in-flight rollback: target sha=$PREV_SHA, DB restore authorized (transaction open)"

  # plan §8-L2 guard: valid backup, checksum, belongs to current transaction.
  backup_checksum_matches "$BACKUP_PATH" "$BACKUP_SHA" \
    || die "pre-deploy backup failed checksum/magic validation — refusing restore (plan §8)"
  info "backup verified: $(basename "$BACKUP_PATH") (sha256 $BACKUP_SHA)"

  maintenance_active || maintenance_on
  # plan §8-L2: record failed state for the handoff, then restore.
  FAILED_SHA="$CURRENT_SHA"
  FAILED_REV="$CURRENT_REV"

  # Level 2 sequence (plan §8):
  "${COMPOSE[@]}" stop acms-app >/dev/null 2>&1 || true
  info "terminating active ACMS database connections"
  "${COMPOSE[@]}" exec -T postgres psql -U acms -d acms \
    -c "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname='acms' AND pid <> pg_backend_pid()" >/dev/null
  info "recreating ACMS database from pre-deploy backup"
  "${COMPOSE[@]}" exec -T postgres psql -U acms -d postgres \
    -c "DROP DATABASE IF EXISTS acms_restore_tmp" >/dev/null
  "${COMPOSE[@]}" exec -T postgres psql -U acms -d postgres \
    -c "CREATE DATABASE acms_restore_tmp OWNER acms" >/dev/null
  if ! cat "$BACKUP_PATH" | "${COMPOSE[@]}" exec -T postgres pg_restore -U acms -d acms_restore_tmp --no-owner --exit-on-error; then
    die "pg_restore failed — DB untouched in acms; requesting human direction (plan §3)"
  fi
  # Swap: drop live acms (schema may be mid-migration/untrusted), rename tmp.
  "${COMPOSE[@]}" exec -T postgres psql -U acms -d postgres \
    -c "DROP DATABASE IF EXISTS acms_old" >/dev/null
  "${COMPOSE[@]}" exec -T postgres psql -U acms -d postgres -c "ALTER DATABASE acms RENAME TO acms_old" >/dev/null
  "${COMPOSE[@]}" exec -T postgres psql -U acms -d postgres -c "ALTER DATABASE acms_restore_tmp RENAME TO acms" >/dev/null
  info "database restored to pre-deploy state (old DB kept as acms_old for forensics)"

  # App back to previous known-good SHA/image. Restore the tracked nginx.conf
  # first — maintenance conf dirties it and git refuses a checkout that would
  # clobber local modifications (nginx.conf may differ between SHAs).
  if ! git -C "$REPO_DIR" checkout -- deploy/reverse-proxy/nginx.conf 2>/dev/null; then
    warn "could not git-restore nginx.conf before checkout (continuing if identical)"
  fi
  if ! git -C "$REPO_DIR" checkout --detach "$PREV_SHA" 2>/dev/null; then
    die "cannot checkout previous SHA $PREV_SHA — requesting human direction"
  fi
  PREV_TAG="$(printf '%s' "$PREV_SHA" | cut -c1-7)"
  if image_exists "acms-app:$PREV_TAG"; then
    info "starting previous image acms-app:$PREV_TAG"
    ACMS_APP_IMAGE_TAG="$PREV_TAG" "${COMPOSE[@]}" up -d acms-app
  else
    info "previous image missing — rebuilding from sha $PREV_SHA"
    PREV_TAG="$(build_release_image "$PREV_SHA")"
    ACMS_APP_IMAGE_TAG="$PREV_TAG" "${COMPOSE[@]}" up -d acms-app
  fi

  maintenance_off
  RESTORED_REV="$(db_alembic_revision || true)"
  info "alembic revision after restore: $RESTORED_REV (expected $OLD_REV)"
  if [ -n "$OLD_REV" ] && [ "$RESTORED_REV" != "$OLD_REV" ] && [ "$RESTORED_REV" != "unknown" ]; then
    die "restored revision mismatch ($RESTORED_REV != $OLD_REV) — requesting human direction"
  fi

  # Full validation of the restored release (plan §8-L2 step 10).
  if "$DEPLOY_DIR/validate-release.sh"; then
    # New release id for the restored state (never overwrite the previous
    # release's accepted metadata — plan §29: do not hide a failed rollback).
    RESTORED_ID="$(printf '%s-rollback-%s' "$(utc_now)" "$(printf '%s' "$PREV_SHA" | cut -c1-7)")"
    record_release "$RESTORED_ID" "restored" "$PREV_SHA" "$PREV_TAG" \
      "$FAILED_REV" "$RESTORED_REV" "$BACKUP_PATH" "$BACKUP_SHA" "$(stat -c %s "$BACKUP_PATH")" "$FAILED_SHA"
    # current/previous pointers: restored release is current; the FAILED
    # release was never accepted, so previous stays as it was before the
    # failed deployment attempt (the failed release never occupied the slot).
    printf '%s\n' "$RESTORED_ID" > "$ACMS_RELEASES_DIR/current"
    transaction_clear
    history_append "rollback" "success" "to_sha=$PREV_SHA" "db_restore=yes" "failed_sha=$FAILED_SHA"
    info "ROLLBACK COMPLETE: sha=$PREV_SHA alembic=$RESTORED_REV (validated)"
    exit 0
  else
    history_append "rollback" "validation_failed" "to_sha=$PREV_SHA" "db_restore=yes"
    error "restored release did not validate — keeping maintenance OFF but app on previous sha; requesting human direction"
    exit 4
  fi
fi

# ---------- Level 1 / standalone rollback path ----------
if [ "$PREV_META_EXISTS" -eq 0 ]; then
  die "no previous release metadata — cannot determine rollback target (plan §29: never guess)"
fi

PREV_SHA="$(release_field "$PREV_RELEASE" git_sha)"
PREV_TAG="$(release_field "$PREV_RELEASE" image_tag)"
[ -n "$PREV_SHA" ] || die "previous release metadata missing git_sha"

# Standalone rollback = no open transaction → application-only (Level 1).
# Database restore outside an open transaction is FORBIDDEN (plan §8-L2 guards):
# writes may have occurred since the last verified backup; a restore would
# silently destroy data. If a DB restore is truly needed, the human must
# explicitly authorize it (plan §3) — this script will not do it alone.
info "standalone rollback: application-only (Level 1) to sha=$PREV_SHA"
repo_clean || die "production tree dirty — refusing"
# Restore tracked nginx.conf before checkout (maintenance conf dirties it).
git -C "$REPO_DIR" checkout -- deploy/reverse-proxy/nginx.conf 2>/dev/null || true
if ! git -C "$REPO_DIR" checkout --detach "$PREV_SHA" 2>/dev/null; then
  die "cannot checkout $PREV_SHA"
fi
if ! image_exists "acms-app:$PREV_TAG"; then
  info "image acms-app:$PREV_TAG missing — rebuilding from $PREV_SHA"
  PREV_TAG="$(build_release_image "$PREV_SHA")"
fi
"${COMPOSE[@]}" stop acms-app >/dev/null 2>&1 || true
ACMS_APP_IMAGE_TAG="$PREV_TAG" "${COMPOSE[@]}" up -d acms-app

if "$DEPLOY_DIR/validate-release.sh"; then
  history_append "rollback" "success" "to_sha=$PREV_SHA" "db_restore=no"
  # Pointers: current = restored Level-1 release; failed one never accepted.
  RESTORED_ID="$(printf '%s-rollback-%s' "$(utc_now)" "$(printf '%s' "$PREV_SHA" | cut -c1-7)")"
  record_release "$RESTORED_ID" "restored" "$PREV_SHA" "$PREV_TAG" \
    "$CURRENT_REV" "$(db_alembic_revision || true)" "" "" "" "$CURRENT_SHA"
  printf '%s\n' "$RESTORED_ID" > "$ACMS_RELEASES_DIR/current"
  info "LEVEL 1 ROLLBACK COMPLETE: sha=$PREV_SHA (validated); DB untouched"
  info "NOTE: if the failed release ran migrations, old-app/new-schema may limit function — verify features; if data corruption is suspected, a human must authorize a DB restore (plan §3)"
  exit 0
else
  history_append "rollback" "failed" "to_sha=$PREV_SHA" "db_restore=no"
  die "rollback validation FAILED — do not hide it; request human direction"
fi
