@echo off
REM Windows: 더블클릭으로 실행 (처음 1회 가상환경 생성, 의존성 설치)
chcp 65001 >nul
cd /d "%~dp0"
if not exist .venv (
  py -3 -m venv .venv || python -m venv .venv || (echo Python 3 이 필요합니다 & pause & exit /b 1)
  .venv\Scripts\pip install -r requirements.txt || (pause & exit /b 1)
)
start "" http://127.0.0.1:8765/
.venv\Scripts\python -m psd_cleanup serve --port 8765
pause
