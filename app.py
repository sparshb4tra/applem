from __future__ import annotations

import importlib.util
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
    bg = "#fff5fb"
    panel = "#ffffff"
    text = "#171017"
    muted = "#6f6470"
    border = "#f3bddb"
    pink = "#ff1493"
    pink_dark = "#d60073"
    pink_soft = "#ffe1f1"
    grey = "#e8e4e8"
    font = ("Helvetica", 11, "bold")
    title_font = ("Helvetica", 18, "bold")

    style = ttk.Style(root)
    style.theme_use("clam")

    root.configure(bg=bg)
    root.option_add("*Font", font)

    style.configure(".", font=font, background=bg, foreground=text)
    style.configure("TFrame", background=bg)
    style.configure("TLabel", background=bg, foreground=text, font=font)
    style.configure("Title.TLabel", background=bg, foreground=pink, font=title_font)
    style.configure("Muted.TLabel", background=bg, foreground=muted, font=font)

    style.configure(
        "TButton",
        padding=(12, 8),
        background=pink,
        foreground="#ffffff",
        bordercolor=pink_dark,
        lightcolor=pink,
        darkcolor=pink_dark,
        relief="flat",
        font=font,
    )
    style.map(
        "TButton",
        background=[("disabled", grey), ("pressed", pink_dark), ("active", pink_dark)],
        foreground=[("disabled", muted), ("pressed", "#ffffff"), ("active", "#ffffff")],
        bordercolor=[("disabled", grey), ("pressed", pink_dark), ("active", pink_dark)],
    )

    style.configure(
        "TEntry",
        padding=7,
        fieldbackground=panel,
        background=panel,
        foreground=text,
        bordercolor=border,
        insertcolor=pink,
        relief="flat",
        font=font,
    )
    style.map("TEntry", bordercolor=[("focus", pink), ("!focus", border)])

    style.configure(
        "TCombobox",
        padding=5,
        fieldbackground=panel,
        background=pink_soft,
        foreground=text,
        bordercolor=border,
        arrowcolor=pink,
        relief="flat",
        font=font,
    )
    style.map(
        "TCombobox",
        fieldbackground=[("readonly", panel)],
        bordercolor=[("focus", pink), ("!focus", border)],
        arrowcolor=[("active", pink_dark), ("!active", pink)],
    )

    style.configure(
        "TCheckbutton",
        padding=4,
        background=bg,
        foreground=text,
        indicatorbackground=panel,
        indicatorforeground=pink,
        font=font,
    )
    style.map(
        "TCheckbutton",
        background=[("active", bg)],
        foreground=[("disabled", muted), ("active", pink_dark)],
        indicatorbackground=[("selected", pink), ("!selected", panel)],
    )

    style.configure(
        "Horizontal.TProgressbar",
        background=pink,
        troughcolor="#f1edf1",
        bordercolor="#f1edf1",
        lightcolor=pink,
        darkcolor=pink_dark,
    )
    style.configure(
        "Vertical.TScrollbar",
        background=pink_soft,
        troughcolor="#f1edf1",
        bordercolor=border,
        arrowcolor=pink,
        relief="flat",
    )


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
    root.geometry("820x640")
    root.minsize(520, 520)
    _configure_style(root)

    _launch_main(root)

    root.mainloop()


if __name__ == "__main__":
    main()
