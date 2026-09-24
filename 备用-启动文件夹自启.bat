@echo off
setlocal

rem ============================================================
rem  Install autostart via the Startup folder. NO ADMIN NEEDED.
rem  Trade-off: no delay, so it starts earlier than the Task
rem  Scheduler method. The script itself waits up to 45s for
rem  Wi-Fi, so this is usually fine.
rem  Pure ASCII on purpose (see install-autostart.bat).
rem ============================================================

set "SCRIPT=%~dp0auto_login.py"
set "LNK=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\CampusWiFiAutoLogin.lnk"

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
echo Shortcut    : %LNK%
echo.

powershell -NoProfile -ExecutionPolicy Bypass -Command "$ws = New-Object -ComObject WScript.Shell; $lnk = $ws.CreateShortcut('%LNK%'); $lnk.TargetPath = '%PYW%'; $lnk.Arguments = '\"%SCRIPT%\"'; $lnk.WorkingDirectory = '%~dp0'; $lnk.Save()"

if errorlevel 1 (
    echo [FAILED] Could not create the shortcut.
) else (
    echo [OK] Shortcut placed in your Startup folder.
    echo      It runs the next time you log into Windows.
    echo.
    echo To undo, run:  uninstall-autostart.bat
)

echo.
pause
