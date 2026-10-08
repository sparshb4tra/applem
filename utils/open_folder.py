from __future__ import annotations

import os
import platform
import subprocess
from pathlib import Path


def open_folder(path: Path) -> bool:
    """Open a folder in the OS file manager.

    Returns False when the folder cannot be opened (it does not exist yet, the
    path is a file, or the file manager refuses) so the caller can say so.
    Raising instead used to mean a click on "Open Downloads Folder" did
    nothing visible while the traceback went to a log the user never reads.
    """
    try:
        target = Path(path).expanduser().resolve()
    except OSError:
        return False
    if not target.is_dir():
        return False

    system = platform.system()
    try:
        if system == "Windows":
            os.startfile(str(target))  # type: ignore[attr-defined]
        elif system == "Darwin":
            subprocess.Popen(["open", str(target)])
        else:
            subprocess.Popen(["xdg-open", str(target)])
    except Exception:
        return False
    return True
