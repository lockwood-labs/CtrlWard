@echo off
REM ============================================================
REM  Builds ControlWard.exe (a single file) from app.py.
REM  Needs a Python that has pip. NOTE: the Python bundled with
REM  Spyder has NO pip, so this script hunts for a python.org
REM  install (launcher, user folder, Program Files) before
REM  falling back to whatever "python" is on the PATH.
REM  Everything is written to build_log.txt - if it fails,
REM  send that file to Muse.
REM ============================================================
cd /d %~dp0
set LOG=%~dp0build_log.txt
echo Control Ward build log - %DATE% %TIME% > "%LOG%"

set PY=
py -3 -m pip --version >> "%LOG%" 2>&1 && set PY=py -3
if defined PY goto found
for /d %%D in ("%LOCALAPPDATA%\Programs\Python\Python3*") do (
  if not defined PY (
    "%%D\python.exe" -m pip --version >> "%LOG%" 2>&1 && set "PY="%%D\python.exe""
  )
)
if defined PY goto found
for /d %%D in ("C:\Program Files\Python3*") do (
  if not defined PY (
    "%%D\python.exe" -m pip --version >> "%LOG%" 2>&1 && set "PY="%%D\python.exe""
  )
)
if defined PY goto found
python -m pip --version >> "%LOG%" 2>&1 && set PY=python
if defined PY goto found

echo.
echo ============================================================
echo  NO PYTHON WITH pip WAS FOUND.
echo  Your PC has Python inside Spyder, but that copy has no pip
echo  and this script will not modify Spyder's environment.
echo  Fix (2 minutes):
echo    1. Install Python from https://www.python.org/downloads/
echo       and TICK "Add python.exe to PATH" in the installer.
echo    2. Run build.bat again - it will find the new Python.
echo  Meanwhile: run_demo.bat and run_app.bat work fine with the
echo  Spyder Python you already have - no pip needed to RUN.
echo ============================================================
pause
exit /b 1

:found
echo Using Python: %PY%
echo Using Python: %PY% >> "%LOG%"
%PY% --version >> "%LOG%" 2>&1

echo.
echo STEP 1 of 2: Installing the packager (PyInstaller)...
%PY% -m pip install --upgrade pyinstaller >> "%LOG%" 2>&1
if errorlevel 1 (
  echo.
  echo STEP 1 FAILED - pip could not install PyInstaller.
  echo Send build_log.txt ^(in this folder^) to Muse and it will fix it.
  pause
  exit /b 1
)

echo STEP 2 of 2: Building ControlWard.exe (about a minute)...
%PY% -m PyInstaller --onefile --name ControlWard --add-data "tool.html;." app.py >> "%LOG%" 2>&1
if errorlevel 1 (
  echo.
  echo STEP 2 FAILED - the build did not finish.
  echo Send build_log.txt ^(in this folder^) to Muse and it will fix it.
  pause
  exit /b 1
)

echo.
echo ============================================================
echo  Done! Your app is:  dist\ControlWard.exe
echo  Copy that one file anywhere (Desktop is fine) and
echo  double-click it to run. You can delete the rest.
echo ============================================================
pause
