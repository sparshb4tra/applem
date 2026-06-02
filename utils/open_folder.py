from __future__ import annotations

import os
import platform
import subprocess
from pathlib import Path


def open_folder(path: Path) -> None:
    target = str(path.resolve())
    system = platform.system()
    if system == "Windows":
        os.startfile(target)  # type: ignore[attr-defined]
    elif system == "Darwin":
        subprocess.Popen(["open", target])
    else:
        subprocess.Popen(["xdg-open", target])
