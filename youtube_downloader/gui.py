from __future__ import annotations

import threading
import tkinter as tk
from tkinter import filedialog, ttk, messagebox, Menu
import webbrowser
import subprocess
import os
try:
    import requests  # optional for thumbnails
    from PIL import Image, ImageTk  # optional for thumbnails
except Exception:
    requests = None
    Image = None
    ImageTk = None
from pathlib import Path

from . import config
from .downloader import download_audio, download_video, get_video_metadata, DownloadTask
from .utils import notify


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("YouTube Downloader")
        self.geometry("800x600")
        self.minsize(720, 480)

        self.url_var = tk.StringVar()
        self.output_dir = tk.StringVar(value=str(config.DEFAULT_DOWNLOAD_DIR))
        self.mode_var = tk.StringVar(value="video")
        self.format_var = tk.StringVar(value=config.DEFAULT_VIDEO_FORMAT)
        self.quality_var = tk.StringVar(value="highest")
        self.subs_var = tk.StringVar(value="")
        self.embed_subs_var = tk.BooleanVar(value=False)
        self.start_var = tk.StringVar(value="")
        self.end_var = tk.StringVar(value="")
        self._theme = getattr(config, "DEFAULT_THEME", "light")

        self.tasks: list[DownloadTask] = []
        self.queue_paths: list[str | None] = []

        self._init_styles()
        self._build_ui()

    def _build_ui(self):
        pad = {"padx": 8, "pady": 6}
        frm = ttk.Frame(self)
        frm.pack(fill=tk.BOTH, expand=True)

        # Menu bar
        menubar = Menu(self)
        settings_menu = Menu(menubar, tearoff=0)
        settings_menu.add_command(label="Toggle Theme", command=self._toggle_theme)
        settings_menu.add_command(label="Preferences", command=self._show_preferences)
        settings_menu.add_separator()
        settings_menu.add_command(label="Help", command=self._show_help)
        settings_menu.add_command(label="About", command=self._show_about)
        menubar.add_cascade(label="Settings", menu=settings_menu)
        self.config(menu=menubar)

        ttk.Label(frm, text="YouTube URL").grid(row=0, column=0, sticky="w", **pad)
        ttk.Entry(frm, textvariable=self.url_var, width=60).grid(row=0, column=1, columnspan=3, sticky="ew", **pad)

        ttk.Label(frm, text="Mode").grid(row=1, column=0, sticky="w", **pad)
        ttk.Combobox(frm, textvariable=self.mode_var, values=["video", "audio"], width=10, state="readonly").grid(row=1, column=1, sticky="w", **pad)

        ttk.Label(frm, text="Format").grid(row=1, column=2, sticky="w", **pad)
        ttk.Combobox(frm, textvariable=self.format_var, values=["mp4", "webm", "mp3"], width=10, state="readonly").grid(row=1, column=3, sticky="w", **pad)

        ttk.Label(frm, text="Quality").grid(row=2, column=0, sticky="w", **pad)
        ttk.Combobox(frm, textvariable=self.quality_var, values=["highest", "1080p", "720p", "480p", "360p"], width=10, state="readonly").grid(row=2, column=1, sticky="w", **pad)
        ttk.Label(frm, text="Subs (e.g., en,es)").grid(row=2, column=2, sticky="w", **pad)
        ttk.Entry(frm, textvariable=self.subs_var, width=12).grid(row=2, column=3, sticky="w", **pad)

        ttk.Label(frm, text="Folder").grid(row=3, column=0, sticky="w", **pad)
        ttk.Entry(frm, textvariable=self.output_dir, width=50).grid(row=3, column=1, columnspan=2, sticky="ew", **pad)
        ttk.Button(frm, text="Browse", command=self._choose_folder).grid(row=3, column=3, sticky="e", **pad)

        ttk.Checkbutton(frm, text="Embed Subs", variable=self.embed_subs_var).grid(row=4, column=0, sticky="w", **pad)
        ttk.Label(frm, text="Start").grid(row=4, column=1, sticky="e", **pad)
        ttk.Entry(frm, textvariable=self.start_var, width=10).grid(row=4, column=2, sticky="w", **pad)
        ttk.Label(frm, text="End").grid(row=4, column=3, sticky="w", **pad)
        ttk.Entry(frm, textvariable=self.end_var, width=10).grid(row=4, column=3, sticky="e", **pad)

        self.progress = ttk.Progressbar(frm, orient="horizontal", mode="indeterminate")
        self.progress.grid(row=5, column=0, columnspan=4, sticky="ew", padx=8, pady=(12, 6))

        btns = ttk.Frame(frm)
        btns.grid(row=6, column=0, columnspan=4, sticky="e", **pad)
        ttk.Button(btns, text="Get Info", style="Accent.TButton", command=self._show_metadata).pack(side=tk.LEFT, padx=6)
        ttk.Button(btns, text="Download", style="Accent.TButton", command=self._download).pack(side=tk.LEFT, padx=6)
        ttk.Button(btns, text="Cancel Last", command=self._cancel_last).pack(side=tk.LEFT, padx=6)

        # Metadata preview (inline)
        preview = ttk.LabelFrame(frm, text="Metadata Preview")
        preview.grid(row=7, column=0, columnspan=4, sticky="nsew", padx=8, pady=(6, 8))
        self.thumb_label = ttk.Label(preview)
        self.thumb_label.grid(row=0, column=0, sticky="nw", padx=6, pady=6)
        self.meta_text = tk.Text(preview, height=5, wrap="word", relief="flat", bd=0)
        self.meta_text.grid(row=0, column=1, sticky="nsew", padx=6, pady=6)
        # Set text colors to match theme
        if getattr(self, "_theme", "light") == "dark":
            self.meta_text.configure(bg="#111827", fg="#e5e7eb")
        else:
            self.meta_text.configure(bg="#ffffff", fg="#0f172a")
        preview.columnconfigure(1, weight=1)

        # Queue list with context menu
        queue_frame = ttk.LabelFrame(frm, text="Download Queue")
        queue_frame.grid(row=8, column=0, columnspan=4, sticky="nsew", padx=8, pady=(6, 8))
        self.queue_box = tk.Listbox(queue_frame, height=6)
        self.queue_box.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)
        self.queue_box.bind("<Button-3>", self._queue_context_menu)

        frm.columnconfigure(1, weight=1)
        frm.columnconfigure(2, weight=1)
        frm.rowconfigure(7, weight=1)
        frm.rowconfigure(8, weight=2)

        # Shortcuts and clipboard polling
        self.bind("<Control-v>", lambda e: self._paste_url())
        self.bind("<Return>", lambda e: self._download())
        self.bind("<Control-i>", lambda e: self._show_metadata())
        self._poll_clipboard()

    def _choose_folder(self):
        path = filedialog.askdirectory(initialdir=self.output_dir.get() or str(config.DEFAULT_DOWNLOAD_DIR))
        if path:
            self.output_dir.set(path)

    def _show_metadata(self):
        url = self.url_var.get().strip()
        if not url:
            messagebox.showwarning("Missing URL", "Please enter a YouTube URL.")
            return
        try:
            md = get_video_metadata(url)
            # Inline text
            self.meta_text.delete("1.0", tk.END)
            self.meta_text.insert(tk.END, f"Title: {md.title}\nChannel: {md.author}\nDuration: {md.length_seconds}s\nViews: {md.views}")
            # Thumbnail
            if requests and Image and ImageTk and md.thumbnail_url:
                try:
                    r = requests.get(md.thumbnail_url, timeout=10, headers={
                        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
                        "Accept-Language": "en-US,en;q=0.9",
                    })
                    r.raise_for_status()
                    from io import BytesIO
                    img = Image.open(BytesIO(r.content)).resize((160, 90))
                    self._thumb_img = ImageTk.PhotoImage(img)
                    self.thumb_label.configure(image=self._thumb_img)
                except Exception:
                    self.thumb_label.configure(image="")
            else:
                self.thumb_label.configure(image="")
        except Exception as exc:
            messagebox.showerror("Error", f"Failed to fetch metadata: {exc}")

    def _download(self):
        url = self.url_var.get().strip()
        if not url:
            messagebox.showwarning("Missing URL", "Please enter a YouTube URL.")
            return
        out = Path(self.output_dir.get() or config.DEFAULT_DOWNLOAD_DIR)
        out.mkdir(parents=True, exist_ok=True)
        mode = self.mode_var.get()
        fmt = self.format_var.get()
        quality = self.quality_var.get()
        subs = [s.strip() for s in self.subs_var.get().split(",") if s.strip()] or None
        start = self.start_var.get().strip() or None
        end = self.end_var.get().strip() or None

        index = self._push_queue(f"{mode}:{url} → pending")
        self.queue_paths.append(None)

        def task():
            try:
                # Auto format suggestion
                if mode == "audio" and fmt != "mp3":
                    self.after(0, lambda: self.format_var.set("mp3"))
                if mode == "video" and fmt == "mp3":
                    self.after(0, lambda: self.format_var.set("mp4"))
                self.after(0, lambda: self._update_queue_status(index, "downloading", "blue"))
                if mode == "audio":
                    path = download_audio(url, out, audio_format=fmt, trim_start=start, trim_end=end)
                else:
                    path = download_video(url, out, resolution=(None if quality == "highest" else quality), file_format=fmt if fmt in ("mp4", "webm") else config.DEFAULT_VIDEO_FORMAT, subtitles_langs=subs, embed_subtitles=self.embed_subs_var.get(), trim_start=start, trim_end=end)
                self.queue_paths[index] = str(path)
                self.after(0, lambda: self._update_queue_status(index, "completed", "green"))
                try:
                    notify("Download complete", str(path))
                except Exception:
                    pass
            except Exception as exc:
                err_msg = str(exc)
                self.after(0, lambda: self._update_queue_status(index, "failed", "red"))
                self.after(0, lambda e=err_msg: messagebox.showerror("Error", e))
            finally:
                self.after(0, self._stop_progress)

        self._start_progress()
        threading.Thread(target=task, daemon=True).start()

    def _push_queue(self, text: str) -> int:
        idx = self.queue_box.size()
        self.queue_box.insert(tk.END, text)
        self.queue_box.itemconfig(idx, {'fg': 'gray'})
        self.queue_box.see(tk.END)
        return idx

    def _update_queue_status(self, index: int, status: str, color: str):
        text = self.queue_box.get(index)
        base = text.split("→")[0].strip() if "→" in text else text
        self.queue_box.delete(index)
        self.queue_box.insert(index, f"{base} → {status}")
        self.queue_box.itemconfig(index, {'fg': color})

    def _cancel_last(self):
        messagebox.showinfo("Cancel", "Cancellation is minimal in this version. Use CLI for full control.")

    def _paste_url(self):
        try:
            self.url_var.set(self.clipboard_get())
        except Exception:
            pass

    def _poll_clipboard(self):
        try:
            clip = self.clipboard_get()
            if "youtube.com/watch" in clip or "youtu.be/" in clip:
                if self.url_var.get() != clip:
                    self.url_var.set(clip)
        except Exception:
            pass
        self.after(3000, self._poll_clipboard)

    def _toggle_theme(self):
        new_theme = "dark" if getattr(self, "_theme", getattr(config, "DEFAULT_THEME", "light")) == "light" else "light"
        self._apply_theme(new_theme)

    def _apply_theme(self, theme: str):
        self._theme = theme
        style = ttk.Style()
        if theme == "dark":
            self.configure(bg="#2e2e2e")
            style.configure("TLabel", background="#2e2e2e", foreground="white")
            style.configure("TFrame", background="#2e2e2e")
            style.configure("TButton", background="#444", foreground="white")
        else:
            self.configure(bg="SystemButtonFace")
            style.configure("TLabel", background="SystemButtonFace", foreground="black")
            style.configure("TFrame", background="SystemButtonFace")
            style.configure("TButton", background="SystemButtonFace", foreground="black")

    def _show_preferences(self):
        win = tk.Toplevel(self)
        win.title("Preferences")
        win.geometry("360x180")
        pad = {"padx": 8, "pady": 6}
        tk.Label(win, text="Default Folder").grid(row=0, column=0, sticky="w", **pad)
        folder_var = tk.StringVar(value=self.output_dir.get())
        ttk.Entry(win, textvariable=folder_var, width=40).grid(row=0, column=1, **pad)
        ttk.Button(win, text="Browse", command=lambda: folder_var.set(filedialog.askdirectory(initialdir=folder_var.get() or self.output_dir.get() or str(config.DEFAULT_DOWNLOAD_DIR)))).grid(row=0, column=2, **pad)
        tk.Label(win, text="Default Format").grid(row=1, column=0, sticky="w", **pad)
        fmt_var = tk.StringVar(value=self.format_var.get())
        ttk.Combobox(win, textvariable=fmt_var, values=["mp4", "webm", "mp3"], state="readonly").grid(row=1, column=1, **pad)
        tk.Label(win, text="Theme").grid(row=2, column=0, sticky="w", **pad)
        theme_var = tk.StringVar(value=getattr(self, "_theme", getattr(config, "DEFAULT_THEME", "light")))
        ttk.Combobox(win, textvariable=theme_var, values=["light", "dark"], state="readonly").grid(row=2, column=1, **pad)

        def save_prefs():
            self.output_dir.set(folder_var.get())
            self.format_var.set(fmt_var.get())
            self._apply_theme(theme_var.get())
            try:
                current = {}
                current.update({
                    "default_folder": self.output_dir.get(),
                    "default_format": self.format_var.get(),
                    "theme": getattr(self, "_theme", getattr(config, "DEFAULT_THEME", "light")),
                })
                from .utils import save_settings
                save_settings(current)
            except Exception:
                pass
            win.destroy()

        ttk.Button(win, text="Save", command=save_prefs).grid(row=3, column=1, sticky="e", **pad)

    def _queue_context_menu(self, event):
        try:
            index = self.queue_box.nearest(event.y)
            self.queue_box.selection_clear(0, tk.END)
            self.queue_box.selection_set(index)
            menu = Menu(self, tearoff=0)
            menu.add_command(label="Retry", command=lambda: self._retry_download(index))
            menu.add_command(label="Remove", command=lambda: self.queue_box.delete(index))
            if index < len(self.queue_paths) and self.queue_paths[index]:
                menu.add_command(label="Open location", command=lambda: self._open_location(self.queue_paths[index]))
            menu.post(event.x_root, event.y_root)
        except Exception:
            pass

    def _retry_download(self, index: int):
        text = self.queue_box.get(index)
        if "→" in text:
            parts = text.split("→")[0].strip()
            mode, url = parts.split(":", 1)
            self.url_var.set(url.strip())
            self.mode_var.set(mode.strip())
            self._download()

    def _open_location(self, path: str | None):
        if not path:
            return
        p = Path(path)
        if not p.exists():
            return
        try:
            if os.name == "posix":
                subprocess.Popen(["xdg-open", str(p.parent)])
            elif os.name == "nt":
                os.startfile(p.parent)  # type: ignore
            else:
                webbrowser.open(str(p.parent))
        except Exception:
            pass

    def _start_progress(self):
        self.progress.config(mode="indeterminate")
        self.progress.start(50)

    def _stop_progress(self):
        self.progress.stop()

    def _toggle_theme(self):
        new_theme = "dark" if self._theme == "light" else "light"
        self._apply_theme(new_theme)

    def _apply_theme(self, theme: str):
        self._theme = theme
        self._init_styles()

    def _init_styles(self):
        style = ttk.Style()
        try:
            style.theme_use('clam')
        except Exception:
            pass
        # Color palettes
        if getattr(self, "_theme", "light") == "dark":
            bg = "#1f2937"  # slate-800
            fg = "#e5e7eb"  # gray-200
            card = "#111827"  # gray-900
            accent = "#10b981"  # emerald-500
            accent_hover = "#059669"  # emerald-600
            entry_bg = "#374151"
            border = "#374151"
        else:
            bg = "#f8fafc"  # slate-50
            fg = "#0f172a"  # slate-900
            card = "#ffffff"
            accent = "#2563eb"  # blue-600
            accent_hover = "#1d4ed8"  # blue-700
            entry_bg = "#ffffff"
            border = "#cbd5e1"  # slate-300

        self.configure(bg=bg)

        base_font = ("Segoe UI", 10)
        title_font = ("Segoe UI", 11, "bold")

        # Frames and labels
        style.configure("TFrame", background=bg)
        style.configure("TLabelframe", background=bg, foreground=fg, font=title_font)
        style.configure("TLabelframe.Label", background=bg, foreground=fg, font=title_font, padding=4)
        style.configure("TLabel", background=bg, foreground=fg, font=base_font)

        # Inputs
        style.configure("TEntry", fieldbackground=entry_bg, background=entry_bg, foreground=fg, bordercolor=border)
        style.configure("TCombobox", fieldbackground=entry_bg, background=entry_bg, foreground=fg)
        style.map("TCombobox",
                  fieldbackground=[("readonly", entry_bg)],
                  foreground=[("readonly", fg)])
        style.configure("TCheckbutton", background=bg, foreground=fg)

        # Buttons
        style.configure("Accent.TButton", background=accent, foreground="#ffffff", padding=6)
        style.map("Accent.TButton",
                  background=[("active", accent_hover)])
        style.configure("TButton", padding=6)

        # Progressbar
        style.configure("TProgressbar", troughcolor=card, background=accent)

        # Text widget background via widget config during creation

    def _show_preferences(self):
        win = tk.Toplevel(self)
        win.title("Preferences")
        win.geometry("360x160")
        pad = {"padx": 8, "pady": 6}
        tk.Label(win, text="Default Folder").grid(row=0, column=0, sticky="w", **pad)
        folder_var = tk.StringVar(value=self.output_dir.get())
        ttk.Entry(win, textvariable=folder_var, width=36).grid(row=0, column=1, **pad)
        ttk.Button(win, text="Browse", command=lambda: folder_var.set(filedialog.askdirectory(initialdir=folder_var.get() or self.output_dir.get() or str(config.DEFAULT_DOWNLOAD_DIR)))).grid(row=0, column=2, **pad)

        tk.Label(win, text="Default Format").grid(row=1, column=0, sticky="w", **pad)
        fmt_var = tk.StringVar(value=self.format_var.get())
        ttk.Combobox(win, textvariable=fmt_var, values=["mp4", "webm", "mp3"], state="readonly").grid(row=1, column=1, **pad)

        def save_prefs():
            self.output_dir.set(folder_var.get())
            self.format_var.set(fmt_var.get())
            win.destroy()

        ttk.Button(win, text="Save", command=save_prefs).grid(row=2, column=1, sticky="e", **pad)

    def _show_help(self):
        messagebox.showinfo("Help", "Paste a YouTube URL, choose options, and click Download. Right-click queue for actions.")

    def _show_about(self):
        messagebox.showinfo("About", "YouTube Downloader\nBuilt with Python and Tkinter.")


def launch_gui():
    app = App()
    app.mainloop()


