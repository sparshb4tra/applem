from __future__ import annotations

import math
import queue
import threading
import tkinter as tk
import webbrowser
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from core.config import AppConfig, save_config
from core.downloader import DownloadCancelled, DownloadControls, PlaylistDownloaderError, run_download_sync
from core.downloader import verify_playlist_downloads
from ui.settings import DownloadSettings
from utils.ffmpeg_check import detect_ffmpeg, ffmpeg_install_instructions_url
from utils.open_folder import open_folder

ASSETS_DIR = Path(__file__).resolve().parent.parent / "assets"


class MainWindow(tk.Frame):
    def __init__(self, master: tk.Tk, config: AppConfig):
        super().__init__(master, bg="#fff7fb", padx=14, pady=12)
        self.master = master
        self.config = config
        self.events: queue.Queue[dict] = queue.Queue()
        self.download_thread: threading.Thread | None = None
        self.download_in_progress = False
        self.utility_in_progress = False
        self.pause_event = threading.Event()
        self.cancel_event = threading.Event()
        self.total_items = 0
        self.current_item = 0
        self._compact_layout = False
        self._gradient_phase = 0.0
        self.logo_image: tk.PhotoImage | None = None
        self.gradient_canvas = tk.Canvas(self, highlightthickness=0, bd=0)
        self.gradient_canvas.place(x=0, y=0, relwidth=1, relheight=1)
        self._lower_gradient()
        self._build_ui()
        self.bind("<Configure>", self._on_resize)
        self.after_idle(self._refresh_responsive_layout)
        self.after_idle(self._animate_gradient)
        self.after(100, self.poll_queue)

    def _build_ui(self) -> None:
        header = ttk.Frame(self)
        header.grid(row=0, column=0, columnspan=3, sticky="ew", pady=(0, 12))
        logo_path = ASSETS_DIR / "logo_header.png"
        if logo_path.exists():
            try:
                self.logo_image = tk.PhotoImage(file=str(logo_path))
            except tk.TclError:
                self.logo_image = None
        if self.logo_image:
            ttk.Label(header, image=self.logo_image).grid(row=0, column=0, sticky="w", padx=(0, 10))
        ttk.Label(header, text="Apple Music Downloader", style="Title.TLabel").grid(row=0, column=1, sticky="w")
        header.columnconfigure(1, weight=1)

        ttk.Label(self, text="Playlist link:").grid(row=1, column=0, sticky="w")
        self.url_var = tk.StringVar()
        self.url_entry = ttk.Entry(self, textvariable=self.url_var)
        self.url_entry.grid(row=2, column=0, columnspan=3, sticky="ew", pady=(2, 12))

        ttk.Label(self, text="Save to:").grid(row=3, column=0, sticky="w")
        self.output_var = tk.StringVar(value=self.config.output_dir)
        output_entry = ttk.Entry(self, textvariable=self.output_var)
        output_entry.grid(row=4, column=0, columnspan=2, sticky="ew", pady=(2, 12))
        ttk.Button(self, text="Browse", command=self.pick_output_folder).grid(
            row=4, column=2, sticky="ew", padx=(8, 0), pady=(2, 12)
        )

        options = ttk.Frame(self)
        options.grid(row=5, column=0, columnspan=3, sticky="ew", pady=(0, 12))
        ttk.Label(options, text="Audio format:").grid(row=0, column=0, sticky="w")
        self.format_var = tk.StringVar(value=self.config.output_format or "mp3")
        ttk.Combobox(
            options,
            textvariable=self.format_var,
            state="readonly",
            values=("mp3", "wav"),
            width=12,
        ).grid(row=0, column=1, sticky="w", padx=(8, 18))

        self.skip_existing_var = tk.BooleanVar(value=self.config.skip_existing)
        ttk.Checkbutton(
            options,
            text="Skip songs already downloaded",
            variable=self.skip_existing_var,
        ).grid(row=0, column=2, sticky="e")
        options.columnconfigure(2, weight=1)

        self.controls_frame = ttk.Frame(self)
        self.controls_frame.grid(row=6, column=0, columnspan=3, sticky="ew", pady=(0, 10))
        self.download_btn = ttk.Button(
            self.controls_frame,
            text="Download Playlist",
            command=self.start_download,
            style="Primary.TButton",
        )
        self.pause_btn = ttk.Button(
            self.controls_frame,
            text="Pause",
            command=self.pause_download,
            state="disabled",
            style="Pause.TButton",
        )
        self.resume_btn = ttk.Button(
            self.controls_frame,
            text="Resume",
            command=self.resume_download,
            state="disabled",
            style="Resume.TButton",
        )
        self.cancel_btn = ttk.Button(
            self.controls_frame,
            text="Cancel",
            command=self.cancel_download,
            state="disabled",
            style="Danger.TButton",
        )
        self.control_buttons = [self.download_btn, self.pause_btn, self.resume_btn, self.cancel_btn]

        self.progress = ttk.Progressbar(self, mode="determinate")
        self.progress.grid(row=7, column=0, columnspan=3, sticky="ew", pady=(4, 4))
        self.progress_label = ttk.Label(self, text="Ready")
        self.progress_label.grid(row=8, column=0, columnspan=3, sticky="ew", pady=(0, 8))

        log_frame = ttk.Frame(self)
        log_frame.grid(row=9, column=0, columnspan=3, sticky="nsew")
        self.log_text = tk.Text(
            log_frame,
            width=1,
            height=10,
            state="disabled",
            wrap="word",
            bg="#ffffff",
            fg="#1d1d1f",
            insertbackground="#ff2d55",
            selectbackground="#ff2d55",
            selectforeground="#ffffff",
            relief="flat",
            bd=0,
            padx=10,
            pady=10,
            font=("Helvetica", 11),
        )
        self.log_text.grid(row=0, column=0, sticky="nsew")
        log_scroll = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_text.yview)
        log_scroll.grid(row=0, column=1, sticky="ns")
        self.log_text.configure(yscrollcommand=log_scroll.set)
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)

        self.utilities_frame = ttk.Frame(self)
        self.utilities_frame.grid(row=10, column=0, columnspan=3, sticky="ew", pady=(10, 0))
        self.verify_btn = ttk.Button(
            self.utilities_frame,
            text="Verify Downloads",
            command=self.start_verify,
            style="Verify.TButton",
        )
        self.retry_btn = ttk.Button(
            self.utilities_frame,
            text="Retry Missing",
            command=self.retry_missing,
            style="Retry.TButton",
        )
        self.open_folder_btn = ttk.Button(self.utilities_frame, text="Open Downloads Folder", command=self._open_output_folder)
        self.clear_log_btn = ttk.Button(self.utilities_frame, text="Clear Log", command=self.clear_log)
        self.utility_buttons = [self.verify_btn, self.retry_btn, self.open_folder_btn, self.clear_log_btn]

        self.columnconfigure(0, weight=1)
        self.columnconfigure(1, weight=1)
        self.columnconfigure(2, weight=0, minsize=96)
        self.rowconfigure(9, weight=1)
        self._layout_button_groups(compact=False)

    def _layout_button_groups(self, compact: bool) -> None:
        self._layout_buttons(self.controls_frame, self.control_buttons, compact)
        self._layout_buttons(self.utilities_frame, self.utility_buttons, compact)

    def _layout_buttons(self, parent: ttk.Frame, buttons: list[ttk.Button], compact: bool) -> None:
        for button in buttons:
            button.grid_forget()
        for index in range(4):
            parent.columnconfigure(index, weight=0)

        columns = 2 if compact else len(buttons)
        for index, button in enumerate(buttons):
            row = index // columns
            column = index % columns
            button.grid(row=row, column=column, sticky="ew", padx=4, pady=3)
        for index in range(columns):
            parent.columnconfigure(index, weight=1, uniform=str(parent))

    def _on_resize(self, event: tk.Event) -> None:
        if event.widget is not self:
            return
        self._draw_gradient(event.width, event.height)
        self._refresh_responsive_layout(event.width)

    def _draw_gradient(self, width: int, height: int) -> None:
        self.gradient_canvas.delete("gradient")
        if width <= 1 or height <= 1:
            return

        glow = math.sin(self._gradient_phase)
        top = (255, 242 + int(glow * 2), 249 + int(glow * 2))
        middle = (255, 250, 253)
        bottom = (245, 245, 247)
        split = 0.58 + (glow * 0.035)
        steps = max(height, 1)
        for y in range(steps):
            if y < steps * split:
                amount = y / max(steps * split, 1)
                start, end = top, middle
            else:
                amount = (y - steps * split) / max(steps * (1 - split), 1)
                start, end = middle, bottom
            red = int(start[0] + (end[0] - start[0]) * amount)
            green = int(start[1] + (end[1] - start[1]) * amount)
            blue = int(start[2] + (end[2] - start[2]) * amount)
            color = f"#{red:02x}{green:02x}{blue:02x}"
            self.gradient_canvas.create_line(0, y, width, y, fill=color, tags=("gradient",))
        self._lower_gradient()

    def _animate_gradient(self) -> None:
        if not self.winfo_exists():
            return
        self._gradient_phase = (self._gradient_phase + 0.045) % (math.pi * 2)
        self._draw_gradient(self.winfo_width(), self.winfo_height())
        self.after(160, self._animate_gradient)

    def _lower_gradient(self) -> None:
        self.tk.call("lower", self.gradient_canvas._w)

    def _refresh_responsive_layout(self, width: int | None = None) -> None:
        current_width = width or self.winfo_width()
        compact = current_width < 640
        if compact != self._compact_layout:
            self._compact_layout = compact
            self._layout_button_groups(compact=compact)
        self.progress_label.configure(wraplength=max(260, current_width - 36))

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

    def clear_log(self) -> None:
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

    def _settings(self) -> DownloadSettings:
        return DownloadSettings(
            output_dir=Path(self.output_var.get()),
            output_format=self.format_var.get(),
            skip_existing=self.skip_existing_var.get(),
        )

    def _set_idle_controls(self) -> None:
        self.download_btn.configure(state="normal")
        self.verify_btn.configure(state="normal")
        self.retry_btn.configure(state="normal")
        self.pause_btn.configure(state="disabled")
        self.resume_btn.configure(state="disabled")
        self.cancel_btn.configure(state="disabled")

    def _set_download_controls(self) -> None:
        self.download_btn.configure(state="disabled")
        self.verify_btn.configure(state="disabled")
        self.retry_btn.configure(state="disabled")
        self.pause_btn.configure(state="normal")
        self.resume_btn.configure(state="disabled")
        self.cancel_btn.configure(state="normal")

    def _set_utility_controls(self) -> None:
        self.download_btn.configure(state="disabled")
        self.verify_btn.configure(state="disabled")
        self.retry_btn.configure(state="disabled")
        self.pause_btn.configure(state="disabled")
        self.resume_btn.configure(state="disabled")
        self.cancel_btn.configure(state="disabled")

    def start_download(self, force_skip_existing: bool = False) -> None:
        if self.download_in_progress or self.utility_in_progress:
            return

        url = self.url_var.get().strip()
        settings = self._settings()
        if force_skip_existing:
            settings.skip_existing = True

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
        self.config.skip_existing = settings.skip_existing
        save_config(self.config)

        self.download_in_progress = True
        self.pause_event.clear()
        self.cancel_event.clear()
        self._set_download_controls()
        self.current_item = 0
        self.total_items = 0
        self.progress.stop()
        self.progress.configure(mode="determinate", value=0, maximum=100)
        self.progress_label.configure(text="Starting...")

        self.download_thread = threading.Thread(
            target=self._worker,
            args=(url, settings),
            daemon=True,
        )
        self.download_thread.start()

    def retry_missing(self) -> None:
        self._append_log("Retrying missing songs. Existing files will be skipped.")
        self.start_download(force_skip_existing=True)

    def pause_download(self) -> None:
        if not self.download_in_progress:
            return
        self.pause_event.set()
        self.pause_btn.configure(state="disabled")
        self.resume_btn.configure(state="normal")
        self.progress_label.configure(text="Paused")
        self._append_log("Paused. The current song may finish its current step first.")

    def resume_download(self) -> None:
        if not self.download_in_progress:
            return
        self.pause_event.clear()
        self.pause_btn.configure(state="normal")
        self.resume_btn.configure(state="disabled")
        self._append_log("Resumed.")

    def cancel_download(self) -> None:
        if not self.download_in_progress:
            return
        self.cancel_event.set()
        self.pause_event.clear()
        self.cancel_btn.configure(state="disabled")
        self.pause_btn.configure(state="disabled")
        self.resume_btn.configure(state="disabled")
        self.progress_label.configure(text="Cancelling...")
        self._append_log("Cancelling. The current song may need a moment to stop.")

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
                skip_existing=settings.skip_existing,
                controls=DownloadControls(pause_event=self.pause_event, cancel_event=self.cancel_event),
            )
            self.events.put({"type": "done", "failures": len(failures), "output_dir": str(settings.output_dir)})
        except DownloadCancelled as exc:
            self.events.put({"type": "cancelled", "message": str(exc), "output_dir": str(settings.output_dir)})
        except PlaylistDownloaderError as exc:
            self.events.put({"type": "fatal", "message": str(exc)})
        except Exception as exc:
            msg = str(exc).lower()
            if "name or service not known" in msg or "timed out" in msg or "network" in msg:
                text = "No internet connection. Check your Wi-Fi and try again."
            else:
                text = "Something went wrong. Please try again."
            self.events.put({"type": "fatal", "message": text})

    def start_verify(self) -> None:
        if self.download_in_progress or self.utility_in_progress:
            return
        url = self.url_var.get().strip()
        settings = self._settings()
        if not url:
            messagebox.showerror("Missing link", "Paste an Apple Music playlist link first.")
            return

        self.config.output_dir = str(settings.output_dir)
        self.config.output_format = settings.output_format
        self.config.skip_existing = settings.skip_existing
        save_config(self.config)

        self.utility_in_progress = True
        self._set_utility_controls()
        self.progress.configure(mode="indeterminate")
        self.progress.start(12)
        self.progress_label.configure(text="Verifying downloads...")
        self._append_log("Verifying downloaded files...")
        thread = threading.Thread(target=self._verify_worker, args=(url, settings), daemon=True)
        thread.start()

    def _verify_worker(self, url: str, settings: DownloadSettings) -> None:
        try:
            result = verify_playlist_downloads(url, settings.output_dir, settings.output_format)
            self.events.put(
                {
                    "type": "verify_done",
                    "total": result.total,
                    "present": result.present,
                    "missing": len(result.missing),
                    "incomplete": len(result.incomplete),
                    "report_path": str(result.report_path),
                }
            )
        except PlaylistDownloaderError as exc:
            self.events.put({"type": "verify_error", "message": str(exc)})
        except Exception:
            self.events.put({"type": "verify_error", "message": "Could not verify downloads. Please try again."})

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
                    self._set_idle_controls()
                    self.progress_label.configure(text="Stopped")
                elif event_type == "cancelled":
                    self._append_log("Stopped by user.")
                    self.download_in_progress = False
                    self._set_idle_controls()
                    self.progress_label.configure(text="Cancelled")
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
                    self._set_idle_controls()
                    self.progress_label.configure(text="Completed")
                    open_folder(Path(event["output_dir"]))
                elif event_type == "verify_done":
                    self.progress.stop()
                    self.progress.configure(mode="determinate", value=0, maximum=100)
                    self.utility_in_progress = False
                    self._set_idle_controls()
                    total = int(event["total"])
                    present = int(event["present"])
                    missing = int(event["missing"])
                    incomplete = int(event["incomplete"])
                    self.progress_label.configure(text=f"Verified: {present} of {total} ready")
                    self._append_log(
                        f"VERIFY  {present}/{total} ready, {missing} missing, {incomplete} incomplete."
                    )
                    self._append_log(f"Report: {event['report_path']}")
                    if missing or incomplete:
                        messagebox.showwarning(
                            "Verification finished",
                            f"{present} of {total} songs are ready.\n"
                            f"{missing} missing, {incomplete} incomplete.\n"
                            "Click Retry Missing to fill the gaps.",
                        )
                    else:
                        messagebox.showinfo("Verification finished", "Everything is downloaded.")
                elif event_type == "verify_error":
                    self.progress.stop()
                    self.progress.configure(mode="determinate", value=0, maximum=100)
                    self.utility_in_progress = False
                    self._set_idle_controls()
                    self.progress_label.configure(text="Verification stopped")
                    self._append_log(f"ERROR  {event['message']}")
                    messagebox.showerror("Verification error", event["message"])
        except queue.Empty:
            pass
        self.after(100, self.poll_queue)
