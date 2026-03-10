#!/usr/bin/env bash
# =============================================================================
# deploy_strategy.sh — Deploy a Polymarket strategy version to a target host
#
# Usage:
#   ./scripts/deploy_strategy.sh [v1|v2|v3] [user@host]
#
# Examples:
#   ./scripts/deploy_strategy.sh v1                     # deploy V1 locally
#   ./scripts/deploy_strategy.sh v1 root@1.2.3.4       # deploy V1 to server
#   ./scripts/deploy_strategy.sh v2 ubuntu@staging      # deploy V2 to staging
#
# What it does:
#   1. Copies all polymarket/*.py files to /root/polymarket/ on the target
#   2. Merges the chosen strategy env file into /etc/polymarket.env
#   3. Ensures AUTO_RESTART_HOURS=1 is set
#   4. Restarts the polymarket systemd service
#   5. Tails the log so you can verify startup
# =============================================================================

set -euo pipefail

STRATEGY="${1:-v1}"
TARGET="${2:-}"
SRC_DIR="$(cd "$(dirname "$0")/.." && pwd)/polymarket"
ENV_FILE="$(cd "$(dirname "$0")/.." && pwd)/strategies/${STRATEGY}_0.env"

# ─── Validate ─────────────────────────────────────────────────────────────────
if [[ ! "$STRATEGY" =~ ^v[123]$ ]]; then
  echo "ERROR: Unknown strategy '$STRATEGY'. Use v1, v2, or v3." >&2
  exit 1
fi
if [[ ! -f "$ENV_FILE" ]]; then
  echo "ERROR: Strategy env file not found: $ENV_FILE" >&2
  exit 1
fi

echo "==> Deploying strategy $STRATEGY from $SRC_DIR"

# ─── Remote deploy ────────────────────────────────────────────────────────────
if [[ -n "$TARGET" ]]; then
  echo "==> Copying source files to $TARGET:/root/polymarket/"
  rsync -az --exclude '__pycache__' --exclude '*.pyc' \
    "$SRC_DIR/" "$TARGET:/root/polymarket/"

  echo "==> Uploading env template"
  scp "$ENV_FILE" "$TARGET:/tmp/strategy.env"

  echo "==> Merging env vars on remote"
  ssh "$TARGET" bash <<'REMOTE'
    set -euo pipefail
    ENV_DEST=/etc/polymarket.env
    # Ensure file exists
    touch "$ENV_DEST"
    # Apply each non-comment, non-empty line from strategy template
    while IFS='=' read -r key rest; do
      [[ "$key" =~ ^#.*$ || -z "$key" ]] && continue
      key="${key%%*( )}"
      # If key already present, replace; otherwise append
      if grep -q "^${key}=" "$ENV_DEST" 2>/dev/null; then
        sed -i "s|^${key}=.*|${key}=${rest}|" "$ENV_DEST"
      else
        echo "${key}=${rest}" >> "$ENV_DEST"
      fi
    done < /tmp/strategy.env
    rm /tmp/strategy.env
    # Ensure auto-restart is enabled
    if ! grep -q 'AUTO_RESTART_HOURS' "$ENV_DEST"; then
      echo 'AUTO_RESTART_HOURS=1' >> "$ENV_DEST"
    fi
    echo "--- /etc/polymarket.env (non-secret lines) ---"
    grep -v 'KEY\|SECRET\|PASS\|TOKEN' "$ENV_DEST" || true
    echo "----------------------------------------------"
REMOTE

  echo "==> Restarting polymarket service"
  ssh "$TARGET" "sudo systemctl restart polymarket"

  echo "==> Tailing logs (Ctrl-C to stop)"
  ssh "$TARGET" "journalctl -u polymarket -f --no-hostname -n 20"

# ─── Local deploy ─────────────────────────────────────────────────────────────
else
  echo "==> Local deploy to /root/polymarket/"
  sudo rsync -az --exclude '__pycache__' --exclude '*.pyc' \
    "$SRC_DIR/" /root/polymarket/

  echo "==> Merging env vars into /etc/polymarket.env"
  sudo touch /etc/polymarket.env
  while IFS='=' read -r key rest; do
    [[ "$key" =~ ^#.*$ || -z "$key" ]] && continue
    key="${key%%*( )}"
    if sudo grep -q "^${key}=" /etc/polymarket.env 2>/dev/null; then
      sudo sed -i "s|^${key}=.*|${key}=${rest}|" /etc/polymarket.env
    else
      echo "${key}=${rest}" | sudo tee -a /etc/polymarket.env > /dev/null
    fi
  done < "$ENV_FILE"

  if ! sudo grep -q 'AUTO_RESTART_HOURS' /etc/polymarket.env; then
    echo 'AUTO_RESTART_HOURS=1' | sudo tee -a /etc/polymarket.env > /dev/null
  fi

  echo "==> Restarting polymarket service"
  sudo systemctl restart polymarket

  echo "==> Tailing logs (Ctrl-C to stop)"
  journalctl -u polymarket -f --no-hostname -n 20
fi
