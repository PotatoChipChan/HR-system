@echo off
TITLE SmartHR AI System - Automated Setup
cd /d "%~dp0"
SETLOCAL EnableExtensions DisableDelayedExpansion
set "PYTHONPATH="

echo ================================================================
echo           SmartHR AI - Automated Server Setup
echo ================================================================
echo.

:: 1. Check if Python is installed
python --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python is not installed or not in PATH.
    echo Please install Python 3.10+ from https://python.org
    echo Make sure to check "Add Python to PATH" during installation.
    pause
    exit /b 1
)

:: 2. Create Virtual Environment (always fresh)
echo [1/4] Creating Virtual Environment (.venv)...
if exist ".venv" (
    echo [INFO] Removing old virtual environment...
    rd /s /q ".venv"
    if exist ".venv" (
        echo [ERROR] Could not remove the old virtual environment.
        echo Close any SmartHR or Python windows using it, then run setup again.
        pause
        exit /b 1
    )
)
python -m venv .venv
if errorlevel 1 (
    echo [ERROR] Failed to create virtual environment.
    pause
    exit /b 1
)

:: 3. Verify the new environment
".venv\Scripts\python.exe" --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Virtual environment was created but cannot be used.
    pause
    exit /b 1
)

:: 4. Install Core Dependencies
echo [2/4] Installing Core Python Packages...
".venv\Scripts\python.exe" -m pip install --upgrade pip >nul 2>&1
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
    echo [ERROR] Failed to install core dependencies.
    pause
    exit /b 1
)

:: 5. Optional: Face Recognition
echo [INFO] Installing optional face recognition packages...
".venv\Scripts\python.exe" -m pip install dlib face_recognition face_recognition_models >nul 2>&1
if errorlevel 1 (
    echo [WARN] Face recognition packages skipped (requires C++ build tools).
)

:: 6. Initialize Database
echo [3/4] Initializing Database (SQLite)...
".venv\Scripts\python.exe" init_db.py
if errorlevel 1 (
    echo [ERROR] Failed to initialize database.
    pause
    exit /b 1
)

:: 7. Final Instructions
echo [4/4] Setup Complete!
echo.
echo ================================================================
echo SUCCESS: SmartHR is ready to use.
echo.
echo TO START THE SERVER:
echo Double-click 'Start_SmartHR.bat'
echo ================================================================
echo.
pause
exit /b 0
