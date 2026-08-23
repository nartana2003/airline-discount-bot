@echo off
REM Double-click this to open the control panel.
REM
REM The panel has to be served, not opened as a file: a file:// page isn't
REM allowed to read or write watches.json, so opening ui/index.html directly
REM shows an error instead of your routes.

cd /d "%~dp0"
echo Starting the Flight Watch control panel...
echo Close this window (or press Ctrl+C) when you're done.
echo.

python check.py --ui
if errorlevel 1 (
  echo.
  echo Could not start with "python" - trying the "py" launcher instead...
  py check.py --ui
)

echo.
echo Control panel stopped.
pause
