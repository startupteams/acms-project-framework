#!/usr/bin/env bash
# Install/upgrade the MIAM MCP gateway on VM114 (Window 1).
# Usage: bash deploy/mcp-gateway/install-gateway.sh [repo-checkout-on-vm114]
# Assumes: repo at /opt/mcp-gateway/repo (rsynced from the workstation), env at
# /etc/miam-mcp-gateway/env (0600). Creates /var/lib/miam-mcp-gateway.
set -euo pipefail

REPO_DIR="${1:-/opt/mcp-gateway/repo}"
GATEWAY_DIR=/var/lib/miam-mcp-gateway
ENV_FILE=/etc/miam-mcp-gateway/env

fail() { echo "FAIL: $*" >&2; exit 1; }

[ -d "$REPO_DIR/mcp_gateway" ] || fail "mcp_gateway package not found in $REPO_DIR"
mkdir -p "$GATEWAY_DIR" /etc/miam-mcp-gateway
chmod 700 "$GATEWAY_DIR" /etc/miam-mcp-gateway

# python deps into a dedicated venv (PEP 668-safe)
if [ ! -x "$REPO_DIR/.venv-mcp/bin/python" ]; then
  python3 -m venv "$REPO_DIR/.venv-mcp"
  "$REPO_DIR/.venv-mcp/bin/pip" install --quiet 'mcp>=1.26,<2'
fi

# unit file with the venv python
sed -e "s|ExecStart=/usr/bin/python3|ExecStart=$REPO_DIR/.venv-mcp/bin/python|" \
  -e "s|WorkingDirectory=/opt/mcp-gateway/repo|WorkingDirectory=$REPO_DIR|" \
  -e "s|Environment=PYTHONPATH=/opt/mcp-gateway/repo|Environment=PYTHONPATH=$REPO_DIR|" \
  "$REPO_DIR/deploy/mcp-gateway/miam-mcp-gateway.service" > /etc/systemd/system/miam-mcp-gateway.service

[ -f "$ENV_FILE" ] || { echo "NOTE: $ENV_FILE missing — create it from env.example before first start"; }
chmod 600 "$ENV_FILE" 2>/dev/null || true

systemctl daemon-reload
systemctl enable --now miam-mcp-gateway
sleep 2
systemctl --no-pager is-active miam-mcp-gateway
curl -fsS http://127.0.0.1:8202/health && echo && echo "GATEWAY-INSTALL-OK"
