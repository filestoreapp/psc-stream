@echo off
rem PSC Stream updater for Windows.
rem Double-click this file to update the app to the latest version from GitHub.
rem It keeps your .venv (Python environment). Your login, channel and library
rem live in %LOCALAPPDATA%\PSCStream, outside this folder, so those are safe too.
rem Note: this file updates everything except itself.
setlocal
cd /d "%~dp0"

set "ZIP_URL=https://github.com/filestoreapp/psc-stream/archive/refs/heads/main.zip"
set "TMPDIR=%TEMP%\psc-stream-update"

echo [PSC Stream] Downloading the latest version...
if exist "%TMPDIR%" rmdir /s /q "%TMPDIR%"
mkdir "%TMPDIR%" 2>nul

powershell -NoProfile -ExecutionPolicy Bypass -Command "Invoke-WebRequest -Uri '%ZIP_URL%' -OutFile '%TMPDIR%\latest.zip'"
if errorlevel 1 (
  echo [PSC Stream] ERROR: download failed. Check your internet connection and try again.
  pause
  exit /b 1
)

echo [PSC Stream] Extracting...
powershell -NoProfile -ExecutionPolicy Bypass -Command "Expand-Archive -Path '%TMPDIR%\latest.zip' -DestinationPath '%TMPDIR%\x' -Force"
if errorlevel 1 (
  echo [PSC Stream] ERROR: could not extract the download.
  pause
  exit /b 1
)

if not exist "%TMPDIR%\x\psc-stream-main\app.py" (
  echo [PSC Stream] ERROR: the download doesn't look right. Please try again.
  pause
  exit /b 1
)

echo [PSC Stream] Installing the update...
robocopy "%TMPDIR%\x\psc-stream-main" "%CD%" /E /XD .venv /XF update.bat >nul
if errorlevel 8 (
  echo [PSC Stream] ERROR: could not copy the new files.
  pause
  exit /b 1
)

rmdir /s /q "%TMPDIR%"
echo.
echo [PSC Stream] Updated! Now double-click run.bat to start the new version.
pause
