@echo off
REM Run the app straight from source (no build needed). Requires Python 3.9+.
cd /d %~dp0
python -c "import sys" 2>nul
if errorlevel 1 (
  echo.
  echo PYTHON WAS NOT FOUND. Install Python 3.9+ from
  echo https://www.python.org/downloads/ and TICK "Add python.exe to PATH",
  echo then run this again. If a Microsoft Store window popped up, close
  echo it - that is Windows' fake Python shortcut, not real Python.
  echo.
  pause
  exit /b 1
)
python app.py %*
echo.
echo The app has stopped. Any error message above says why - send
echo a photo of this window to Muse if it looks wrong.
pause
