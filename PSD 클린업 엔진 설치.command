#!/bin/bash
# PSD 클린업 엔진 설치 (macOS 전용) — Finder 에서 우클릭 → 열기 (처음 한 번)
# 1) Mac 칩 · macOS · Photoshop 버전 확인  2) Python 3.10+ 확인  3) ~/PSDCleanup/engine 에 설치
# 4) 자가 테스트  5) 로그인 시 자동 실행(LaunchAgent) 등록  6) 엔진 응답 확인
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$HOME/PSDCleanup"
ENGINE="$ROOT/engine"
LABEL="kr.graduation2027.psdcleanup.engine"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

say() { printf "%s\n" "$*"; }
fail() { say ""; say "❌ $*"; say ""; read -r -p "Enter 를 누르면 창을 닫습니다..." _; exit 1; }

say "==============================================="
say " PSD 클린업 엔진 설치"
say "==============================================="
[ "$(uname -s)" = "Darwin" ] || fail "macOS 에서만 실행할 수 있습니다."

ARCH="$(uname -m)"
if [ "$ARCH" = "arm64" ]; then CHIP="Apple Silicon (M 시리즈)"; else CHIP="Intel ($ARCH)"; fi
# Rosetta 로 실행된 터미널이어도 실제 칩 확인
if [ "$(sysctl -n sysctl.proc_translated 2>/dev/null)" = "1" ]; then CHIP="Apple Silicon (Rosetta 터미널)"; fi
MACOS="$(sw_vers -productVersion)"
say "• Mac 칩    : $CHIP  ($(sysctl -n machdep.cpu.brand_string 2>/dev/null))"
say "• macOS     : $MACOS"
PS_FOUND=0
for app in /Applications/Adobe\ Photoshop*/Adobe\ Photoshop*.app; do
  [ -d "$app" ] || continue
  v="$(defaults read "$app/Contents/Info" CFBundleShortVersionString 2>/dev/null)"
  say "• Photoshop : $(basename "$app") — 버전 $v"
  PS_FOUND=1
  major="${v%%.*}"; minor="$(echo "$v" | cut -d. -f2)"
  if [ "${major:-0}" -lt 24 ] || { [ "${major:-0}" -eq 24 ] && [ "${minor:-0}" -lt 4 ]; }; then
    say "  ⚠️  이 버전은 픽셀 읽기·쓰기(Imaging API, 24.4+)를 지원하지 않습니다. Creative Cloud 에서 업데이트하세요."
  fi
done
[ "$PS_FOUND" = 1 ] || say "• Photoshop : /Applications 에서 찾지 못함 (다른 위치에 설치된 경우 무시해도 됩니다)"

MAJ="$(echo "$MACOS" | cut -d. -f1)"
[ "${MAJ:-0}" -ge 11 ] || fail "macOS 11 이상이 필요합니다 (현재 $MACOS)."

# --- Python 3.10+ 찾기 (python.org 설치본 → Homebrew → 시스템)
PY=""
for c in /Library/Frameworks/Python.framework/Versions/3.1[0-9]/bin/python3 /opt/homebrew/bin/python3 /usr/local/bin/python3 /usr/bin/python3; do
  [ -x "$c" ] || continue
  if "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)' 2>/dev/null; then PY="$c"; break; fi
done
if [ -z "$PY" ]; then
  say ""
  say "Python 3.10 이상이 필요합니다. 설치 페이지를 엽니다."
  say " → 'macOS 64-bit universal2 installer' 를 받아 설치한 뒤 이 파일을 다시 실행하세요."
  say "   (universal2 = Apple Silicon 과 Intel 모두 지원)"
  open "https://www.python.org/downloads/macos/"
  fail "Python 설치 후 다시 실행해 주세요."
fi
say "• Python    : $PY ($("$PY" -c 'import sys,platform; print(sys.version.split()[0], platform.machine())'))"

# --- 엔진 설치 (다운로드 폴더를 옮기거나 지워도 계속 동작하도록 복사)
say ""
say "엔진을 설치합니다: $ENGINE"
mkdir -p "$ENGINE" "$ROOT/jobs" "$HOME/Library/LaunchAgents"
rm -rf "$ENGINE/app"
mkdir -p "$ENGINE/app"
cp -R "$HERE/psd_cleanup" "$ENGINE/app/" || fail "엔진 파일 복사 실패"
cp "$HERE/requirements.txt" "$ENGINE/app/" || fail "requirements.txt 복사 실패"
if [ ! -x "$ENGINE/venv/bin/python" ]; then
  "$PY" -m venv "$ENGINE/venv" || fail "가상환경 생성 실패"
fi
VPY="$ENGINE/venv/bin/python"
say "필요한 부품을 설치합니다 (처음엔 1~3분)…"
"$VPY" -m pip install --upgrade pip -q || fail "pip 업그레이드 실패 (인터넷 연결 확인)"
"$VPY" -m pip install -q "psd-tools>=1.10,<2" "numpy>=1.24" "scipy>=1.10" "Pillow>=10.0" || fail "부품 설치 실패 (인터넷 연결 확인)"

say "엔진 자가 테스트…"
( cd "$ENGINE/app" && "$VPY" -m psd_cleanup selftest ) || fail "엔진 자가 테스트 실패"

# --- LaunchAgent: 로그인할 때마다 자동 실행, 꺼지면 다시 실행
cat > "$PLIST" <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array><string>$VPY</string><string>-m</string><string>psd_cleanup</string><string>watch</string></array>
  <key>WorkingDirectory</key><string>$ENGINE/app</string>
  <key>EnvironmentVariables</key><dict><key>PYTHONPATH</key><string>$ENGINE/app</string></dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ProcessType</key><string>Background</string>
  <key>StandardOutPath</key><string>$ROOT/engine.log</string>
  <key>StandardErrorPath</key><string>$ROOT/engine.log</string>
</dict>
</plist>
PL
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || launchctl unload "$PLIST" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST" 2>/dev/null || launchctl load -w "$PLIST" || fail "백그라운드 엔진 등록 실패"

say "엔진 응답 확인…"
OK=0
for i in $(seq 1 20); do
  if [ -f "$ROOT/engine.json" ] && "$VPY" -c "import json,time,sys; d=json.load(open('$ROOT/engine.json')); sys.exit(0 if time.time()-d['time']<10 else 1)" 2>/dev/null; then OK=1; break; fi
  sleep 1
done
[ "$OK" = 1 ] || fail "엔진이 응답하지 않습니다. 로그: $ROOT/engine.log"

say ""
say "✅ 설치 완료! 클린업 엔진이 백그라운드에서 실행 중입니다 (로그인할 때 자동으로 켜집니다)."
say "   이제 Photoshop 의 'PSD 클린업' 패널에서 '환경 확인'을 눌러 보세요."
say "   제거하려면 'PSD 클린업 엔진 제거.command' 를 실행하세요."
say ""
read -r -p "Enter 를 누르면 창을 닫습니다..." _
