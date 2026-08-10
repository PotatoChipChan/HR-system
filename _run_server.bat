@echo off
cd /d "%~dp0"
".venv\Scripts\python.exe" --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] Virtual environment not found or broken.
    echo Run Setup_SmartHR.bat first.
    pause
    exit /b 1
)
call .venv\Scripts\activate.bat
if %errorlevel% neq 0 (
    echo [ERROR] Failed to activate virtual environment.
    pause
    exit /b 1
)

:: Check deps installed
python -c "import flask" >nul 2>&1 || (
    echo [INFO] Dependencies missing. Installing...
    pip install -r requirements.txt || (
        echo [ERROR] Failed to install dependencies.
        pause
        exit /b 1
    )
)

echo [INFO] Starting SmartHR server...
python run.py

:: If we reach here, the server has stopped for any reason
echo.
echo [INFO] Server has stopped (exit code %errorlevel%).
pause
