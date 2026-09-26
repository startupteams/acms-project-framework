#!/usr/bin/env bash
# smoke-test.sh — ACMS post-deployment smoke checks (plan §11/§14).
#
# Usage: deploy/smoke-test.sh [base-url]
#   default base URL: https://10.0.20.122 (the ACMS VM's own IP; the nginx
#   allowlist admits the VM's source IP, and MARION has no home.arpa DNS —
#   raw IPs are the convention).
#
# Checks backend (direct), HTTPS UI through the proxy, auth denial, build
# identity, and Docker health status.

set -euo pipefail

BASE_URL="${1:-https://10.0.20.122}"
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
check "/health responds" curl -fsS --max-time 8 http://127.0.0.1:8000/health
check "/version reports build identity" curl -fsS --max-time 8 http://127.0.0.1:8000/version
check "unauthenticated registry is denied" bash -c '! curl -fsS --max-time 8 http://127.0.0.1:8000/api/v1/agents'

echo "==> HTTPS UI (via reverse proxy: $BASE_URL)"
check "HTTPS reachable (self-signed ok)" curl -kfsS --max-time 8 "$BASE_URL/ui/login"
BASE_HOST="$(echo "$BASE_URL" | sed -E 's|^https?://||')"
check "HTTP redirects to HTTPS" bash -c "curl -s -o /dev/null -w '%{http_code}' --max-time 8 \"http://$BASE_HOST/ui/login\" | grep -q 301"
check "login page served over TLS" bash -c "curl -kfsS --max-time 8 '$BASE_URL/ui/login' | grep -qi 'sign in'"
check "UI redirects unauthenticated users" bash -c "curl -k -o /dev/null -w '%{http_code}' --max-time 8 '$BASE_URL/ui/' | grep -q 303"
check "registry API not exposed via proxy" bash -c '! curl -kfsS --max-time 8 "$BASE_URL/api/v1/agents"'

echo "==> Build identity (plan §4)"
check "/version exposes git_sha" bash -c "curl -kfsS --max-time 8 '$BASE_URL/version' | grep -q 'git_sha'"
GIT_SHA="$(curl -kfsS --max-time 8 "$BASE_URL/version" | sed -n 's/.*"git_sha"[[:space:]]*:[[:space:]]*\"\([^\"]*\)\".*/\1/p' | head -1)"
if [ -n "$GIT_SHA" ] && [ "$GIT_SHA" != "unknown" ]; then
  echo "  [ok]   git_sha: $GIT_SHA"
else
  echo "  [FAIL] git_sha missing/unknown in /version"
  FAILURES=$((FAILURES + 1))
fi

echo "==> Docker health"
check "acms-app container healthy" bash -c "docker inspect acms-acms-app-1 --format '{{.State.Health.Status}}' | grep -q healthy"
