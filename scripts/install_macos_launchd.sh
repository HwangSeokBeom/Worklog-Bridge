#!/bin/zsh
set -euo pipefail

LABEL="com.worklogbridge.lorolog.daily"
SCRIPT_DIR="${0:A:h}"
PROJECT_DIR="${SCRIPT_DIR:h}"
SOURCE_PLIST="$PROJECT_DIR/launchd/$LABEL.plist"
COLLECTOR="$PROJECT_DIR/mac_collect_lorotopik_worklog.py"
TARGET_DIR="$HOME/Library/LaunchAgents"
TARGET_PLIST="$TARGET_DIR/$LABEL.plist"
CONFIG_PATH="$PROJECT_DIR/config.local.json"
DRY_RUN=false

usage() {
  print "Usage: $0 [--config /absolute/path/config.local.json] [--dry-run]"
}

while (( $# > 0 )); do
  case "$1" in
    --config)
      (( $# >= 2 )) || { print -u2 "오류: --config 뒤에 경로가 필요합니다."; exit 2; }
      CONFIG_PATH="$2"
      shift 2
      ;;
    --dry-run)
      DRY_RUN=true
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      print -u2 "오류: 알 수 없는 옵션: $1"
      usage
      exit 2
      ;;
  esac
done

if [[ "$CONFIG_PATH" != /* ]]; then
  CONFIG_PATH="${CONFIG_PATH:A}"
fi

PYTHON_REQUESTED="${PYTHON_BIN:-python3}"
PYTHON_BIN="$(command -v -- "$PYTHON_REQUESTED" 2>/dev/null || true)"
if [[ -z "$PYTHON_BIN" || "$PYTHON_BIN" != /* || ! -f "$PYTHON_BIN" || ! -x "$PYTHON_BIN" ]]; then
  print -u2 "오류: 실행 가능한 Python 경로가 필요합니다: $PYTHON_REQUESTED"
  print -u2 "조치: PYTHON_BIN=/absolute/path/to/python3.10 을 지정하세요."
  exit 2
fi

if ! "$PYTHON_BIN" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)'; then
  print -u2 "오류: Python 3.10+가 필요합니다. PYTHON_BIN=/path/to/python3.12 를 지정하세요."
  exit 2
fi

if [[ "$DRY_RUN" == true ]]; then
  print "DRY RUN: launchd 파일을 설치하거나 job을 load하지 않습니다."
  print "config: $CONFIG_PATH"
  print "python: $PYTHON_BIN"
  print "plist template: $SOURCE_PLIST"
fi

if [[ ! -f "$CONFIG_PATH" ]]; then
  print -u2 "오류: config 파일이 없습니다: $CONFIG_PATH"
  print -u2 "조치: cp '$PROJECT_DIR/config.example.json' '$PROJECT_DIR/config.local.json' 후 실제 경로를 입력하세요."
  exit 2
fi

if [[ ! -r "$CONFIG_PATH" ]]; then
  print -u2 "오류: config 파일을 읽을 수 없습니다: $CONFIG_PATH"
  exit 2
fi

if [[ ! -f "$COLLECTOR" || ! -r "$COLLECTOR" ]]; then
  print -u2 "오류: Python collector가 없거나 읽을 수 없습니다: $COLLECTOR"
  exit 2
fi

if [[ ! -f "$SOURCE_PLIST" || ! -r "$SOURCE_PLIST" ]]; then
  print -u2 "오류: launchd plist template이 없거나 읽을 수 없습니다: $SOURCE_PLIST"
  exit 2
fi

if ! CONFIGURED_DIRS=$("$PYTHON_BIN" -c '
import json, os, sys
from pathlib import Path

config_path = Path(sys.argv[1]).expanduser().resolve()
data = json.loads(config_path.read_text(encoding="utf-8"))

def resolve_path(value, label):
    if not isinstance(value, str) or not value.strip():
        raise SystemExit(f"config의 {label} 문자열이 필요합니다.")
    path = Path(os.path.expandvars(value)).expanduser()
    return (path if path.is_absolute() else config_path.parent / path).resolve()

print(resolve_path(data.get("outbox_dir", data.get("outbox_path")), "outbox_dir"))
print(resolve_path(data.get("log_dir"), "log_dir"))
' "$CONFIG_PATH"); then
  print -u2 "오류: config의 outbox/log 경로를 확인할 수 없습니다."
  exit 2
fi

CONFIGURED_PATHS=("${(@f)CONFIGURED_DIRS}")
if (( ${#CONFIGURED_PATHS} != 2 )); then
  print -u2 "오류: config의 outbox/log 경로 형식이 올바르지 않습니다."
  exit 2
fi
OUTBOX_DIR="$CONFIGURED_PATHS[1]"
LOG_DIR="$CONFIGURED_PATHS[2]"

for path_label path_value in outbox "$OUTBOX_DIR" log "$LOG_DIR"; do
  if [[ ! -d "$path_value" ]]; then
    print -u2 "오류: ${path_label} 디렉터리가 없습니다: $path_value"
    exit 2
  fi
  if [[ ! -w "$path_value" ]]; then
    print -u2 "오류: ${path_label} 디렉터리에 쓸 수 없습니다: $path_value"
    exit 2
  fi
done

TIMEZONE=$(readlink /etc/localtime 2>/dev/null || true)
if [[ "$TIMEZONE" != */Asia/Seoul ]]; then
  print -u2 "오류: launchd 17:00 실행은 Mac 시스템 시간대가 Asia/Seoul이어야 합니다. 현재: ${TIMEZONE:-unknown}"
  exit 2
fi

if "$PYTHON_BIN" "$COLLECTOR" --config "$CONFIG_PATH" --preflight; then
  PREFLIGHT_CODE=0
else
  PREFLIGHT_CODE=$?
fi
print "preflight exit: ${PREFLIGHT_CODE}"
if (( PREFLIGHT_CODE != 0 )); then
  print -u2 "오류: preflight가 exit ${PREFLIGHT_CODE}로 실패해 launchd를 설치하지 않았습니다."
  exit 2
fi

if "$PYTHON_BIN" "$COLLECTOR" --config "$CONFIG_PATH" --mode daily --dry-run; then
  DAILY_DRY_RUN_CODE=0
else
  DAILY_DRY_RUN_CODE=$?
fi
print "daily dry-run exit: ${DAILY_DRY_RUN_CODE}"
if (( DAILY_DRY_RUN_CODE != 0 )); then
  print -u2 "오류: daily dry-run이 exit ${DAILY_DRY_RUN_CODE}로 실패해 launchd를 설치하지 않았습니다."
  exit 2
fi

if ! "$PYTHON_BIN" -c '
import json, sys
from pathlib import Path
config_path = Path(sys.argv[1]).resolve()
log_dir = Path(sys.argv[2]).resolve()
summary_path = log_dir / "last_run_summary.json"
if not summary_path.is_file():
    raise SystemExit("one-shot 증거가 없습니다: " + str(summary_path))
summary = json.loads(summary_path.read_text(encoding="utf-8"))
if summary.get("final_status") != "SUCCESS" or summary.get("mode") != "daily":
    raise SystemExit("최근 daily one-shot run이 SUCCESS가 아닙니다.")
if Path(summary.get("config_path", "")).resolve() != config_path:
    raise SystemExit("최근 one-shot run의 config가 현재 config와 다릅니다.")
repos = summary.get("source_repos_considered")
if not isinstance(repos, list) or not repos:
    raise SystemExit("최근 one-shot run의 source_repos_considered가 비어 있습니다.")
outputs = [Path(value) for value in summary.get("output_files_created", [])]
if len(outputs) != 2 or not all(path.is_file() for path in outputs):
    raise SystemExit("최근 one-shot output JSON/Markdown을 확인할 수 없습니다.")
' "$CONFIG_PATH" "$LOG_DIR"; then
  print -u2 "오류: 성공한 daily one-shot 증거가 없어 launchd를 설치하지 않았습니다."
  print -u2 "먼저 collector를 --mode daily로 한 번 실제 실행하세요."
  exit 2
fi

print "one-shot evidence: PASS"

if [[ "$DRY_RUN" == true ]]; then
  print "DRY RUN READY: 모든 설치 안전 게이트를 통과했습니다."
  print "NOT INSTALLED: plist를 복사하거나 launchd job을 load하지 않았습니다."
  exit 0
fi

xml_escape() {
  print -rn -- "$1" | sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g' -e 's/"/\&quot;/g' -e "s/'/\&apos;/g"
}

sed_escape() {
  print -rn -- "$1" | sed -e 's/[\\&|]/\\&/g'
}

project_value=$(sed_escape "$(xml_escape "$PROJECT_DIR")")
python_value=$(sed_escape "$(xml_escape "$PYTHON_BIN")")
home_value=$(sed_escape "$(xml_escape "$HOME")")
config_value=$(sed_escape "$(xml_escape "$CONFIG_PATH")")
log_value=$(sed_escape "$(xml_escape "$LOG_DIR")")

mkdir -p "$TARGET_DIR" "$HOME/Library/Caches/WorklogBridge/python"
TEMP_PLIST=$(mktemp "${TMPDIR:-/tmp}/worklogbridge.plist.XXXXXX")
trap 'rm -f "$TEMP_PLIST"' EXIT

sed \
  -e "s|__PROJECT_DIR__|$project_value|g" \
  -e "s|__PYTHON__|$python_value|g" \
  -e "s|__HOME__|$home_value|g" \
  -e "s|__CONFIG_PATH__|$config_value|g" \
  -e "s|__LOG_DIR__|$log_value|g" \
  "$SOURCE_PLIST" > "$TEMP_PLIST"

plutil -lint "$TEMP_PLIST"
cp "$TEMP_PLIST" "$TARGET_PLIST"
launchctl bootout "gui/$UID/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$UID" "$TARGET_PLIST"

print "launchd 등록 완료: 월~금 17:00 Asia/Seoul"
print "config: $CONFIG_PATH"
print "logs: $LOG_DIR"
print "diagnose: '$PYTHON_BIN' '$COLLECTOR' --config '$CONFIG_PATH' --diagnose"
print "one-shot: launchctl kickstart -k gui/$UID/$LABEL"
