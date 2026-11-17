@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Run start-modern.bat first.
  pause
  exit /b 1
)
.venv\Scripts\python.exe -m pip install pyinstaller -r requirements.txt -r requirements-optional.txt
if errorlevel 1 exit /b 1
.venv\Scripts\python.exe -m PyInstaller --noconfirm --clean NobrainModern.spec
if errorlevel 1 pause

