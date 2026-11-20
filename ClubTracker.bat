@echo off
setlocal
cd /d "%~dp0"

title Club Tracker — Desktop Application

echo ============================================================
echo   Club Tracker — University Timetable Analyzer
echo ============================================================

if not exist ".venv\Scripts\python.exe" (
    echo [1/3] Initializing local Python environment (.venv)...
    python -m venv .venv
    if errorlevel 1 (
        echo ERROR: Python 3.11+ is required. Please install Python and ensure it is on your PATH.
        pause
        exit /b 1
    )
    echo [2/3] Installing dependencies...
    call ".venv\Scripts\pip.exe" install -r requirements.txt
)

:: Verify desktop UI engine
".venv\Scripts\python.exe" -c "import webview" 2>nul
if errorlevel 1 (
    echo Installing desktop UI engine (pywebview)...
    call ".venv\Scripts\pip.exe" install pywebview
)

echo [3/3] Launching Club Tracker Desktop...
call ".venv\Scripts\python.exe" desktop.py %*
if errorlevel 1 (
    echo.
    echo ============================================================
    echo [ERROR] The desktop application exited with code %errorlevel%.
    echo Please see 'desktop.log' in this folder for diagnostic details.
    echo ============================================================
    echo.
    pause
)
