@echo off
echo ============================================
echo  ECG Monitor - Dependency Installer
echo ============================================
echo.

:: Check if Python is installed
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] Python not found! Please install Python first.
    echo Download: https://www.python.org/downloads/
    pause
    exit /b 1
)

echo [INFO] Python found:
python --version
echo.

:: Upgrade pip first
echo [STEP 1] Upgrading pip...
python -m pip install --upgrade pip --quiet
echo Done.
echo.

:: Install each dependency (skip if already installed)
echo [STEP 2] Installing required packages...
echo.

echo  - requests
python -m pip install requests --quiet && echo   [OK] requests || echo   [FAILED] requests

echo  - pyserial
python -m pip install pyserial --quiet && echo   [OK] pyserial || echo   [FAILED] pyserial

echo  - PyQt5
python -m pip install PyQt5 --quiet && echo   [OK] PyQt5 || echo   [FAILED] PyQt5

echo  - pyqtgraph
python -m pip install pyqtgraph --quiet && echo   [OK] pyqtgraph || echo   [FAILED] pyqtgraph

echo.
echo ============================================
echo  All done! Now run:
echo  python ecg_monitor_v4__2_.py
echo ============================================
echo.
pause
