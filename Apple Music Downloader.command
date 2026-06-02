#!/bin/bash
cd "$(dirname "$0")"

if ! command -v python3 >/dev/null 2>&1; then
    echo "Python is required to run Apple Music Downloader."
    open "https://www.python.org/downloads/"
    exit 1
fi

python3 - <<'PY'
import sys
raise SystemExit(0 if sys.version_info >= (3, 10) else 1)
PY
if [ $? -ne 0 ]; then
    echo "Python 3.10 or newer is required."
    open "https://www.python.org/downloads/"
    exit 1
fi

python3 -c "import yt_dlp" >/dev/null 2>&1
if [ $? -ne 0 ]; then
    echo "Installing downloader dependency..."
    python3 -m pip install -r requirements.txt || exit 1
fi

if ! command -v ffmpeg >/dev/null 2>&1 && command -v brew >/dev/null 2>&1; then
    echo "Installing FFmpeg for audio conversion..."
    brew install ffmpeg
fi

python3 app.py
