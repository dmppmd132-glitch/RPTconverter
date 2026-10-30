@echo off
rem ============================================================
rem  Build b_to_c.py into a single exe (Python 3.12 + PyInstaller)
rem  Put this file and b_to_c.py in the same folder, then run it.
rem ============================================================
cd /d "%~dp0"

py -3.12 --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python 3.12 not found. Install it from python.org first.
    pause
    exit /b 1
)

if not exist b_to_c.py (
    echo [ERROR] b_to_c.py not found in this folder.
    pause
    exit /b 1
)

rem Use a clean virtual environment so the exe stays small
py -3.12 -m venv .buildenv
call .buildenv\Scripts\activate.bat
python -m pip install --upgrade pip
python -m pip install pyinstaller

pyinstaller --noconfirm --clean --onefile --windowed --name "B2C" b_to_c.py
if errorlevel 1 (
    echo [ERROR] Build failed.
    pause
    exit /b 1
)

echo.
echo Done. Output: %~dp0dist\B2C.exe
pause
