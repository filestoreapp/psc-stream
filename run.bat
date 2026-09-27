@echo off
rem PSC Stream launcher for Windows.
rem Creates .venv on first run, installs dependencies, starts the app.
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo [PSC Stream] Creating virtual environment...
  where py >nul 2>nul
  if %errorlevel%==0 (
    py -3.11 -m venv .venv
  ) else (
    python -m venv .venv
  )
  if errorlevel 1 (
    echo [PSC Stream] ERROR: could not create the virtual environment.
    echo Install Python 3.11+ from https://www.python.org/downloads/ and tick "Add python.exe to PATH".
    pause
    exit /b 1
  )
)

echo [PSC Stream] Installing dependencies...
".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
  echo [PSC Stream] ERROR: pip install failed.
  pause
  exit /b 1
)

echo [PSC Stream] Starting...
".venv\Scripts\python.exe" app.py
pause
