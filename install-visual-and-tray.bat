@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo First run start.bat to create the environment.
  pause
  exit /b 1
)
.venv\Scripts\python.exe -m pip install -r requirements-optional.txt
if errorlevel 1 (echo Optional packages could not be installed.) else (echo Optional features installed. Restart the app.)
pause
