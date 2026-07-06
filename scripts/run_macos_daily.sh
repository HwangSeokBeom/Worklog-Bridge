#!/bin/zsh
set -u

PYTHON_BIN="${1:-/opt/homebrew/bin/python3.10}"
COLLECTOR="${2:-/Users/hwangseokbeom/Documents/GitHub/Worklog Bridge/mac_collect_lorotopik_worklog.py}"

PROJECT_DIR="/Users/hwangseokbeom/Documents/GitHub/Worklog Bridge"
CONFIG="${WORKLOGBRIDGE_CONFIG:-$PROJECT_DIR/config.local.json}"

cd "$PROJECT_DIR"

exec "$PYTHON_BIN" "$COLLECTOR" \
  --config "$CONFIG" \
  --mode daily
