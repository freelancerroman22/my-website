@echo off
title Recap Studio
cd /d "%~dp0"

echo ========================================
echo         Recap Studio - starting
echo ========================================
echo.

REM ---- Check Python ----
python --version >nul 2>&1
if errorlevel 1 (
  echo [ERROR] Python not found. Install Python first, then run again.
  echo.
  pause
  exit /b
)

REM ---- Check ffmpeg ----
ffmpeg -version >nul 2>&1
if errorlevel 1 (
  echo [ERROR] ffmpeg not found. Install ffmpeg first, then run again.
  echo.
  pause
  exit /b
)

REM ---- Check API key ----
if "%GEMINI_API_KEY%"=="" (
  echo [NOTE] GEMINI_API_KEY is not set. The clip-finding step will not work.
  echo See the setup guide to set your free Google key.
  echo.
)

REM ---- First time: create virtual environment ----
if not exist ".venv\Scripts\python.exe" (
  echo First-time setup. This can take a few minutes. Please wait...
  python -m venv .venv
)

REM ---- Install / update required packages ----
echo Installing required packages (quick after the first time)...
".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
  echo.
  echo [ERROR] Could not install packages.
  echo Please take a screenshot of this window and send it.
  echo.
  pause
  exit /b
)

echo.
echo Starting the app... your browser will open automatically.
echo (To stop, just close this black window.)
echo.

".venv\Scripts\python.exe" app.py

pause
