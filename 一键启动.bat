@echo off
setlocal EnableExtensions
cd /d "%~dp0"

if "%SERVER_HOST%"=="" set "SERVER_HOST=0.0.0.0"
if "%SERVER_PORT%"=="" set "SERVER_PORT=8000"
if "%SERVER_WORKERS%"=="" set "SERVER_WORKERS=1"
set "APP_TARGET=app.main:app"

echo.
echo ========================================
echo  Grok2API one-click local launcher
echo ========================================
echo  Admin:  http://127.0.0.1:%SERVER_PORT%/admin/login
echo  API:    http://127.0.0.1:%SERVER_PORT%/v1/models
echo.

set "UV_CMD="
where uv >nul 2>nul
if not errorlevel 1 set "UV_CMD=uv"

set "PYTHON_CMD="
if exist ".venv\Scripts\python.exe" set "PYTHON_CMD=.venv\Scripts\python.exe"
if not defined PYTHON_CMD (
  where python >nul 2>nul
  if not errorlevel 1 set "PYTHON_CMD=python"
)
if not defined PYTHON_CMD (
  where py >nul 2>nul
  if not errorlevel 1 set "PYTHON_CMD=py -3"
)
if not defined PYTHON_CMD (
  echo [ERROR] Python 3.13+ is not installed or not in PATH.
  pause
  exit /b 1
)

call %PYTHON_CMD% -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 13) else 1)" >nul 2>nul
if errorlevel 1 (
  echo [ERROR] Python 3.13+ is required.
  call %PYTHON_CMD% --version
  pause
  exit /b 1
)

if "%GROK2API_START_CHECK%"=="1" (
  echo [OK] Startup script check passed.
  echo      Target: %APP_TARGET%
  echo      URL:    http://127.0.0.1:%SERVER_PORT%
  exit /b 0
)

if defined UV_CMD (
  echo [1/2] Syncing dependencies with uv...
  call "%UV_CMD%" sync
  if errorlevel 1 goto :fail
  echo [2/2] Starting server on http://127.0.0.1:%SERVER_PORT% ...
  call "%UV_CMD%" run granian --interface asgi --host %SERVER_HOST% --port %SERVER_PORT% --workers %SERVER_WORKERS% %APP_TARGET%
  goto :end
)

if not exist ".venv\Scripts\python.exe" (
  echo [1/3] Creating virtual environment...
  call %PYTHON_CMD% -m venv .venv
  if errorlevel 1 goto :fail
)

set "VENV_PY=.venv\Scripts\python.exe"
echo [2/3] Installing dependencies with pip...
call "%VENV_PY%" -m pip install --upgrade pip
if errorlevel 1 goto :fail
call "%VENV_PY%" -m pip install -e .
if errorlevel 1 goto :fail

echo [3/3] Starting server on http://127.0.0.1:%SERVER_PORT% ...
call "%VENV_PY%" -m granian --interface asgi --host %SERVER_HOST% --port %SERVER_PORT% --workers %SERVER_WORKERS% %APP_TARGET%
goto :end

:fail
echo.
echo [ERROR] Startup failed. Check the messages above.
pause
exit /b 1

:end
set "EXIT_CODE=%errorlevel%"
echo.
echo Server stopped. Exit code: %EXIT_CODE%
pause
exit /b %EXIT_CODE%
