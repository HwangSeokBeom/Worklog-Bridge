#!/bin/zsh
set -euo pipefail

PROJECT_DIR="${0:A:h:h}"
CONFIG_PATH="${1:-$PROJECT_DIR/config.local.json}"

python3 "$PROJECT_DIR/mac_collect_lorotopik_worklog.py" \
  --config "$CONFIG_PATH" \
  --mode daily \
  --comment "LoroTopik OIDC 로그인 흐름을 검토하고 다음 개선 항목을 정리했다." \
  --dry-run
