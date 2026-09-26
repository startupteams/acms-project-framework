#!/usr/bin/env bash
# validate-release.sh — post-deploy validation gate (feature-delivery plan §11).
#
# Usage:
#   deploy/validate-release.sh                     # full stage (standalone)
#   deploy/validate-release.sh --stage app --strict-build
#   deploy/validate-release.sh --stage full --strict-build
#   deploy/validate-release.sh --expected-rev <alembic-rev>
#   deploy/validate-release.sh --feature-smoke <shell-command>
#
# release.sh runs two stages around maintenance mode:
#   app  (maintenance still ON): direct container checks only — repeated
#        /health, DB, Alembic revision, restart count, log scan, build identity
#   full (maintenance OFF): everything + HTTPS proxy path, UI login page,
#        unauthenticated denials, registry not exposed via proxy
#
# Every check must pass before a release is accepted.

set -euo pipefail

DEPLOY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$DEPLOY_DIR/common.sh"

STAGE="full"
STRICT_BUILD=0
FEATURE_SMOKE=""
EXPECTED_REV="${ACMS_EXPECTED_REV:-}"
while [ $# -gt 0 ]; do
  case "$1" in
    --stage) STAGE="$2"; shift 2 ;;
    --strict-build) STRICT_BUILD=1; shift ;;
    --feature-smoke) FEATURE_SMOKE="$2"; shift 2 ;;
    --expected-rev) EXPECTED_REV="$2"; shift 2 ;;
    *) die "unknown validate-release.sh argument: $1" ;;
  esac
done
case "$STAGE" in app|full) ;; *) die "invalid stage: $STAGE (app|full)" ;; esac

FAILURES=0
check() { # <name> <cmd...>
  local name="$1"; shift
  if "$@" >/dev/null 2>&1; then
    info "  [ok]   $name"
  else
    error "  [FAIL] $name"
    FAILURES=$((FAILURES + 1))
  fi
}

info "=== Release validation (stage: $STAGE) ==="

# --- repeated /health, not one request (plan §11) ---
health_repeats() {
  local i
  for i in 1 2 3; do
    app_container_direct_health || return 1
    [ "$i" -lt 3 ] && sleep 2
  done
}
check "repeated /health (x3, direct)" health_repeats

# --- database connectivity ---
check "database connectivity" db_ready

# --- alembic revision ---
CURRENT_REV="$(db_alembic_revision || true)"
if [ -n "$CURRENT_REV" ]; then
  info "  [ok]   alembic revision: $CURRENT_REV"
  if [ -n "$EXPECTED_REV" ] && [ "$CURRENT_REV" != "$EXPECTED_REV" ]; then
    error "  [FAIL] alembic revision $CURRENT_REV != expected $EXPECTED_REV"
    FAILURES=$((FAILURES + 1))
  fi
else
  error "  [FAIL] could not read alembic revision"
  FAILURES=$((FAILURES + 1))
fi

# --- container restart count ---
RESTARTS="$(app_restart_count)"
if [ "${RESTARTS:-0}" -eq 0 ]; then
  info "  [ok]   acms-app restart count: 0"
else
  error "  [FAIL] acms-app restarted ${RESTARTS}x — crash loop suspected"
  FAILURES=$((FAILURES + 1))
fi

# --- app logs free of repeated exceptions ---
check "app logs clean (no repeated exceptions)" app_log_error_scan

# --- build identity (plan §4: Git SHA is the authoritative identity) ---
BUILD_SHA="$(app_container_direct_get /version 2>/dev/null \
  | sed -n 's/.*"git_sha"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | head -1)"
if [ "$STRICT_BUILD" -eq 1 ]; then
  DEPLOYED_SHA="$(repo_git_sha)"
  if [ -n "$BUILD_SHA" ] && [ "$BUILD_SHA" = "$DEPLOYED_SHA" ]; then
    info "  [ok]   /version git_sha matches deployed HEAD ($BUILD_SHA)"
  else
    error "  [FAIL] /version git_sha '$BUILD_SHA' != deployed HEAD '$DEPLOYED_SHA'"
    FAILURES=$((FAILURES + 1))
  fi
elif [ -n "$BUILD_SHA" ] && [ "$BUILD_SHA" != "unknown" ]; then
  info "  [ok]   /version git_sha: $BUILD_SHA"
else
  error "  [FAIL] /version missing git_sha"
  FAILURES=$((FAILURES + 1))
fi

# --- proxy-path checks: full stage only (app stage runs under maintenance) ---
if [ "$STAGE" = "full" ]; then
  check "/health via HTTPS proxy" proxy_health
  check "UI login page served via HTTPS" \
    curl -kfsS --max-time 8 "$ACMS_SMOKE_BASE_URL/ui/login"
  check "login page content" \
    bash -c "curl -kfsS --max-time 8 '$ACMS_SMOKE_BASE_URL/ui/login' | grep -qi 'sign in'"
  check "unauthenticated UI redirects (303)" \
    bash -c "curl -k -o /dev/null -w '%{http_code}' --max-time 8 '$ACMS_SMOKE_BASE_URL/ui/' | grep -q 303"
  check "registry API denied via proxy" \
    bash -c "! curl -kfsS --max-time 8 '$ACMS_SMOKE_BASE_URL/api/v1/agents'"
  PROXY_SHA="$(curl -kfsS --max-time 8 "$ACMS_SMOKE_BASE_URL/version" 2>/dev/null \
    | sed -n 's/.*"git_sha"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | head -1)"
  if [ -n "$PROXY_SHA" ] && [ "$PROXY_SHA" = "$BUILD_SHA" ]; then
    info "  [ok]   proxy /version git_sha matches app ($PROXY_SHA)"
  else
    error "  [FAIL] proxy /version git_sha '$PROXY_SHA' != app git_sha '$BUILD_SHA'"
    FAILURES=$((FAILURES + 1))
  fi
fi

# --- feature-specific smoke (plan §11) ---
if [ -n "$FEATURE_SMOKE" ]; then
  check "feature smoke: $FEATURE_SMOKE" bash -c "$FEATURE_SMOKE"
fi

if [ "$FAILURES" -eq 0 ]; then
  info "RELEASE VALIDATION PASSED (stage: $STAGE)"
  exit 0
fi
error "RELEASE VALIDATION FAILED: $FAILURES check(s) (stage: $STAGE)"
exit 1