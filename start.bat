@echo off
setlocal

cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo Creating virtual environment...
  py -3 -m venv .venv
  if errorlevel 1 (
    echo Failed to create virtual environment.
    pause
    exit /b 1
  )
)

call ".venv\Scripts\activate.bat"
if errorlevel 1 (
  echo Failed to activate virtual environment.
  pause
  exit /b 1
)

python -m pip install --upgrade pip
if errorlevel 1 (
  echo Failed to upgrade pip.
  pause
  exit /b 1
)

pip install -r requirements.txt
if errorlevel 1 (
  echo Failed to install requirements.
  pause
  exit /b 1
)

if not exist ".env" (
  copy ".env.example" ".env" >nul
  echo Created .env from .env.example. Server usernames and passwords are configured on the web page per target.
)

set MONITOR_TARGETS_FILE=%cd%\config\targets.json

echo Starting Server Monitor: http://127.0.0.1:8080
uvicorn app.main:app --host 0.0.0.0 --port 8080

pause
