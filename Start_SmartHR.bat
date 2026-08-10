@echo off
cd /d "%~dp0"
SETLOCAL EnableDelayedExpansion

:: ── Check Python ──────────────────────────────────────────────────
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] Python is not installed or not in PATH.
    echo Please install Python 3.10+ from https://python.org
    pause
    exit /b
)

:: ── Run setup if venv is missing or broken ────────────────────────
".venv\Scripts\python.exe" --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [INFO] Virtual environment missing or broken. Running setup...
    call Setup_SmartHR.bat
    if %errorlevel% neq 0 (
        pause
        exit /b
    )
)

:: ── Detect IP ────────────────────────────────────────────────────
:init
set "IP="
for /f "delims=" %%i in ('python -c "import socket; ips=[ip for ip in socket.gethostbyname_ex(socket.gethostname())[2] if not ip.startswith(''127.'')]; print(ips[0] if ips else '''')"') do set IP=%%i
if "%IP%"=="" (
    for /f "tokens=2 delims=:" %%a in ('ipconfig ^| findstr /c:"IPv4 Address"') do (
        set val=%%a
        set IP=!val: =!
    )
)
if "%IP%"=="" set IP=127.0.0.1

:: ── Menu ─────────────────────────────────────────────────────────
:menu
cls
echo ================================================================
echo        SmartHR - AI-Powered HR Management System
echo ================================================================
echo.
echo  NETWORK ACCESS: http://%IP%:5000
echo  LOCAL ACCESS:   http://127.0.0.1:5000
echo.
echo  [1] Start Server
echo  [2] Re-detect IP
echo  [3] Exit
echo.
set /p choice="Enter option (1-3): "

if "%choice%"=="1" goto launch
if "%choice%"=="2" goto init
if "%choice%"=="3" exit
goto menu

:: ── Launch Server ────────────────────────────────────────────────
:launch
echo [INFO] Starting SmartHR...
echo [INFO] Access the app at http://%IP%:5000
echo [INFO] A new window will open for the server.
echo [INFO] Close that window to return to this menu.
echo.
start /wait "" cmd /c "call _run_server.bat"
echo.
pause
goto menu
