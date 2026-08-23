@echo off
setlocal
REM Double-click this to open the control panel.
REM
REM The panel has to be served, not opened as a file: a file:// page isn't
REM allowed to read or write watches.json, so opening ui/index.html directly
REM (including via an editor's preview pane) shows an error instead of routes.

cd /d "%~dp0"

REM Find a working Python. "python" on Windows can be a Microsoft Store stub
REM that does nothing, so fall back to the py launcher.
set "PY="
python -c "import sys" >nul 2>&1 && set "PY=python"
if not defined PY py -3 -c "import sys" >nul 2>&1 && set "PY=py -3"

if not defined PY (
  echo.
  echo   Python was not found.
  echo.
  echo   Install it from https://www.python.org/downloads/ and make sure you
  echo   tick "Add python.exe to PATH" during setup, then run this again.
  echo.
  pause
  exit /b 1
)

echo.
echo   ============================================================
echo     Flight Watch control panel
echo.
echo     Opening:  http://127.0.0.1:8765/
echo.
echo     If your browser doesn't open by itself, paste that address
echo     in yourself. Do NOT open ui/index.html directly - it can't
echo     read your routes that way.
echo.
echo     Leave this window open. Press Ctrl+C to stop.
echo   ============================================================
echo.

%PY% check.py --ui

echo.
echo   Control panel stopped.
pause
