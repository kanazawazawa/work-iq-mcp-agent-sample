@echo off
rem Messages are ASCII on purpose. cmd.exe misparses this file when it
rem contains multibyte text, and the breakage shifts with every edit.
cd /d "%~dp0"
title Work IQ MCP sample

if not exist ".venv\Scripts\python.exe" (
    echo Creating the virtual environment...
    py -3 -m venv .venv 2>nul || python -m venv .venv
    if errorlevel 1 goto fail
    echo.
    echo Installing dependencies. The first run takes a few minutes.
    .venv\Scripts\python.exe -m pip install --upgrade pip
    .venv\Scripts\python.exe -m pip install -r requirements.txt
    if errorlevel 1 goto fail
)

if not exist ".env" copy ".env.example" ".env" >nul

rem Check that the key has a value, not just that the key exists.
findstr /r /c:"^WORKIQ_CLIENT_ID=." ".env" >nul 2>&1
if errorlevel 1 goto needconfig

echo.
echo     http://localhost:8000
echo.
echo Open the URL above in your browser. Press Ctrl+C here to stop.
echo.
rem --reload: applies code changes. Without it the templates and the code drift apart.
.venv\Scripts\python.exe -m uvicorn main:app --host 127.0.0.1 --port 8000 --reload
goto end

:needconfig
echo.
echo .env is not filled in yet. Notepad will open it.
echo Set the values, save, then run this file again.
echo     %CD%\.env
echo.
echo See the setup section in README.md.
start "" notepad ".env"
goto end

:fail
echo.
echo Setup failed. Check that Python 3.10 or later is installed.

:end
echo.
pause
