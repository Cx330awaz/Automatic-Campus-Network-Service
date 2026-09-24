@echo off
setlocal

rem ============================================================
rem  Install logon autostart via Task Scheduler (needs admin).
rem  NOTE: This file is pure ASCII on purpose. Chinese characters
rem  in a .bat can break cmd.exe's parser due to a byte-offset bug.
rem ============================================================

set "TASK=CampusWiFiAutoLogin"
set "SCRIPT=%~dp0auto_login.py"

rem ---- self-elevate if not running as admin ----
net session >nul 2>&1
if errorlevel 1 (
    echo Requesting administrator rights ^(UAC prompt^)...
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)

rem ---- locate pythonw.exe (no console window) ----
set "PYW="
for /f "delims=" %%i in ('where pythonw.exe 2^>nul') do if not defined PYW set "PYW=%%i"
if not defined PYW for /f "delims=" %%i in ('where python.exe 2^>nul') do if not defined PYW set "PYW=%%i"

if not defined PYW (
    echo [ERROR] pythonw.exe / python.exe not found in PATH.
    echo         Reinstall Python and tick "Add Python to PATH".
    echo.
    pause
    exit /b 1
)

echo Interpreter : %PYW%
echo Script      : %SCRIPT%
echo.

rem ---- create the logon task, delayed 15s to let Wi-Fi come up ----
schtasks /create /tn "%TASK%" /sc onlogon /delay 0000:15 /tr "\"%PYW%\" \"%SCRIPT%\"" /f

if errorlevel 1 (
    echo.
    echo [FAILED] Could not create the scheduled task. See the error above.
    echo.
    echo Tip: if you would rather avoid admin rights entirely, run
    echo      the other file instead:  "backup-startup-folder.bat"
) else (
    echo.
    echo [OK] Logon task created: %TASK%
    echo      It runs 15 seconds after you log into Windows.
    echo.
    echo To undo, run:  uninstall-autostart.bat
)

echo.
pause
