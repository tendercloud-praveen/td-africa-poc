@echo off
title TD Africa POC - Setup
echo ==================================================
echo   TD Africa Order Bot POC - one-time setup
echo ==================================================
echo.

where python >nul 2>nul
if errorlevel 1 goto nopython
where npm >nul 2>nul
if errorlevel 1 goto nonode

echo [1/3] Creating Python virtual environment...
cd /d "%~dp0backend"
if not exist venv python -m venv venv
call venv\Scripts\activate.bat

echo [2/3] Installing backend packages. Slow internet can take a few minutes...
python -m pip install -r requirements.txt --default-timeout=200 --retries 10
if errorlevel 1 goto failbackend

echo [3/3] Installing frontend packages...
cd /d "%~dp0frontend"
call npm install
if errorlevel 1 goto failfrontend

echo.
echo ==================================================
echo   [OK] Setup finished.
echo   Now double-click  2_start_demo.bat
echo ==================================================
pause
exit /b 0

:nopython
echo [X] Python not found. Install Python 3.10 or newer from https://www.python.org
echo     Tick "Add python.exe to PATH" during install, then run this again.
pause
exit /b 1

:nonode
echo [X] Node.js not found. Install Node.js LTS from https://nodejs.org then run this again.
pause
exit /b 1

:failbackend
echo.
echo [X] Backend install failed - usually the internet. Just run 1_setup.bat again.
pause
exit /b 1

:failfrontend
echo.
echo [X] Frontend install failed - usually the internet. Just run 1_setup.bat again.
pause
exit /b 1
