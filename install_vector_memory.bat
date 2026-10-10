@echo off
chcp 65001 >nul 2>&1
title Install Vector Memory

echo ============================================
echo   Install Vector RAG Memory Dependencies
echo ============================================
echo.
echo This will install fastembed and chromadb.
echo First run downloads ~90MB embedding model.
echo.

if not exist "bar\Scripts\activate.bat" (
    echo [ERROR] Venv not found. Run start.bat first.
    pause
    exit /b 1
)

call bar\Scripts\activate.bat

echo [INFO] Installing fastembed and chromadb...
pip install fastembed>=0.3.0 chromadb>=0.5.0
if errorlevel 1 (
    echo.
    echo [ERROR] Install failed. Check network.
    pause
    exit /b 1
)

echo.
echo ============================================
echo  [OK] Vector memory deps installed!
echo.
echo  Enable in script settings:
echo    "vector_memory_enabled": true
echo.
echo  Model will download on first game run.
echo ============================================
echo.
pause
\r