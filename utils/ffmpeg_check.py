from __future__ import annotations

import os
import platform
import shutil
from pathlib import Path


def _ffmpeg_dir_from(paths: list[Path]) -> str:
    for path in paths:
        if path.exists() and (path.parent / _ffprobe_name()).exists():
            return str(path.parent)
    return ""


def _ffmpeg_name() -> str:
    return "ffmpeg.exe" if platform.system() == "Windows" else "ffmpeg"


def _ffprobe_name() -> str:
    return "ffprobe.exe" if platform.system() == "Windows" else "ffprobe"


def detect_ffmpeg() -> tuple[bool, str]:
    ffmpeg_path = shutil.which("ffmpeg")
    ffprobe_path = shutil.which("ffprobe")
    if ffmpeg_path and ffprobe_path:
        ffmpeg_dir = Path(ffmpeg_path).parent
        if ffmpeg_dir == Path(ffprobe_path).parent:
            return True, str(ffmpeg_dir)

    system = platform.system()
    if system == "Windows":
        candidates = [
            Path("C:/ffmpeg/bin") / _ffmpeg_name(),
            Path(os.environ.get("ProgramFiles", "")) / "FFmpeg" / "bin" / _ffmpeg_name(),
            Path(os.environ.get("LOCALAPPDATA", ""))
            / "Microsoft"
            / "WinGet"
            / "Packages",
        ]
        package_root = candidates.pop()
        if package_root.exists():
            candidates.extend(package_root.glob(f"*FFmpeg*/**/{_ffmpeg_name()}"))
        found = _ffmpeg_dir_from(candidates)
        if found:
            return True, found

    if system == "Darwin":
        found = _ffmpeg_dir_from([Path("/opt/homebrew/bin/ffmpeg"), Path("/usr/local/bin/ffmpeg")])
        if found:
            return True, found

    return False, ""


def ffmpeg_install_instructions_url() -> str:
    return "https://ffmpeg.org/download.html"
