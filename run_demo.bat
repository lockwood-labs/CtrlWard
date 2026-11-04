@echo off
REM Demo mode: a simulated champ select fills in pick by pick (~every 6s)
REM so you can watch Live Draft update by itself. No League client needed.
cd /d %~dp0
python -c "import sys" 2>nul
if errorlevel 1 (
  echo.
  echo PYTHON WAS NOT FOUND. Install Python 3.9+ from
  echo https://www.python.org/downloads/ and TICK "Add python.exe to PATH",
  echo then run this again.
  echo.
  pause
  exit /b 1
)
python app.py --demo %*
echo.
echo The demo has stopped. Any error message above says why.
pause
