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
  if not exist ".venv\\Scripts\\python.exe" (
    echo [PSC Stream] ERROR: could not create the virtual environment.
    echo.
    echo Python 3.11 or newer is not installed. Fix it with ONE of these:
    echo   1. Open Command Prompt and run:  py install 3.11
    echo   2. Or install from https://www.python.org/downloads/ (tick "Add python.exe to PATH")
    echo Then delete the .venv folder if one was created, and run this file again.
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
