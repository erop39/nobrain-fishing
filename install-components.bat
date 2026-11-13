@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  py -3 -m venv .venv
  if errorlevel 1 (echo Cannot create virtual environment & pause & exit /b 1)
)
.venv\Scripts\python.exe -m pip install -r requirements.txt -r requirements-optional.txt
if errorlevel 1 (echo Installation failed & pause & exit /b 1)
echo Installed. Restart Nobrain Fishing.
pause
