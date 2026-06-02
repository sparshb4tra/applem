#!/bin/bash
set -e
set -o pipefail

cd "$(dirname "$0")"

# Finder-launched .command files do not get the usual shell PATH.
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"

LOG_FILE="applem-launch.log"
VENV_DIR=".venv"
VENV_PYTHON="$VENV_DIR/bin/python"

run() {
    echo "$ $*" >> "$LOG_FILE"
    "$@" 2>&1 | tee -a "$LOG_FILE"
}

need_python() {
    echo "Python 3.10 or newer is required."
    echo "Opening python.org..."
    open "https://www.python.org/downloads/"
    echo ""
    echo "Install Python, then open this launcher again."
    read -r -p "Press return to close..."
    exit 1
}

if ! command -v python3 >/dev/null 2>&1; then
    need_python
fi

if ! python3 - <<'PY'
import sys
raise SystemExit(0 if sys.version_info >= (3, 10) else 1)
PY
then
    need_python
fi

if [ ! -x "$VENV_PYTHON" ]; then
    echo "Preparing app environment..."
    run python3 -m venv "$VENV_DIR"
fi

if ! "$VENV_PYTHON" -c "import yt_dlp" >/dev/null 2>&1; then
    echo "Installing downloader dependency..."
    run "$VENV_PYTHON" -m pip install --upgrade pip
    run "$VENV_PYTHON" -m pip install -r requirements.txt
fi

if ! command -v ffmpeg >/dev/null 2>&1 || ! command -v ffprobe >/dev/null 2>&1; then
    if command -v brew >/dev/null 2>&1; then
        echo "Installing FFmpeg..."
        run brew install ffmpeg
    else
        echo "FFmpeg or ffprobe is missing."
        echo "Install Homebrew from https://brew.sh, then run:"
        echo "brew install ffmpeg"
        echo ""
        read -r -p "Press return to close..."
        exit 1
    fi
fi

run "$VENV_PYTHON" app.py
