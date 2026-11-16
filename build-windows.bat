@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" py -3 -m venv .venv
if errorlevel 1 goto :error
.venv\Scripts\python.exe -m pip install --upgrade pip pyinstaller
if errorlevel 1 goto :error
.venv\Scripts\python.exe -m pip install -r requirements.txt -r requirements-optional.txt
if errorlevel 1 goto :error
.venv\Scripts\python.exe -m PyInstaller --noconfirm --clean NobrainCompanion.spec
if errorlevel 1 goto :error
echo Success: dist\NobrainCompanion\NobrainCompanion.exe
pause
exit /b 0
:error
echo Build failed.
pause
exit /b 1
