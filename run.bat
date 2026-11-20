@echo off
setlocal
cd /d "%~dp0"

echo ============================================================
echo   University Club Timetable ^& Availability Analyzer
echo ============================================================

if not exist ".venv\Scripts\python.exe" (
    echo [1/3] Creating local Python virtual environment ^(.venv^)...
    python -m venv .venv
    if errorlevel 1 (
        echo ERROR: Could not create virtual environment. Ensure Python 3.11+ is installed.
        pause
        exit /b 1
    )
    echo [2/3] Installing requirements...
    call ".venv\Scripts\pip.exe" install -r requirements.txt
) else (
    call ".venv\Scripts\activate.bat"
)

echo [Starting] Launching FastAPI on http://127.0.0.1:8000 ...
start "" powershell -NoProfile -WindowStyle Hidden -Command "Start-Sleep -Seconds 2; Start-Process 'http://127.0.0.1:8000'"

".venv\Scripts\python.exe" -m uvicorn main:app --host 127.0.0.1 --port 8000 --reload

