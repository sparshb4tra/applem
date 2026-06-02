from __future__ import annotations

import importlib.util
import sys
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

from core.config import load_config, save_config
from ui.main_window import MainWindow

ASSETS_DIR = Path(__file__).resolve().parent / "assets"


def _check_python_version() -> None:
    if sys.version_info < (3, 10):
        raise RuntimeError("Python 3.10 or newer is required.")


def _check_required_packages() -> None:
    if importlib.util.find_spec("yt_dlp") is None:
        raise RuntimeError("yt-dlp is not installed. Run install script first.")


def _configure_style(root: tk.Tk) -> None:
    bg = "#fff7fb"
    panel = "#ffffff"
    text = "#1d1d1f"
    muted = "#6e6e73"
    border = "#d2d2d7"
    accent = "#ff2d55"
    accent_dark = "#d7003f"
    soft_accent = "#fff0f4"
    control = "#ffffff"
    control_hover = "#f2f2f4"
    control_pressed = "#e8e8ed"
    disabled = "#eeeeef"
    danger = "#ff3b30"
    danger_dark = "#d70015"
    black = "#1d1d1f"
    body_font = ("Helvetica", 11)
    button_font = ("Helvetica", 11, "bold")
    title_font = ("Helvetica", 19, "bold")

    style = ttk.Style(root)
    style.theme_use("clam")

    root.configure(bg=bg)
    root.option_add("*Font", body_font)

    style.configure(".", font=body_font, background=bg, foreground=text)
    style.configure("TFrame", background=bg)
    style.configure("TLabel", background=bg, foreground=text, font=body_font)
    style.configure("Title.TLabel", background=bg, foreground=text, font=title_font)
    style.configure("Muted.TLabel", background=bg, foreground=muted, font=body_font)

    style.configure(
        "TButton",
        padding=(12, 8),
        background=control,
        foreground=text,
        bordercolor=border,
        lightcolor=control,
        darkcolor=border,
        relief="flat",
        font=button_font,
    )
    style.map(
        "TButton",
        background=[("disabled", disabled), ("pressed", control_pressed), ("active", control_hover)],
        foreground=[("disabled", muted), ("pressed", text), ("active", text)],
        bordercolor=[("disabled", disabled), ("pressed", border), ("active", border)],
    )

    style.configure(
        "Primary.TButton",
        background=black,
        foreground="#ffffff",
        bordercolor=black,
        lightcolor=black,
        darkcolor=black,
    )
    style.map(
        "Primary.TButton",
        background=[("disabled", disabled), ("pressed", "#000000"), ("active", "#2c2c2e")],
        foreground=[("disabled", muted), ("pressed", "#ffffff"), ("active", "#ffffff")],
        bordercolor=[("disabled", disabled), ("pressed", "#000000"), ("active", "#2c2c2e")],
    )

    style.configure(
        "Danger.TButton",
        background=control,
        foreground=danger,
        bordercolor=border,
        lightcolor=control,
        darkcolor=border,
    )
    style.map(
        "Danger.TButton",
        background=[("disabled", disabled), ("pressed", "#fff2f1"), ("active", "#fff5f4")],
        foreground=[("disabled", muted), ("pressed", danger_dark), ("active", danger_dark)],
        bordercolor=[("disabled", disabled), ("pressed", danger), ("active", danger)],
    )

    style.configure(
        "TEntry",
        padding=7,
        fieldbackground=panel,
        background=panel,
        foreground=text,
        bordercolor=border,
        insertcolor=accent,
        relief="flat",
        font=body_font,
    )
    style.map("TEntry", bordercolor=[("focus", accent), ("!focus", border)])

    style.configure(
        "TCombobox",
        padding=5,
        fieldbackground=panel,
        background=control,
        foreground=text,
        bordercolor=border,
        arrowcolor=muted,
        relief="flat",
        font=body_font,
    )
    style.map(
        "TCombobox",
        fieldbackground=[("readonly", panel)],
        bordercolor=[("focus", accent), ("!focus", border)],
        arrowcolor=[("active", accent), ("!active", muted)],
    )

    style.configure(
        "TCheckbutton",
        padding=4,
        background=bg,
        foreground=text,
        indicatorbackground=panel,
        indicatorforeground=accent,
        font=body_font,
    )
    style.map(
        "TCheckbutton",
        background=[("active", bg)],
        foreground=[("disabled", muted), ("active", text)],
        indicatorbackground=[("selected", accent), ("!selected", panel)],
    )

    style.configure(
        "Horizontal.TProgressbar",
        background=accent,
        troughcolor="#e8e8ed",
        bordercolor="#e8e8ed",
        lightcolor=accent,
        darkcolor=accent_dark,
    )
    style.configure(
        "Vertical.TScrollbar",
        background=soft_accent,
        troughcolor="#e8e8ed",
        bordercolor=border,
        arrowcolor=accent,
        relief="flat",
    )


def _set_window_icon(root: tk.Tk) -> None:
    icon_path = ASSETS_DIR / "icon.png"
    if not icon_path.exists():
        return
    try:
        icon = tk.PhotoImage(file=str(icon_path))
    except tk.TclError:
        return
    root.iconphoto(True, icon)
    root._applem_icon = icon  # keep a Tcl image reference alive


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
    _set_window_icon(root)

    _launch_main(root)

    root.mainloop()


if __name__ == "__main__":
    main()
