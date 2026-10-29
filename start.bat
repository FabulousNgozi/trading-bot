@echo off
REM start.bat - one-click launcher for the Deriv Volatility Bot web panel (Windows)
setlocal enabledelayedexpansion
cd /d "%~dp0"

echo ==============================
echo  Deriv Volatility Bot - launcher
echo ==============================
echo.

REM 1. find python
where python >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python not found on PATH.
    echo   Install Python 3 from https://www.python.org/downloads/
    echo   IMPORTANT: tick "Add python.exe to PATH" during install.
    echo.
    pause
    exit /b 1
)
for /f "delims=" %%v in ('python --version 2^>^&1') do echo Using: %%v

REM 2. venv
if not exist "venv\Scripts\python.exe" (
    echo Creating virtual environment...
    python -m venv venv
    if errorlevel 1 (
        echo ERROR: venv creation failed.
        pause
        exit /b 1
    )
)

REM 3. deps
echo Installing dependencies...
call "venv\Scripts\activate.bat"
python -m pip install -q --disable-pip-version-check --upgrade pip >nul 2>&1
python -m pip install -q --disable-pip-version-check -r requirements.txt
if errorlevel 1 (
    echo ERROR: dependency install failed. Check your internet connection.
    pause
    exit /b 1
)
echo Dependencies ready.
echo.

REM 4. .env from example, first run only
if not exist ".env" (
    if exist ".env.example" (
        copy /y ".env.example" ".env" >nul
        echo Created .env from .env.example.
        echo   -^> Add DERIV_APP_ID and a DEMO-scope DERIV_API_TOKEN to trade.
        echo   -^> Without them the panel still loads; Start will explain what is missing.
        echo.
    )
)

REM 5. open browser shortly after server starts
start "" cmd /c "timeout /t 3 >nul & start http://127.0.0.1:8000/"

echo Starting panel at http://127.0.0.1:8000/
echo Press Ctrl-C to stop.
echo
python server.py

echo.
echo Panel stopped.
pause
