#!/usr/bin/env bash
# First-time deployment on the ACMS VM (plan §13).
# Preconditions (see README): /opt/acms checkout, /opt/acms/.env (0600),
# /opt/acms/tls/acms.crt + acms.key, DNS + firewall in place.
set -euo pipefail

DEPLOY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$(dirname "$DEPLOY_DIR")"
ENV_FILE="${ACMS_ENV_FILE:-/opt/acms/.env}"
COMPOSE="docker compose -f $DEPLOY_DIR/compose.yaml --env-file $ENV_FILE"

cd "$APP_DIR"
echo "==> Deploying commit: $(git rev-parse HEAD)"
echo "    (approved main must be checked out; see deploy/update.sh for updates)"

echo "==> Building ACMS app image"
$COMPOSE build acms-app

echo "==> Starting PostgreSQL"
$COMPOSE up -d postgres
echo -n "==> Waiting for database health"
for _ in $(seq 1 30); do
  if $COMPOSE exec -T postgres pg_isready -U acms -d acms >/dev/null 2>&1; then
    echo " ok"; break
  fi
  echo -n "."; sleep 2
done

echo "==> Running alembic upgrade head"
$COMPOSE run --rm acms-app alembic upgrade head

echo "==> Starting ACMS app"
$COMPOSE up -d acms-app

echo "==> Starting reverse proxy"
$COMPOSE up -d reverse-proxy

echo "==> Health check"
for _ in $(seq 1 20); do
  if curl -fsS http://127.0.0.1:8000/health >/dev/null 2>&1; then break; fi
  sleep 2
done

echo "==> Smoke test"
"$DEPLOY_DIR/smoke-test.sh"

echo
echo "Deployed commit: $(git rev-parse HEAD)"
$COMPOSE ps
