@echo off
setlocal
cd /d "%~dp0\.."

echo Setting up Apple Music Downloader...
echo.

python --version >nul 2>&1
IF ERRORLEVEL 1 (
    echo Python not found.
    echo Opening python.org - install Python, then re-run this script.
    start https://www.python.org/downloads/
    pause
    exit /b 1
)

python -m pip install -r requirements.txt
IF ERRORLEVEL 1 (
    echo Something went wrong with pip install.
    echo Make sure you are connected to the internet and try again.
    pause
    exit /b 1
)

where ffmpeg >nul 2>&1
IF ERRORLEVEL 1 (
    echo FFmpeg not found. Installing via winget ^(needed only for MP3 conversion^)...
    winget install --id Gyan.FFmpeg -e --silent
)

echo.
echo Setup complete!
echo Double-click "Apple Music Downloader.bat" to start the app.
pause
