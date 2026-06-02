from __future__ import annotations

import importlib.util
import platform
import sys
import tkinter as tk
from tkinter import messagebox, ttk

from core.config import load_config, save_config
from ui.main_window import MainWindow


def _check_python_version() -> None:
    if sys.version_info < (3, 10):
        raise RuntimeError("Python 3.10 or newer is required.")


def _check_required_packages() -> None:
    if importlib.util.find_spec("yt_dlp") is None:
        raise RuntimeError("yt-dlp is not installed. Run install script first.")


def _configure_style(root: tk.Tk) -> None:
    style = ttk.Style(root)
    style.theme_use("vista" if platform.system() == "Windows" else "clam")
    default_font = ("TkDefaultFont", 11)
    root.option_add("*Font", default_font)
    style.configure("TButton", padding=8)
    style.configure("TEntry", padding=6)
    style.configure("TCheckbutton", padding=3)


def _show_startup_error(message: str) -> None:
    root = tk.Tk()
    root.withdraw()
    messagebox.showerror("Startup error", message)
    root.destroy()


def _launch_main(root: tk.Tk) -> None:
    config = load_config()
    frame = MainWindow(root, config=config)
    frame.pack(fill="both", expand=True)


def main() -> None:
    try:
        _check_python_version()
        _check_required_packages()
    except RuntimeError as exc:
        _show_startup_error(str(exc))
        return

    config = load_config()
    save_config(config)

    root = tk.Tk()
    root.title("Apple Music Downloader")
    root.geometry("760x620")
    root.minsize(700, 560)
    _configure_style(root)

    _launch_main(root)

    root.mainloop()


if __name__ == "__main__":
    main()
