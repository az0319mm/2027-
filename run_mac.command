#!/bin/bash
# macOS: 더블클릭 또는 터미널에서 ./run_mac.command  (처음 1회 가상환경 생성·의존성 설치)
cd "$(dirname "$0")" || exit 1
if [ ! -d .venv ]; then
  python3 -m venv .venv || { echo "python3 가 필요합니다 (https://www.python.org)"; exit 1; }
  ./.venv/bin/pip install -r requirements.txt || exit 1
fi
( sleep 2; open "http://127.0.0.1:8765/" ) &
./.venv/bin/python -m psd_cleanup serve --port 8765
