@echo off
REM Double-click this file to set up and launch FFVII Realtime (no typing needed).
REM Creates a private Python environment, installs the tool, fetches FFmpeg if
REM needed, and opens the app in your browser.
cd /d "%~dp0"
set "DIR=%USERPROFILE%\.ffvii-realtime"
set "BIN=%DIR%\bin"
if not exist "%BIN%" mkdir "%BIN%"

echo === FFVII Realtime setup ===

where python >nul 2>nul
if errorlevel 1 (
  echo Python 3 is required. Install it from https://www.python.org/downloads/
  echo During install, check "Add python.exe to PATH". Then run this again.
  pause
  exit /b 1
)

if not exist "%DIR%\venv" (
  echo Creating Python environment ^(one-time^)...
  python -m venv "%DIR%\venv"
)
call "%DIR%\venv\Scripts\activate.bat"
python -m pip install -q --upgrade pip
echo Installing/updating the tool...
python -m pip install -q -e .

REM FFmpeg: fetch a static build if not already available
where ffmpeg >nul 2>nul
if errorlevel 1 if not exist "%BIN%\ffmpeg.exe" (
  echo Downloading FFmpeg ^(one-time^)...
  powershell -Command "Invoke-WebRequest -Uri 'https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip' -OutFile '%DIR%\ffmpeg.zip'"
  powershell -Command "Expand-Archive -Force '%DIR%\ffmpeg.zip' '%DIR%\ffmpeg_extract'"
  for /r "%DIR%\ffmpeg_extract" %%f in (ffmpeg.exe ffprobe.exe) do copy /y "%%f" "%BIN%" >nul
  rmdir /s /q "%DIR%\ffmpeg_extract"
  del "%DIR%\ffmpeg.zip"
)

echo Starting FFVII Realtime - your browser will open shortly.
echo (Keep this window open while you use the app. Close it when you're done.)
ffvii-realtime gui
pause
