from __future__ import annotations

import queue
import threading
import tkinter as tk
import webbrowser
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from core.config import AppConfig, save_config
from core.downloader import PlaylistDownloaderError, run_download_sync
from ui.settings import DownloadSettings
from utils.ffmpeg_check import detect_ffmpeg, ffmpeg_install_instructions_url
from utils.open_folder import open_folder


class MainWindow(ttk.Frame):
    def __init__(self, master: tk.Tk, config: AppConfig):
        super().__init__(master, padding=16)
        self.master = master
        self.config = config
        self.events: queue.Queue[dict] = queue.Queue()
        self.download_thread: threading.Thread | None = None
        self.download_in_progress = False
        self.total_items = 0
        self.current_item = 0
        self._build_ui()
        self.after(100, self.poll_queue)

    def _build_ui(self) -> None:
        ttk.Label(self, text="Apple Music Downloader", font=("TkDefaultFont", 16, "bold")).grid(
            row=0, column=0, columnspan=3, sticky="w", pady=(0, 12)
        )

        ttk.Label(self, text="Playlist link:").grid(row=1, column=0, sticky="w")
        self.url_var = tk.StringVar()
        self.url_entry = ttk.Entry(self, textvariable=self.url_var, width=64)
        self.url_entry.grid(row=2, column=0, columnspan=3, sticky="ew", pady=(2, 12))

        ttk.Label(self, text="Save to:").grid(row=3, column=0, sticky="w")
        self.output_var = tk.StringVar(value=self.config.output_dir)
        output_entry = ttk.Entry(self, textvariable=self.output_var, width=50)
        output_entry.grid(row=4, column=0, columnspan=2, sticky="ew", pady=(2, 12))
        ttk.Button(self, text="...", width=4, command=self.pick_output_folder).grid(row=4, column=2, sticky="e")

        ttk.Label(self, text="Audio format:").grid(row=5, column=0, sticky="w")
        self.format_var = tk.StringVar(value=self.config.output_format or "mp3")
        ttk.Combobox(
            self,
            textvariable=self.format_var,
            state="readonly",
            values=("mp3", "wav"),
            width=12,
        ).grid(row=5, column=1, sticky="w", pady=(0, 12))

        self.download_btn = ttk.Button(self, text="Download Playlist", command=self.start_download)
        self.download_btn.grid(row=6, column=0, columnspan=3, pady=(0, 12))

        self.progress = ttk.Progressbar(self, mode="determinate", length=500)
        self.progress.grid(row=7, column=0, columnspan=3, sticky="ew", pady=(4, 4))
        self.progress_label = ttk.Label(self, text="Ready")
        self.progress_label.grid(row=8, column=0, columnspan=3, sticky="w", pady=(0, 8))

        self.log_text = tk.Text(self, width=80, height=12, state="disabled", wrap="word")
        self.log_text.grid(row=9, column=0, columnspan=3, sticky="nsew")

        ttk.Button(self, text="Open Downloads Folder", command=self._open_output_folder).grid(
            row=10, column=0, columnspan=3, pady=(10, 0)
        )

        self.columnconfigure(0, weight=1)
        self.columnconfigure(1, weight=1)
        self.rowconfigure(9, weight=1)

    def pick_output_folder(self) -> None:
        selected = filedialog.askdirectory(title="Choose download folder")
        if selected:
            self.output_var.set(selected)

    def _open_output_folder(self) -> None:
        open_folder(Path(self.output_var.get()))

    def _append_log(self, line: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", f"{line}\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _settings(self) -> DownloadSettings:
        return DownloadSettings(output_dir=Path(self.output_var.get()), output_format=self.format_var.get())

    def start_download(self) -> None:
        if self.download_in_progress:
            return

        url = self.url_var.get().strip()
        settings = self._settings()

        if not url:
            messagebox.showerror("Missing link", "Paste an Apple Music playlist link first.")
            return

        has_ffmpeg, _ = detect_ffmpeg()
        if not has_ffmpeg:
            if messagebox.askyesno(
                "FFmpeg required",
                "FFmpeg is needed for audio conversion. Open install instructions?",
            ):
                webbrowser.open(ffmpeg_install_instructions_url())
            return

        self.config.output_dir = str(settings.output_dir)
        self.config.output_format = settings.output_format
        save_config(self.config)

        self.download_in_progress = True
        self.download_btn.configure(state="disabled")
        self.current_item = 0
        self.total_items = 0
        self.progress.configure(value=0, maximum=100)
        self.progress_label.configure(text="Starting...")

        self.download_thread = threading.Thread(
            target=self._worker,
            args=(url, settings),
            daemon=True,
        )
        self.download_thread.start()

    def _worker(self, url: str, settings: DownloadSettings) -> None:
        def progress_callback(current: int, total: int, track_name: str) -> None:
            self.events.put({"type": "progress", "current": current, "total": total, "track": track_name})

        def log_callback(line: str) -> None:
            self.events.put({"type": "log", "line": line})

        def error_callback(track_name: str, reason: str) -> None:
            self.events.put({"type": "track_error", "track": track_name, "reason": reason})

        try:
            has_ffmpeg, ffmpeg_path = detect_ffmpeg()
            if not has_ffmpeg:
                raise PlaylistDownloaderError(
                    "FFmpeg is needed for audio conversion. Click here for install instructions."
                )
            failures = run_download_sync(
                url=url,
                output_dir=settings.output_dir,
                progress_callback=progress_callback,
                log_callback=log_callback,
                error_callback=error_callback,
                output_format=settings.output_format,
                ffmpeg_path=ffmpeg_path or "ffmpeg",
            )
            self.events.put({"type": "done", "failures": len(failures), "output_dir": str(settings.output_dir)})
        except PlaylistDownloaderError as exc:
            self.events.put({"type": "fatal", "message": str(exc)})
        except Exception as exc:
            msg = str(exc).lower()
            if "name or service not known" in msg or "timed out" in msg or "network" in msg:
                text = "No internet connection. Check your Wi-Fi and try again."
            else:
                text = "Something went wrong. Please try again."
            self.events.put({"type": "fatal", "message": text})

    def poll_queue(self) -> None:
        try:
            while True:
                event = self.events.get_nowait()
                event_type = event.get("type")
                if event_type == "progress":
                    self.current_item = int(event["current"])
                    self.total_items = int(event["total"])
                    max_val = self.total_items if self.total_items else 1
                    self.progress.configure(maximum=max_val, value=self.current_item)
                    self.progress_label.configure(
                        text=f"{self.current_item} of {self.total_items} songs - {event['track']}"
                    )
                elif event_type == "log":
                    self._append_log(event["line"])
                elif event_type == "track_error":
                    self._append_log(f"WARN  {event['track']} - {event['reason']}")
                elif event_type == "fatal":
                    self._append_log(f"ERROR  {event['message']}")
                    messagebox.showerror("Download error", event["message"])
                    self.download_in_progress = False
                    self.download_btn.configure(state="normal")
                    self.progress_label.configure(text="Stopped")
                elif event_type == "done":
                    fail_count = int(event["failures"])
                    if fail_count:
                        messagebox.showwarning(
                            "Finished with warnings",
                            f"Done. {fail_count} song(s) failed. Check failed_downloads.txt in your output folder.",
                        )
                    else:
                        messagebox.showinfo("Done", "Download complete.")
                    self.download_in_progress = False
                    self.download_btn.configure(state="normal")
                    self.progress_label.configure(text="Completed")
                    open_folder(Path(event["output_dir"]))
        except queue.Empty:
            pass
        self.after(100, self.poll_queue)
