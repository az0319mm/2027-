#!/bin/bash
# PSD 클린업 엔진 제거 (백그라운드 서비스 해제 + 엔진 파일 삭제). PSD 파일은 건드리지 않습니다.
LABEL="kr.graduation2027.psdcleanup.engine"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || launchctl unload "$PLIST" 2>/dev/null
rm -f "$PLIST"
rm -rf "$HOME/PSDCleanup/engine" "$HOME/PSDCleanup/jobs" "$HOME/PSDCleanup/engine.json" "$HOME/PSDCleanup/engine.lock"
echo "✅ 클린업 엔진을 제거했습니다. (로그 파일 ~/PSDCleanup/engine.log 는 남겨 두었습니다)"
read -r -p "Enter 를 누르면 창을 닫습니다..." _
