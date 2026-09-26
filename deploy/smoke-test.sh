#!/usr/bin/env bash
# Post-deployment smoke checks (plan §14) runnable on the ACMS VM.
# Usage: deploy/smoke-test.sh [https://acms.miam.home.arpa]
set -euo pipefail

BASE_URL="${1:-https://acms.miam.home.arpa}"
FAILURES=0

check() { # name, condition-command
  local name="$1"; shift
  if "$@" >/dev/null 2>&1; then
    echo "  [ok]   $name"
  else
    echo "  [FAIL] $name"
    FAILURES=$((FAILURES + 1))
  fi
}

echo "==> Backend (direct, on VM)"
check "/health responds" curl -fsS http://127.0.0.1:8000/health
check "/version reports build" curl -fsS http://127.0.0.1:8000/version
check "unauthenticated registry is denied" bash -c '! curl -fsS http://127.0.0.1:8000/api/v1/agents'

echo "==> HTTPS UI (via reverse proxy: $BASE_URL)"
check "HTTPS reachable" curl -fsS "$BASE_URL/ui/login"
# HTTP→HTTPS redirect check (base URL without scheme):
BASE_HOST="$(echo "$BASE_URL" | sed -E 's|^https?://||')"
check "HTTP redirects to HTTPS" bash -c "curl -s -o /dev/null -w '%{http_code}' \"http://$BASE_HOST/ui/login\" | grep -q 301"
check "login page served over TLS" curl -fsS "$BASE_URL/ui/login" | grep -qi "sign in"
check "UI redirects unauthenticated users" bash -c "curl -s -o /dev/null -w '%{http_code}' \"$BASE_URL/ui/\" | grep -q 303"
check "registry API not exposed via proxy" bash -c '! curl -fsS "$BASE_URL/api/v1/agents"'

echo
if [ "$FAILURES" -eq 0 ]; then
  echo "SMOKE TEST PASSED"
else
  echo "SMOKE TEST FAILED: $FAILURES check(s)"
  exit 1
fi
