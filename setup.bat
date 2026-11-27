@echo off
rem Setup: create .venv if missing -> install requirements -> migrate + seed (bootstrap). Idempotent.
setlocal
cd /d "%~dp0"
set "OLDCP="
for /f "tokens=2 delims=:." %%a in ('chcp') do set "OLDCP=%%a"
if defined OLDCP set "OLDCP=%OLDCP: =%"
chcp 65001 >nul
set "PYTHONIOENCODING=utf-8"
set "PIP_VERSION=26.3"
set "RC=0"
if exist ".venv\Scripts\python.exe" goto checkvenv
set "PYEXE="
where py >nul 2>nul
if not errorlevel 1 (
  set "PYEXE=py -3"
) else (
  where python >nul 2>nul
  if not errorlevel 1 set "PYEXE=python"
)
if not defined PYEXE (
  echo [setup] ERROR: Python not found. Install Python 3.10 or later from python.org. 1>&2
  goto fail
)
%PYEXE% -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)"
if errorlevel 1 (
  echo [setup] ERROR: Python 3.10 or later is required. Found: 1>&2
  %PYEXE% -V 1>&2
  goto fail
)
echo [setup] creating virtual environment (.venv)
%PYEXE% -m venv .venv
if errorlevel 1 goto fail
:checkvenv
".venv\Scripts\python.exe" -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)"
if errorlevel 1 (
  echo [setup] ERROR: .venv uses Python older than 3.10. Delete .venv and run setup.bat again. 1>&2
  goto fail
)
".venv\Scripts\python.exe" -m pip install "pip==%PIP_VERSION%"
if errorlevel 1 goto fail
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto fail
".venv\Scripts\python.exe" manage.py bootstrap
if errorlevel 1 goto fail
echo [setup] done. Run: run.bat [port]   (production mode: run.bat prod [port])
goto end
:fail
echo [setup] FAILED 1>&2
set "RC=1"
:end
if defined OLDCP chcp %OLDCP% >nul
exit /b %RC%
