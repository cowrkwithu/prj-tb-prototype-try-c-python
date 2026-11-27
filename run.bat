@echo off
rem Run: run.bat [port]        dev server (binds 127.0.0.1; set TB_BIND=0.0.0.0 to expose)
rem      run.bat prod [port]   production mode, waitress (TB_DEBUG=0)
rem Port: argument > TB_PORT > 4363. Logic lives in manage.py rundev / serve.
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo No virtual environment. Run setup.bat first. 1>&2
  exit /b 1
)
set "OLDCP="
for /f "tokens=2 delims=:." %%a in ('chcp') do set "OLDCP=%%a"
if defined OLDCP set "OLDCP=%OLDCP: =%"
chcp 65001 >nul
set "PYTHONIOENCODING=utf-8"
if /i "%~1"=="prod" goto prod
if "%~1"=="" (
  ".venv\Scripts\python.exe" manage.py rundev
) else (
  ".venv\Scripts\python.exe" manage.py rundev %~1
)
set "RC=%errorlevel%"
goto end
:prod
if "%TB_DEBUG%"=="" set "TB_DEBUG=0"
if "%~2"=="" (
  ".venv\Scripts\python.exe" manage.py serve
) else (
  ".venv\Scripts\python.exe" manage.py serve %~2
)
set "RC=%errorlevel%"
:end
if defined OLDCP chcp %OLDCP% >nul
exit /b %RC%
