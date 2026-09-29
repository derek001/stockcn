@echo off
rem ============================================================
rem  A-Share Analyzer - one-click launcher
rem
rem  KEEP THIS FILE ASCII-ONLY WITH CRLF LINE ENDINGS.
rem  cmd.exe mis-parses batch files containing multi-byte text
rem  (UTF-8 breaks once "chcp 65001" is active; LF-only endings
rem  shift the parser and make the checks below take the wrong
rem  branch). Chinese docs live in README.md.
rem ============================================================
setlocal enabledelayedexpansion
cd /d %~dp0

set "HOST=127.0.0.1"
set "PORT=8610"

where python >nul
if errorlevel 1 goto :no_python

python -c "import fastapi,uvicorn,pandas,openpyxl" >nul
if not errorlevel 1 goto :deps_ok
echo Installing backend dependencies ...
python -m pip install -r backend\requirements.txt
if errorlevel 1 goto :pip_fail
:deps_ok

if exist "frontend\dist\index.html" goto :port_check
echo [ERROR] frontend\dist\index.html not found - build the frontend first:
echo     set "PATH=%~dp0.tools\node;%%PATH%%"
echo     cd frontend ^&^& npm install ^&^& npm run build
echo See README.md for the full steps.
pause
exit /b 1

:port_check
netstat -ano | findstr /C:"%HOST%:%PORT% " | findstr /C:"LISTENING" >nul
if errorlevel 1 goto :start
echo [INFO] Port %PORT% is already in use - the server is most likely running.
echo        Just open http://%HOST%:%PORT% in your browser.
echo        The last number below is the PID holding the port:
netstat -ano | findstr /C:"%HOST%:%PORT% " | findstr /C:"LISTENING"
echo        To restart it: taskkill /PID ^<PID^> /F  and then run this script again.
pause
exit /b 1

:start
echo Starting server at http://%HOST%:%PORT%  (press Ctrl+C to stop)
python -m uvicorn app.main:app --app-dir backend --host %HOST% --port %PORT%
if not errorlevel 1 exit /b 0
echo [ERROR] Server exited with code %errorlevel%.
pause
exit /b 1

:no_python
echo [ERROR] Python not found. Install Python 3.10+ and enable "Add Python to PATH".
pause
exit /b 1

:pip_fail
echo [ERROR] Dependency install failed. Run manually:
echo     python -m pip install -r backend\requirements.txt
pause
exit /b 1
