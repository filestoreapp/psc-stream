@echo off
rem Build a standalone PSC Stream.exe with PyInstaller.
rem IMPORTANT: run this ON WINDOWS — PyInstaller cannot cross-compile from
rem Linux/macOS. Requires that run.bat has been executed once (.venv exists).
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo [PSC Stream] .venv not found. Run run.bat once first.
  pause
  exit /b 1
)

echo [PSC Stream] Installing PyInstaller...
".venv\Scripts\python.exe" -m pip install pyinstaller
if errorlevel 1 (
  echo [PSC Stream] ERROR: could not install PyInstaller.
  pause
  exit /b 1
)

echo [PSC Stream] Building exe (this takes a few minutes)...
".venv\Scripts\python.exe" -m PyInstaller --noconfirm --onefile --windowed --name "PSC Stream" --add-data "web;web" app.py
if errorlevel 1 (
  echo [PSC Stream] ERROR: build failed.
  pause
  exit /b 1
)

echo.
echo [PSC Stream] Done: dist\"PSC Stream.exe"
echo Copy it anywhere and double-click to run. First launch needs internet for Telegram login.
pause
