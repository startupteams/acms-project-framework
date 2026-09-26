#!/usr/bin/env bash
# release.sh — ACMS safe release transaction (feature-delivery plan §7).
#
# Usage: deploy/release.sh [<approved-main-sha>]
#        (default target: origin/main HEAD; must be an ancestor of origin/main)
#
# Phases:
#   A  preflight — approved commit, clean tree, health, disk, secrets (§7-A)
#   B  preserve  — record old state, maintenance ON, verified pre-deploy
#                  PostgreSQL backup, transaction record (§5, §6, §7-B)
#   C  deploy    — checkout target, build SHA-tagged image, migrate, start (§7-C)
#   D  validate  — validate-release.sh; SUCCESS → accept / FAILURE → auto
#                  rollback to the exact previous known-good release (§8)
#
# Deployment authority (plan §3): the human authorizes the release; this script
# is pre-authorized to roll back automatically if validation fails.

set -euo pipefail

DEPLOY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "$DEPLOY_DIR/common.sh"

TARGET="${1:-origin/main}"
# Optional feature-specific smoke command (plan §11): must exit 0 to accept.
FEATURE_SMOKE="${ACMS_FEATURE_SMOKE:-}"
EXPECTED_REV=""  # set post-migration in Phase C

require_root_or_die() {
  [ "$(id -u)" -eq 0 ] || [ -w "$ACMS_RELEASES_DIR" ] || die "run as root (release state is root-owned)"
}

# ---------------------------------------------------------------- Phase A
require_root_or_die
info "=== ACMS release transaction: Phase A — preflight ==="
preflight "$TARGET"
COMMIT="$(git -C "$REPO_DIR" rev-parse --verify "$TARGET^{commit}")"
info "Target commit: $COMMIT"

# ---------------------------------------------------------------- Phase B
info "=== Phase B — preserve old state ==="
OLD_SHA="$(repo_git_sha)"
OLD_REV="$(db_alembic_revision)"
OLD_IMAGE_TAG="$(docker inspect "$("${COMPOSE[@]}" ps -q acms-app)" --format '{{.Config.Image}}' 2>/dev/null | sed 's/^acms-app://' || true)"
OLD_IMAGE_TAG="${OLD_IMAGE_TAG:-local}"
RELEASE_ID="$(new_release_id)"
info "Previous known-good: sha=$OLD_SHA image=acms-app:$OLD_IMAGE_TAG alembic=$OLD_REV"
info "Release id: $RELEASE_ID"

maintenance_on
create_verified_backup "$RELEASE_ID"
transaction_write "$RELEASE_ID" "$COMMIT" "$BACKUP_PATH" "$BACKUP_SHA256" "$OLD_SHA" "$OLD_REV"

rollback_now() { # <phase-failed> — automatic rollback path (plan §3/§8)
  local reason="$1"
  error "Phase D validation FAILED ($reason) — invoking automatic rollback"
  history_append "deploy" "failed" "release_id=$RELEASE_ID" "target=$COMMIT" "reason=$reason"
  # Transaction is still open → Level 2 (app + DB restore) is authorized.
  if "$DEPLOY_DIR/rollback.sh" --in-flight; then
    history_append "rollback" "success" "release_id=$RELEASE_ID" "target=$COMMIT" "reason=$reason"
    info "Automatic rollback completed; release NOT accepted"
    exit 3
  else
    history_append "rollback" "failed" "release_id=$RELEASE_ID" "target=$COMMIT" "reason=$reason"
    error "AUTOMATIC ROLLBACK FAILED — maintenance mode remains ON; requesting human direction (plan §3)"
    error "Do not retry blindly. Inspect $ACMS_HISTORY_FILE and $ACMS_TRANSACTION_FILE"
    exit 4
  fi
}

# ---------------------------------------------------------------- Phase C
info "=== Phase C — deploy target ==="
# Checkout hygiene: maintenance conf overlays the tracked nginx.conf; restore
# the tracked file before moving HEAD, then re-apply maintenance.
git -C "$REPO_DIR" checkout -- deploy/reverse-proxy/nginx.conf
git -C "$REPO_DIR" checkout --detach "$COMMIT"
maintenance_reapply

IMAGE_TAG="$(build_release_image "$COMMIT")" || rollback_now "image build failed"

info "Running alembic upgrade head"
if ! ACMS_APP_IMAGE_TAG="$IMAGE_TAG" "${COMPOSE[@]}" run --rm acms-app alembic upgrade head; then
  rollback_now "alembic migration failed"
fi
NEW_REV="$(db_alembic_revision)"
EXPECTED_REV="$NEW_REV"
info "Alembic revision after migration: $NEW_REV"

info "Starting target release acms-app:$IMAGE_TAG"
ACMS_APP_IMAGE_TAG="$IMAGE_TAG" "${COMPOSE[@]}" up -d acms-app
wait_app_ready

# ---------------------------------------------------------------- Phase D
info "=== Phase D — validate release ==="
# Stage 1 (app): direct checks while maintenance is still ON.
if ! "$DEPLOY_DIR/validate-release.sh" --stage app --strict-build --expected-rev "$EXPECTED_REV"; then
  rollback_now "app-stage validation failed"
fi

# Exit maintenance; Stage 2 (full) exercises the real HTTPS proxy path.
maintenance_off
# Reconcile the proxy with the target compose spec (e.g. one-time transition to
# the directory mount). `up -d` recreates it only when the spec actually
# differs; acms-app (depends_on) is untouched — recreated only if ITS spec
# changed, which the wait_app_ready + validation below would then catch.
ACMS_APP_IMAGE_TAG="$IMAGE_TAG" "${COMPOSE[@]}" up -d reverse-proxy
if ACMS_APP_IMAGE_TAG="$IMAGE_TAG" "$DEPLOY_DIR/validate-release.sh" --stage full --strict-build; then
  record_release "$RELEASE_ID" "accepted" "$COMMIT" "$IMAGE_TAG" "$OLD_REV" "$NEW_REV" \
    "$BACKUP_PATH" "$BACKUP_SHA256" "$BACKUP_BYTES"
  promote_release "$RELEASE_ID"
  transaction_clear
  prune_old_backups
  history_append "deploy" "accepted" "release_id=$RELEASE_ID" "target=$COMMIT" "image=acms-app:$IMAGE_TAG" "alembic=$NEW_REV"
  info "Release ACCEPTED: $RELEASE_ID (acms-app:$IMAGE_TAG, sha $COMMIT, alembic $NEW_REV)"
  info "Previous known-good preserved: sha=$OLD_SHA image=acms-app:$OLD_IMAGE_TAG backup=$(basename "$BACKUP_PATH")"
  exit 0
else
  # Stage 2 failed after maintenance-off. Re-enter maintenance, then Level 2
  # rollback (transaction is still open → DB restore authorized).
  maintenance_on
  rollback_now "full-stage validation failed"
fi