@echo off
title Recap Studio - App Builder
cd /d "%~dp0"

echo =================================================
echo   Recap Studio - একবার .exe অ্যাপ বানানো হচ্ছে
echo =================================================
echo.
echo এটা একবার চালালেই একটা Recap Studio.exe তৈরি হবে,
echo যেটা ডেস্কটপে রেখে ডাবল-ক্লিক করে চালাতে পারবে।
echo কয়েক মিনিট লাগবে, ধৈর্য ধরো।
echo.

REM ---- Python আছে কিনা ----
python --version >nul 2>&1
if errorlevel 1 ( echo [ERROR] Python not found. & pause & exit /b )

REM ---- virtual environment ----
if not exist ".venv\Scripts\python.exe" ( python -m venv .venv )
call ".venv\Scripts\activate.bat"

echo দরকারি জিনিস ইনস্টল হচ্ছে...
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m pip install pyinstaller

echo.
echo অ্যাপ বিল্ড হচ্ছে...
pyinstaller --noconfirm --onefile --windowed --name "Recap Studio" ^
  --icon app.ico ^
  --add-data "templates;templates" ^
  --add-data "static;static" ^
  --collect-all google ^
  --collect-all google_genai ^
  --collect-all webview ^
  --collect-all flask ^
  app.py

if errorlevel 1 (
  echo.
  echo [ERROR] বিল্ড হয়নি। এই উইন্ডোর স্ক্রিনশট পাঠাও।
  echo.
  pause
  exit /b
)

echo.
echo ====================================================
echo  হয়ে গেছে!  dist\Recap Studio.exe  তৈরি হয়েছে।
echo  ওই exe ফাইলটা ডেস্কটপে কপি করে ডাবল-ক্লিক করো।
echo ====================================================
echo.
echo মনে রাখো: ffmpeg ইনস্টল থাকতে হবে আর GEMINI_API_KEY সেট থাকতে হবে
echo (যেগুলো তুমি আগেই করেছ)।
echo.

REM dist ফোল্ডার খুলে দিই
explorer dist

pause
