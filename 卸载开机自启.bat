@echo off
setlocal

rem  Remove BOTH kinds of autostart. Pure ASCII on purpose.

set "TASK=CampusWiFiAutoLogin"
set "LNK=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\CampusWiFiAutoLogin.lnk"

net session >nul 2>&1
if errorlevel 1 (
    echo Requesting administrator rights ^(UAC prompt^)...
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)

schtasks /delete /tn "%TASK%" /f >nul 2>&1
if errorlevel 1 (
    echo [SKIP] Scheduled task "%TASK%" not found.
) else (
    echo [OK]   Scheduled task "%TASK%" deleted.
)

if exist "%LNK%" (
    del /f /q "%LNK%"
    echo [OK]   Startup folder shortcut deleted.
) else (
    echo [SKIP] No Startup folder shortcut found.
)

echo.
pause
