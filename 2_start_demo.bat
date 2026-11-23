@echo off
title TD Africa POC - Start
echo Starting backend (port 8000) and frontend (port 5173)...
start "TD Africa - BACKEND" cmd /k "%~dp0run_backend.bat"
timeout /t 5 /nobreak >nul
start "TD Africa - FRONTEND" cmd /k "%~dp0run_frontend.bat"
timeout /t 6 /nobreak >nul
start "" http://localhost:5173
echo.
echo Demo opened in your browser: http://localhost:5173
echo API docs: http://127.0.0.1:8000/docs
echo To stop: close the two black windows.
timeout /t 8 >nul
