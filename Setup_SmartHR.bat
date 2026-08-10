@echo off
TITLE SmartHR AI System - Automated Setup
cd /d "%~dp0"

echo ================================================================
echo           SmartHR AI - Automated Server Setup
echo ================================================================
echo.

:: 1. Check if Python is installed
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] Python is not installed or not in PATH.
    echo Please install Python 3.10+ from https://python.org
    echo Make sure to check "Add Python to PATH" during installation.
    pause
    exit /b
)

:: 2. Create Virtual Environment (always fresh)
echo [1/4] Creating Virtual Environment (.venv)...
if exist ".venv" (
    echo [INFO] Removing old virtual environment...
    rd /s /q ".venv"
)
python -m venv .venv
if %errorlevel% neq 0 (
    echo [ERROR] Failed to create virtual environment.
    pause
    exit /b
)

:: 3. Activate
call .venv\Scripts\activate.bat
if %errorlevel% neq 0 (
    echo [ERROR] Failed to activate virtual environment.
    pause
    exit /b
)

:: 4. Install Core Dependencies
echo [2/4] Installing Core Python Packages...
python -m pip install --upgrade pip >nul 2>&1
pip install -r requirements.txt
if %errorlevel% neq 0 (
    echo [ERROR] Failed to install core dependencies.
    pause
    exit /b
)

:: 5. Optional: Face Recognition
echo [INFO] Installing optional face recognition packages...
pip install dlib face_recognition face_recognition_models >nul 2>&1
if %errorlevel% neq 0 (
    echo [WARN] Face recognition packages skipped (requires C++ build tools).
)

:: 6. Initialize Database
echo [3/4] Initializing Database (SQLite)...
python init_db.py
if %errorlevel% neq 0 (
    echo [ERROR] Failed to initialize database.
    pause
    exit /b
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
