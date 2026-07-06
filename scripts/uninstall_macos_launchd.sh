#!/bin/zsh
set -euo pipefail

LABEL="com.worklogbridge.lorolog.daily"
TARGET_PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

launchctl bootout "gui/$UID/$LABEL" 2>/dev/null || launchctl unload "$TARGET_PLIST" 2>/dev/null || true
rm -f "$TARGET_PLIST"

print "launchd 등록을 해제했습니다: $LABEL"
print "기존 outbox와 logs 파일은 삭제하지 않았습니다."

