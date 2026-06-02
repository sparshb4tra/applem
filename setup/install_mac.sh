#!/bin/bash
set -e
cd "$(dirname "$0")/.."

echo "Setting up Apple Music Downloader..."

if ! command -v python3 &>/dev/null; then
    echo "Python not found. Opening python.org..."
    open "https://www.python.org/downloads/"
    echo "Install Python, then re-run this script."
    exit 1
fi

PYTHON_VERSION=$(python3 -c 'import sys; print(sys.version_info.minor)')
if [ "$PYTHON_VERSION" -lt 10 ]; then
    echo "Python 3.10 or newer required. Opening python.org..."
    open "https://www.python.org/downloads/"
    exit 1
fi

if ! command -v brew &>/dev/null; then
    echo "Installing Homebrew..."
    /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
fi

if ! command -v ffmpeg &>/dev/null; then
    echo "Installing FFmpeg (needed only for MP3 conversion)..."
    brew install ffmpeg
fi

python3 -m pip install -r requirements.txt

chmod +x "Apple Music Downloader.command"

echo ""
echo "Setup complete!"
echo "Double-click 'Apple Music Downloader.command' to start the app."
