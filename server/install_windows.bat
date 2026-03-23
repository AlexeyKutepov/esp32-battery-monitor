@echo off
setlocal
cd /d %~dp0

where py >nul 2>nul
if %errorlevel% neq 0 (
  echo Python Launcher ^(py^) not found. Install Python 3.11+ from https://www.python.org/downloads/ and try again.
  pause
  exit /b 1
)

py -3 -m venv .venv
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip
pip install -r requirements.txt

echo.
echo Installation complete. Use start_windows.bat to run the server.
pause
