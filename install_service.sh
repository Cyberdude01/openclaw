#!/usr/bin/env bash
# Install and start the Polymarket systemd service.
# Usage:  GITHUB_TOKEN=ghp_xxx bash install_service.sh

set -euo pipefail

TOKEN="${GITHUB_TOKEN:-}"
if [ -z "$TOKEN" ]; then
  echo "ERROR: set GITHUB_TOKEN before running this script"
  echo "  export GITHUB_TOKEN=ghp_xxxxxxxxxxxx"
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_SRC="$SCRIPT_DIR/polymarket.service"
SERVICE_DST="/etc/systemd/system/polymarket.service"

# Inject the real token into the service file
sed "s/REPLACE_ME/$TOKEN/" "$SERVICE_SRC" > "$SERVICE_DST"

systemctl daemon-reload
systemctl enable polymarket
systemctl restart polymarket

echo ""
echo "Service installed and started."
echo "  Status : systemctl status polymarket"
echo "  Logs   : journalctl -u polymarket -f"
echo "  Stop   : systemctl stop polymarket"
