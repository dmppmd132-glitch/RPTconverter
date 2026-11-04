@echo off
rem Build B2C8.exe (onefile, no console) with Python 3.12
rem Put this file and B2C8.py in the same folder, then double-click.
cd /d "%~dp0"

if not exist B2C8.py (echo B2C8.py not found in this folder. & pause & exit /b 1)
py -3.12 --version || (echo Python 3.12 not found. Install it first. & pause & exit /b 1)

if not exist venv py -3.12 -m venv venv
call venv\Scripts\activate.bat
python -m pip install --upgrade pip pyinstaller

rmdir /s /q build dist 2>nul
pyinstaller --onefile --noconsole --clean --name B2C8 B2C8.py

if exist dist\B2C8.exe (echo. & echo Done: dist\B2C8.exe) else (echo. & echo Build FAILED)
pause
