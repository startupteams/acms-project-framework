#!/usr/bin/env bash
# update.sh — DEPRECATED wrapper: use deploy/release.sh (feature-delivery plan §7).
#
# Kept for operator muscle memory. It now runs the same safe release
# transaction as release.sh (preflight → verified backup → maintenance window
# → migrate → validate → auto-rollback on failure), so an operator who types
# update.sh still gets the deterministic rollback path.

set -euo pipefail

DEPLOY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

cat >&2 <<'EOF'
==> update.sh is deprecated: routing through deploy/release.sh (safe release
    transaction with pre-deploy backup + automatic rollback). Use
    deploy/release.sh [<sha>] directly going forward.
EOF

exec "$DEPLOY_DIR/release.sh" "$@"