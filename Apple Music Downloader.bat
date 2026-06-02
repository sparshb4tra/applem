@echo off
setlocal
cd /d "%~dp0"

set "PYTHON_CMD="
py -3 --version >nul 2>&1
if not errorlevel 1 set "PYTHON_CMD=py -3"

if not defined PYTHON_CMD (
    python --version >nul 2>&1
    if not errorlevel 1 set "PYTHON_CMD=python"
)

if not defined PYTHON_CMD (
    echo Python is required to run Apple Music Downloader.
    echo Opening the Python download page...
    start https://www.python.org/downloads/
    pause
    exit /b 1
)

%PYTHON_CMD% -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)"
if errorlevel 1 (
    echo Python 3.10 or newer is required.
    echo Opening the Python download page...
    start https://www.python.org/downloads/
    pause
    exit /b 1
)

%PYTHON_CMD% -c "import yt_dlp" >nul 2>&1
if errorlevel 1 (
    echo Installing downloader dependency...
    %PYTHON_CMD% -m pip install -r requirements.txt
    if errorlevel 1 (
        echo Could not install yt-dlp. Check your internet connection and try again.
        pause
        exit /b 1
    )
)

where ffmpeg >nul 2>&1
if errorlevel 1 (
    where winget >nul 2>&1
    if not errorlevel 1 (
        echo Installing FFmpeg for audio conversion...
        winget install --id Gyan.FFmpeg -e --silent
    )
)

%PYTHON_CMD% app.py
