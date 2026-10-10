@echo off
setlocal EnableExtensions EnableDelayedExpansion
chcp 65001 >nul 2>&1
cd /d "%~dp0"
title Tavern

echo.
echo   Tavern - AI Interactive Fiction
echo   ================================
echo.

set "VENV_PYTHON=bar\Scripts\python.exe"
set "SETUP_MODE="

if not exist "%VENV_PYTHON%" (
    set "SETUP_MODE=create"
) else (
    "%VENV_PYTHON%" --version >nul 2>&1
    if errorlevel 1 set "SETUP_MODE=rebuild"
)

if defined SETUP_MODE (
    set "PYTHON_CMD=python"
    python --version >nul 2>&1
    if errorlevel 1 (
        py -3 --version >nul 2>&1
        if errorlevel 1 (
            echo   [!!] Python 3 was not found.
            echo        Install Python 3 and enable "Add Python to PATH".
            pause
            exit /b 1
        )
        set "PYTHON_CMD=py -3"
    )

    if "!SETUP_MODE!"=="rebuild" (
        echo   [..] Repairing invalid virtual environment...
        !PYTHON_CMD! -m venv --clear bar
    ) else (
        echo   [..] Creating virtual environment...
        !PYTHON_CMD! -m venv bar
    )

    if errorlevel 1 (
        echo   [!!] Failed to create venv
        pause
        exit /b 1
    )

    echo   [..] Installing dependencies...
    "%VENV_PYTHON%" -m pip install -r requirements.txt -q
    if errorlevel 1 (
        echo   [!!] Failed to install required dependencies
        pause
        exit /b 1
    )
    if exist "requirements-vector.txt" "%VENV_PYTHON%" -m pip install -r requirements-vector.txt -q 2>nul
    echo   [OK] Setup complete
)

if not exist "%VENV_PYTHON%" (
    echo   [..] Creating virtual environment...
    echo   [!!] Virtual environment is unavailable
    pause
    exit /b 1
)

echo   [OK] Starting server...
echo.
echo   Shell:  http://127.0.0.1:8500/shell
echo   Chat:   http://127.0.0.1:8500
echo   RPG:    http://127.0.0.1:8500/rpg
echo   ================================
echo.

set TAVERN_PORT=8500
start /b cmd /c "timeout /t 4 /nobreak >nul && start http://127.0.0.1:8500/shell"

"%VENV_PYTHON%" app.py
pause
