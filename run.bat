@echo off
setlocal
cd /d "%~dp0"

if exist ".venv\pyvenv.cfg" if exist ".venv\Scripts\pythonw.exe" (
    ".venv\Scripts\python.exe" -c "import PySide6, requests" >nul 2>&1 && goto run
)

echo === Deadlock Builds: first-time setup ===
set "PY="
set "PYA="

rem 1) Python launcher "py"
py -3 -c "import sys" >nul 2>&1 && (set "PY=py" & set "PYA=-3")

rem 2) real python.exe in PATH (the Microsoft Store stub fails this test)
if not defined PY python -c "import sys" >nul 2>&1 && set "PY=python"

rem 3) default per-user install locations
if not defined PY for %%V in (313 312 311) do (
    if not defined PY if exist "%LOCALAPPDATA%\Programs\Python\Python%%V\python.exe" set "PY=%LOCALAPPDATA%\Programs\Python\Python%%V\python.exe"
)

if defined PY goto make_venv

echo.
echo Python is not installed on this PC.
choice /C YN /M "Install Python 3.12 automatically via winget now"
if errorlevel 2 goto manual

winget install -e --id Python.Python.3.12 --scope user --accept-source-agreements --accept-package-agreements
if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" (
    set "PY=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
    goto make_venv
)

:manual
echo.
echo Please install Python 3.11+ from https://www.python.org/downloads/
echo IMPORTANT: tick "Add python.exe to PATH" in the installer.
echo Then run run.bat again.
pause
exit /b 1

:make_venv
echo Using Python: %PY%
if exist ".venv" (
    rem close leftover processes from this .venv - they lock the files
    powershell -NoProfile -Command "Get-Process python,pythonw,py,pyw -ErrorAction SilentlyContinue | Where-Object { $_.Path -like '%CD%\.venv\*' -or $_.MainWindowTitle -like '*Python Launcher*' } | Stop-Process -Force" >nul 2>&1
    timeout /t 1 /nobreak >nul
    rmdir /s /q ".venv"
)
if exist ".venv" (
    echo.
    echo Cannot delete the old .venv folder - a file in it is still in use.
    echo Restart the PC, delete the .venv folder manually, then run run.bat again.
    pause
    exit /b 1
)
"%PY%" %PYA% -m venv .venv
if not exist ".venv\Scripts\python.exe" (
    echo Failed to create the virtual environment.
    pause
    exit /b 1
)
echo Installing dependencies (1-2 minutes)...
".venv\Scripts\python.exe" -m pip install --upgrade pip -q
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
    echo Dependency installation failed. See the messages above.
    rmdir /s /q ".venv"
    pause
    exit /b 1
)

:run
start "" ".venv\Scripts\pythonw.exe" -m dlbuilds
exit /b 0
