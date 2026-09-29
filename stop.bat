@echo off
rem ============================================================
rem  A-Share Analyzer - stop the running server
rem
rem  Companion to start.bat. Finds whatever process is listening
rem  on PORT and terminates it (with its child processes).
rem  KEEP THIS FILE ASCII-ONLY WITH CRLF LINE ENDINGS.
rem ============================================================
setlocal
cd /d %~dp0

set "HOST=127.0.0.1"
set "PORT=8610"

netstat -ano | findstr /C:"%HOST%:%PORT% " | findstr /C:"LISTENING" >nul
if errorlevel 1 goto :not_running

echo Listener found on port %PORT%:
netstat -ano | findstr /C:"%HOST%:%PORT% " | findstr /C:"LISTENING"
echo.
for /f "tokens=5" %%p in ('netstat -ano ^| findstr /C:"%HOST%:%PORT% " ^| findstr /C:"LISTENING"') do call :kill %%p
echo.
echo Port %PORT% is now free.
pause
exit /b 0

:kill
echo Stopping PID %1 ...
taskkill /PID %1 /T /F
exit /b 0

:not_running
echo Nothing is listening on port %PORT% - server is already stopped.
pause
exit /b 0
