@echo off
setlocal
cd /d "%~dp0"
rem Builds DeadlockBuilds.exe locally (needs run.bat to have been run once).
if not exist ".venv\Scripts\python.exe" (
    echo Run run.bat once first - it installs Python dependencies.
    pause
    exit /b 1
)
echo Installing PyInstaller...
".venv\Scripts\python.exe" -m pip install -q pyinstaller
echo Building DeadlockBuilds.exe (1-3 minutes)...
".venv\Scripts\python.exe" -m PyInstaller --noconfirm --onefile --windowed --name DeadlockBuilds --exclude-module tkinter --exclude-module unittest app.py
if not exist "dist\DeadlockBuilds.exe" (
    echo Build failed. See the messages above.
    pause
    exit /b 1
)
copy /y "dist\DeadlockBuilds.exe" "DeadlockBuilds.exe" >nul
echo.
echo Done: DeadlockBuilds.exe is in this folder. You can move it anywhere.
pause
