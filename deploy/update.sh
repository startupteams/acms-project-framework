#!/usr/bin/env bash
# Update to an approved main commit (plan §17): record → fetch → checkout →
# build → migrate → restart → health-check → print deployed commit.
# Usage: deploy/update.sh [<commit-sha>]   (default: origin/main HEAD)
set -euo pipefail

DEPLOY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$(dirname "$DEPLOY_DIR")"
ENV_FILE="${ACMS_ENV_FILE:-/opt/acms/.env}"
COMPOSE="docker compose -f $DEPLOY_DIR/compose.yaml --env-file $ENV_FILE"

cd "$APP_DIR"
PREVIOUS="$(git rev-parse HEAD)"
TARGET="${1:-origin/main}"
echo "==> Current commit: $PREVIOUS"
echo "==> Target:         $TARGET"

echo "==> Fetching approved main"
git fetch origin main

# Resolve the target to a concrete commit and refuse non-main history.
COMMIT="$(git rev-parse --verify "$TARGET^{commit}")"
git merge-base --is-ancestor "$COMMIT" origin/main || {
  echo "REFUSING: $COMMIT is not an ancestor of origin/main" >&2
  exit 1
}

echo "==> Checking out $COMMIT"
git checkout --detach "$COMMIT"

echo "==> Building app image"
$COMPOSE build acms-app

echo "==> Running migrations"
$COMPOSE run --rm acms-app alembic upgrade head

echo "==> Restarting app"
$COMPOSE up -d acms-app

echo "==> Health check"
HEALTH_OK=0
for _ in $(seq 1 20); do
  if curl -fsS http://127.0.0.1:8000/health >/dev/null 2>&1; then HEALTH_OK=1; break; fi
  sleep 2
done
if [ "$HEALTH_OK" -ne 1 ]; then
  echo "HEALTH CHECK FAILED — previous commit was: $PREVIOUS" >&2
  echo "Rollback manually per deploy/README.md (rollback section)." >&2
  exit 1
fi

echo
echo "Previously deployed: $PREVIOUS"
echo "Now deployed:        $(git rev-parse HEAD)"
