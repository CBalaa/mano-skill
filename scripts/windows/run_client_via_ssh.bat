@echo off
setlocal EnableExtensions

if not defined SSH_ALIAS set "SSH_ALIAS=tt"
if not defined LOCAL_PORT set "LOCAL_PORT=8000"
if not defined REMOTE_HOST set "REMOTE_HOST=127.0.0.1"
if not defined REMOTE_PORT set "REMOTE_PORT=8000"
if not defined CONDA_ENV set "CONDA_ENV=mano-skill-dev"
if not defined CLIENT_SCRIPT set "CLIENT_SCRIPT=visual\vla.py"
if not defined CLIENT_FLAGS set "CLIENT_FLAGS=--headless"
if not defined PYTHON_EXE if exist "%LocalAppData%\Programs\Python\Python311\python.exe" set "PYTHON_EXE=%LocalAppData%\Programs\Python\Python311\python.exe"

set "SERVER_URL=http://127.0.0.1:%LOCAL_PORT%"

if "%~1"=="" goto :usage

set "COMMAND=%~1"
shift

if /I "%COMMAND%"=="run" goto :run
if /I "%COMMAND%"=="stop" goto :stop
if /I "%COMMAND%"=="status" goto :status
if /I "%COMMAND%"=="go_no" goto :go_no
goto :usage

:usage
echo Usage:
echo   %~nx0 run "task description"
echo   %~nx0 stop
echo   %~nx0 status SESSION_ID
echo   %~nx0 go_no SESSION_ID
echo.
echo Optional environment overrides:
echo   SSH_ALIAS=tt
echo   LOCAL_PORT=8000
echo   REMOTE_HOST=127.0.0.1
echo   REMOTE_PORT=8000
echo   CONDA_ENV=mano-skill-dev
echo   CLIENT_SCRIPT=visual\vla.py
echo   CLIENT_FLAGS=--headless
echo   PYTHON_EXE=%LocalAppData%\Programs\Python\Python311\python.exe
exit /b 1

:resolve_python_runner
if defined PYTHON_EXE (
    if exist "%PYTHON_EXE%" (
        set "PYTHON_RUNNER_KIND=exe"
        exit /b 0
    )
    echo Configured PYTHON_EXE does not exist: %PYTHON_EXE%
)

where conda >nul 2>nul
if not errorlevel 1 (
    set "PYTHON_RUNNER_KIND=conda"
    exit /b 0
)

where python >nul 2>nul
if not errorlevel 1 (
    set "PYTHON_RUNNER_KIND=python"
    exit /b 0
)

where py >nul 2>nul
if not errorlevel 1 (
    set "PYTHON_RUNNER_KIND=py"
    exit /b 0
)

echo Could not find a usable Python runtime.
echo Set PYTHON_EXE to your python.exe, or put conda/python/py on PATH.
exit /b 1

:run_client
call :resolve_python_runner
if errorlevel 1 exit /b 1

if /I "%PYTHON_RUNNER_KIND%"=="exe" (
    echo Using Python: %PYTHON_EXE%
    "%PYTHON_EXE%" %CLIENT_SCRIPT% %*
    exit /b %errorlevel%
)

if /I "%PYTHON_RUNNER_KIND%"=="conda" (
    echo Using Python via conda env: %CONDA_ENV%
    conda run -n %CONDA_ENV% python %CLIENT_SCRIPT% %*
    exit /b %errorlevel%
)

if /I "%PYTHON_RUNNER_KIND%"=="python" (
    echo Using Python from PATH
    python %CLIENT_SCRIPT% %*
    exit /b %errorlevel%
)

echo Using Python launcher: py -3
py -3 %CLIENT_SCRIPT% %*
exit /b %errorlevel%

:ensure_tunnel
powershell -NoProfile -Command "$u='%SERVER_URL%/healthz'; try { $r = Invoke-RestMethod $u -TimeoutSec 2; if ($r.ok) { exit 0 } } catch {}; exit 1" >nul 2>nul
if not errorlevel 1 exit /b 0

echo Starting SSH tunnel on %SERVER_URL% via %SSH_ALIAS% ...
start "mano-skill ssh tunnel" cmd /k ssh -N -o ServerAliveInterval=30 -o ExitOnForwardFailure=yes -L %LOCAL_PORT%:%REMOTE_HOST%:%REMOTE_PORT% %SSH_ALIAS%

powershell -NoProfile -Command "$deadline=(Get-Date).AddSeconds(15); $u='%SERVER_URL%/healthz'; do { try { $r = Invoke-RestMethod $u -TimeoutSec 2; if ($r.ok) { exit 0 } } catch {}; Start-Sleep -Seconds 1 } while ((Get-Date) -lt $deadline); exit 1" >nul 2>nul
if errorlevel 1 (
    echo Failed to reach %SERVER_URL%.
    echo Check that the Linux orchestrator is running and that SSH alias %SSH_ALIAS% works.
    exit /b 1
)
exit /b 0

:run
if "%~1"=="" (
    echo Missing task description.
    exit /b 1
)
set "TASK=%~1"
shift
:collect_task
if "%~1"=="" goto :run_ready
set "TASK=%TASK% %~1"
shift
goto :collect_task

:run_ready
call :ensure_tunnel
if errorlevel 1 exit /b 1

echo Running task via %SERVER_URL%
call :run_client run "%TASK%" --server-url %SERVER_URL% %CLIENT_FLAGS%
exit /b %errorlevel%

:stop
call :ensure_tunnel
if errorlevel 1 exit /b 1

call :run_client stop --server-url %SERVER_URL%
exit /b %errorlevel%

:status
if "%~1"=="" (
    echo Missing session id.
    exit /b 1
)
call :ensure_tunnel
if errorlevel 1 exit /b 1

powershell -NoProfile -Command "Invoke-RestMethod '%SERVER_URL%/v1/sessions/%~1' | ConvertTo-Json -Depth 5"
exit /b %errorlevel%

:go_no
if "%~1"=="" (
    echo Missing session id.
    exit /b 1
)
call :ensure_tunnel
if errorlevel 1 exit /b 1

powershell -NoProfile -Command "Invoke-RestMethod -Method Post '%SERVER_URL%/v1/sessions/%~1/go_no' | ConvertTo-Json -Depth 5"
exit /b %errorlevel%
