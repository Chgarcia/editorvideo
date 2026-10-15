import os
import json
import queue
import re
import shutil
import subprocess
import sys
import threading
import tkinter as tk
import webbrowser
from pathlib import Path
from tkinter import ttk, filedialog, font as tkfont, messagebox

import updater
from updater import APP_VERSION

APP_TITLE = "Muteador y desenfoque de video"
COMPRESSED_RENDER_HINT = "Renderizar y comprimir video de salida"
COMPRESSED_RENDER_DETAIL = "H.264 CRF 20 · AAC hasta 192k · MP4"
BASE_FONT_SIZE = 10
HEADING_FONT_SIZE = 15
SECTION_FONT_SIZE = 11
DEFAULT_AUDIO_BITRATE = 128_000
COMPRESSED_AUDIO_BITRATE = 192_000
BITRATE_SAFETY_FACTOR = 0.92
UPDATE_CHECK_DELAY_MS = 1500
WINGET_FFMPEG_ROOT = os.path.join(
    os.environ.get("LOCALAPPDATA", ""),
    "Microsoft",
    "WinGet",
    "Packages",
    "Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe",
)


def get_runtime_base_dir() -> str:
    if getattr(sys, "frozen", False):
        return getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def find_ffmpeg_tool(tool_name: str) -> str | None:
    runtime_base_dir = get_runtime_base_dir()
    executable_name = f"{tool_name}.exe" if os.name == "nt" else tool_name
    bundled_candidates = [
        os.path.join(runtime_base_dir, executable_name),
        os.path.join(os.path.dirname(sys.executable), executable_name) if getattr(sys, "frozen", False) else "",
    ]

    for candidate in bundled_candidates:
        if candidate and os.path.isfile(candidate):
            return candidate

    tool_path = shutil.which(tool_name)
    if tool_path:
        return tool_path

    if os.path.isdir(WINGET_FFMPEG_ROOT):
        for root, _, files in os.walk(WINGET_FFMPEG_ROOT):
            if executable_name in files:
                return os.path.join(root, executable_name)

    return None


LOCKABLE_WIDGETS = (ttk.Button, ttk.Entry, ttk.Checkbutton, ttk.Radiobutton, ttk.Combobox, ttk.Spinbox)


def ui_scale(widget) -> float:
    return max(widget.winfo_fpixels("1i") / 96.0, 1.0)


TIME_FORMAT_HELP = "Usa segundos (12.5), MM:SS (00:12.500) o HH:MM:SS (01:02:03.250)."
TIME_PATTERN = re.compile(
    r"^(?:(?:(?P<h>\d{1,3}):)?(?P<m>[0-5]?\d):(?P<s>[0-5]?\d)|(?P<plain>\d{1,6}))"
    r"(?:[.,](?P<frac>\d{1,3}))?$"
)
TIME_TYPING_PATTERN = re.compile(r"^[0-9:.,]{0,13}$")


def parse_time_to_seconds(value: str) -> float:
    text = value.strip()
    if not text:
        raise ValueError("Hay un tiempo vacío.")
    match = TIME_PATTERN.match(text)
    if not match:
        raise ValueError(f"Formato de tiempo inválido: '{text}'. {TIME_FORMAT_HELP}")

    fraction = float(f"0.{match.group('frac')}") if match.group("frac") else 0.0
    if match.group("plain") is not None:
        return int(match.group("plain")) + fraction
    hours = int(match.group("h") or 0)
    return hours * 3600 + int(match.group("m")) * 60 + int(match.group("s")) + fraction


def is_valid_partial_time(text: str) -> bool:
    if not TIME_TYPING_PATTERN.match(text):
        return False
    if text.count(":") > 2:
        return False
    decimals = text.count(".") + text.count(",")
    if decimals > 1:
        return False
    if decimals and ":" in text.split(".")[-1].split(",")[-1]:
        return False
    return True


def is_valid_time(text: str) -> bool:
    return bool(TIME_PATTERN.match(text.strip()))


def format_time(seconds: float) -> str:
    total_ms = int(round(seconds * 1000))
    hours, rest = divmod(total_ms, 3_600_000)
    minutes, rest = divmod(rest, 60_000)
    secs, ms = divmod(rest, 1000)
    prefix = f"{hours:02d}:" if hours else ""
    return f"{prefix}{minutes:02d}:{secs:02d}.{ms:03d}"


class TimeRangeRow:
    def __init__(self, master, on_remove):
        s = ui_scale(master)

        def px(value):
            return int(value * s)

        self.frame = ttk.Frame(master, padding=(px(6), px(3)))
        self.start_var = tk.StringVar()
        self.end_var = tk.StringVar()
        self.mute_var = tk.BooleanVar(value=False)
        self.blur_var = tk.BooleanVar(value=False)
        self.on_remove = on_remove

        self.index_label = ttk.Label(self.frame, text="1", width=3, style="RangeIndex.TLabel")
        self.index_label.grid(row=0, column=0, padx=(0, px(4)))

        validate_cmd = (self.frame.register(is_valid_partial_time), "%P")

        ttk.Label(self.frame, text="Inicio").grid(row=0, column=1, padx=(0, px(4)))
        self.start_entry = ttk.Entry(
            self.frame, textvariable=self.start_var, width=11, justify="center",
            validate="key", validatecommand=validate_cmd,
        )
        self.start_entry.grid(row=0, column=2, padx=(0, px(10)))

        ttk.Label(self.frame, text="Fin").grid(row=0, column=3, padx=(0, px(4)))
        self.end_entry = ttk.Entry(
            self.frame, textvariable=self.end_var, width=11, justify="center",
            validate="key", validatecommand=validate_cmd,
        )
        self.end_entry.grid(row=0, column=4, padx=(0, px(14)))

        for entry, var in ((self.start_entry, self.start_var), (self.end_entry, self.end_var)):
            var.trace_add("write", lambda *_, e=entry, v=var: self._refresh_entry_state(e, v))

        self.mute_check = ttk.Checkbutton(self.frame, text="Mutear", variable=self.mute_var)
        self.mute_check.grid(row=0, column=5, padx=(0, px(10)))

        self.blur_check = ttk.Checkbutton(self.frame, text="Desenfocar", variable=self.blur_var)
        self.blur_check.grid(row=0, column=6, padx=(0, px(10)))

        self.frame.columnconfigure(7, weight=1)

        self.remove_button = ttk.Button(
            self.frame, text="✕", command=self.remove, width=3, style="Small.TButton"
        )
        self.remove_button.grid(row=0, column=8, sticky="e")

    def grid(self, **kwargs):
        self.frame.grid(**kwargs)

    @staticmethod
    def _refresh_entry_state(entry, var):
        text = var.get().strip()
        entry.configure(style="TEntry" if not text or is_valid_time(text) else "Invalid.TEntry")

    def set_index(self, index: int):
        self.index_label.configure(text=str(index))

    def remove(self):
        self.frame.destroy()
        self.on_remove(self)

    def get_values(self):
        return self.start_var.get().strip(), self.end_var.get().strip()

    def get_effects(self):
        return self.mute_var.get(), self.blur_var.get()


def get_video_duration_seconds(video_path: str, ffprobe_path: str) -> float:
    cmd = [
        ffprobe_path,
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        video_path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        error_text = (result.stderr or result.stdout or "No fue posible leer la duración del video.").strip()
        raise RuntimeError(f"FFprobe error: {error_text}")

    value = (result.stdout or "").strip()
    if not value:
        raise RuntimeError("FFprobe no devolvió la duración del video.")

    return float(value)


def _probe_value(ffprobe_path: str, args: list[str], video_path: str) -> str:
    result = subprocess.run(
        [ffprobe_path, "-v", "error", *args, "-of", "default=noprint_wrappers=1:nokey=1", video_path],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return ""
    return (result.stdout or "").strip().splitlines()[0].strip() if result.stdout.strip() else ""


def _parse_bitrate(value: str) -> int | None:
    try:
        bitrate = int(float(value))
    except (TypeError, ValueError):
        return None
    return bitrate if bitrate > 0 else None


def get_stream_bitrates(video_path: str, ffprobe_path: str, duration_seconds: float | None) -> dict[str, int | None]:
    video_bitrate = _parse_bitrate(
        _probe_value(ffprobe_path, ["-select_streams", "v:0", "-show_entries", "stream=bit_rate"], video_path)
    )
    audio_bitrate = _parse_bitrate(
        _probe_value(ffprobe_path, ["-select_streams", "a:0", "-show_entries", "stream=bit_rate"], video_path)
    )

    if video_bitrate is None:
        container_bitrate = _parse_bitrate(
            _probe_value(ffprobe_path, ["-show_entries", "format=bit_rate"], video_path)
        )
        if container_bitrate is None and duration_seconds:
            try:
                container_bitrate = int(os.path.getsize(video_path) * 8 / duration_seconds)
            except OSError:
                container_bitrate = None
        if container_bitrate:
            video_bitrate = max(container_bitrate - (audio_bitrate or 0), 1)

    return {"video": video_bitrate, "audio": audio_bitrate}


class App(tk.Tk):
    def __init__(self, check_updates: bool = False):
        super().__init__()
        self.title(f"{APP_TITLE} v{APP_VERSION}")
        self.scale = ui_scale(self)
        self._fit_to_screen()

        self.video_path = tk.StringVar()
        self.output_path = tk.StringVar()
        self.status_var = tk.StringVar(value="Listo")
        self.progress_var = tk.DoubleVar(value=0.0)
        self.progress_text_var = tk.StringVar(value="0%")
        self.compress_render_var = tk.BooleanVar(value=False)
        self.rows = []
        self.config_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "last_ranges.json")
        self.video_duration_seconds = None
        self.ffmpeg_path = ""
        self.ffprobe_path = ""
        self.processing_thread = None
        self.progress_queue = queue.Queue()
        self.ffmpeg_process = None
        self.source_size_bytes = None
        self.is_processing = False
        self._locked_widgets = []
        self._saved_cursors = {}
        self.update_queue = queue.Queue()
        self.update_in_progress = False
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        self._configure_styles()
        self._build_menu()
        self._build_ui()
        self._load_saved_ranges()
        if check_updates:
            self.after(UPDATE_CHECK_DELAY_MS, lambda: self.check_for_updates(manual=False))

    def _build_menu(self):
        menubar = tk.Menu(self, tearoff=False)
        help_menu = tk.Menu(menubar, tearoff=False)
        help_menu.add_command(label="Manual de usuario", command=self.open_user_manual)
        help_menu.add_separator()
        help_menu.add_command(
            label="Buscar actualización",
            command=lambda: self.check_for_updates(manual=True),
        )
        menubar.add_cascade(label="Ayuda", menu=help_menu)
        self.configure(menu=menubar)

    def open_user_manual(self):
        manual_path = os.path.join(get_runtime_base_dir(), "Manual_de_usuario.html")
        if not os.path.isfile(manual_path):
            messagebox.showerror(
                "Manual de usuario",
                "No se encontró el manual de usuario junto con la aplicación.",
            )
            return

        try:
            opened = webbrowser.open(Path(manual_path).resolve().as_uri(), new=2)
        except (OSError, ValueError, webbrowser.Error) as ex:
            messagebox.showerror("Manual de usuario", f"No se pudo abrir el manual:\n{ex}")
            return
        if not opened:
            messagebox.showerror(
                "Manual de usuario",
                "No se pudo abrir el manual en el navegador predeterminado.",
            )

    def _px(self, value: float) -> int:
        return int(value * self.scale)

    def _fit_to_screen(self):
        screen_w, screen_h = self.winfo_screenwidth(), self.winfo_screenheight()
        width = min(self._px(900), int(screen_w * 0.92))
        height = min(self._px(640), int(screen_h * 0.85))
        x = max((screen_w - width) // 2, 0)
        y = max((screen_h - height) // 3, 0)
        self.geometry(f"{width}x{height}+{x}+{y}")
        self.minsize(min(self._px(720), width), min(self._px(500), height))

    def _configure_styles(self):
        for font_name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont"):
            tkfont.nametofont(font_name).configure(size=BASE_FONT_SIZE)

        style = ttk.Style(self)
        if "vista" in style.theme_names():
            style.theme_use("vista")

        px = self._px
        family = "Segoe UI" if sys.platform == "win32" else tkfont.nametofont("TkDefaultFont").actual("family")
        base = (family, BASE_FONT_SIZE)
        style.configure("TLabel", font=base)
        style.configure("TButton", font=base, padding=(px(8), px(3)))
        style.configure("Small.TButton", font=base, padding=(px(2), px(1)))
        style.configure("TCheckbutton", font=base)
        style.configure("TEntry", padding=px(2))
        style.configure("Invalid.TEntry", padding=px(2), foreground="#c62828", fieldbackground="#fdecea")
        style.configure("TLabelframe.Label", font=(family, SECTION_FONT_SIZE, "bold"))
        style.configure("Heading.TLabel", font=(family, HEADING_FONT_SIZE, "bold"))
        style.configure("Subtitle.TLabel", font=base, foreground="#555555")
        style.configure("Hint.TLabel", font=(family, BASE_FONT_SIZE - 1), foreground="#666666")
        style.configure("Status.TLabel", font=base)
        style.configure("RangeIndex.TLabel", font=(family, BASE_FONT_SIZE, "bold"), foreground="#0a66c2")
        style.configure("Primary.TButton", font=(family, SECTION_FONT_SIZE, "bold"), padding=(px(18), px(6)))
        style.configure("Option.TCheckbutton", font=(family, BASE_FONT_SIZE, "bold"))
        style.configure("Big.Horizontal.TProgressbar", thickness=px(16))

    def _build_ui(self):
        px = self._px
        main = ttk.Frame(self, padding=(px(14), px(10)))
        main.pack(fill="both", expand=True)

        header = ttk.Frame(main)
        header.pack(fill="x", pady=(0, px(8)))
        ttk.Label(header, text="Muteo y desenfoque por rangos", style="Heading.TLabel").pack(side="left")
        ttk.Label(
            header,
            text="Silencia, desenfoca o solo comprime tu video",
            style="Subtitle.TLabel",
        ).pack(side="left", padx=(px(12), 0), pady=(px(4), 0))
        ttk.Button(
            header, text="Buscar actualización", style="Small.TButton",
            command=lambda: self.check_for_updates(manual=True),
        ).pack(side="right", pady=(px(4), 0))
        ttk.Button(
            header, text="Manual de usuario", style="Small.TButton",
            command=self.open_user_manual,
        ).pack(side="right", padx=(0, px(8)), pady=(px(4), 0))
        ttk.Label(header, text=f"v{APP_VERSION}", style="Hint.TLabel").pack(
            side="right", padx=(0, px(8)), pady=(px(4), 0)
        )

        file_frame = ttk.LabelFrame(main, text="Archivos", padding=(px(10), px(6)))
        file_frame.pack(fill="x", pady=(0, px(8)))

        ttk.Label(file_frame, text="Entrada").grid(row=0, column=0, sticky="w", padx=(0, px(8)), pady=px(2))
        ttk.Entry(file_frame, textvariable=self.video_path).grid(row=0, column=1, sticky="ew", pady=px(2))
        ttk.Button(file_frame, text="Buscar...", command=self.select_video).grid(
            row=0, column=2, sticky="ew", padx=(px(8), 0), pady=px(2)
        )

        ttk.Label(file_frame, text="Salida").grid(row=1, column=0, sticky="w", padx=(0, px(8)), pady=px(2))
        ttk.Entry(file_frame, textvariable=self.output_path).grid(row=1, column=1, sticky="ew", pady=px(2))
        ttk.Button(file_frame, text="Guardar como...", command=self.select_output).grid(
            row=1, column=2, sticky="ew", padx=(px(8), 0), pady=px(2)
        )

        file_frame.columnconfigure(1, weight=1)

        options_frame = ttk.Frame(main)
        options_frame.pack(fill="x", pady=(0, px(8)))
        ttk.Checkbutton(
            options_frame,
            text=COMPRESSED_RENDER_HINT,
            variable=self.compress_render_var,
            command=self._on_compressed_render_toggle,
            style="Option.TCheckbutton",
        ).pack(side="left")
        ttk.Label(
            options_frame,
            text=f"{COMPRESSED_RENDER_DETAIL}  ·  Desactivado copia sin pérdida lo que no se edita",
            style="Hint.TLabel",
        ).pack(side="left", padx=(px(10), 0))

        ranges_frame = ttk.LabelFrame(main, text="Rangos y efectos (opcional)", padding=(px(10), px(6)))

        action_frame = ttk.Frame(main)
        action_frame.pack(side="bottom", fill="x")

        self.process_button = ttk.Button(
            action_frame, text="Procesar video", command=self.process_video, style="Primary.TButton"
        )
        self.process_button.pack(side="left")
        ttk.Button(action_frame, text="Salir", command=self._on_close).pack(side="right")

        log_frame = ttk.Frame(main)
        log_frame.pack(side="bottom", fill="x", pady=(0, px(8)))
        progress_frame = ttk.Frame(log_frame)
        progress_frame.pack(fill="x")
        self.progress_bar = ttk.Progressbar(
            progress_frame,
            variable=self.progress_var,
            maximum=100,
            style="Big.Horizontal.TProgressbar",
        )
        self.progress_bar.pack(side="left", fill="x", expand=True)
        ttk.Label(progress_frame, textvariable=self.progress_text_var, width=5, anchor="e",
                  style="Status.TLabel").pack(side="left", padx=(px(8), 0))
        self.status_label = ttk.Label(log_frame, textvariable=self.status_var, style="Status.TLabel", justify="left")
        self.status_label.pack(fill="x", anchor="w", pady=(px(4), 0))
        log_frame.bind(
            "<Configure>",
            lambda e: self.status_label.configure(wraplength=max(e.width - px(10), 100)),
        )

        ranges_frame.pack(fill="both", expand=True, pady=(0, px(8)))

        toolbar = ttk.Frame(ranges_frame)
        toolbar.pack(fill="x", pady=(0, px(4)))
        ttk.Button(toolbar, text="+ Agregar", command=self.add_row).pack(side="left")
        ttk.Button(toolbar, text="Limpiar", command=self.clear_rows).pack(side="left", padx=px(6))
        ttk.Button(toolbar, text="Guardar", command=self.save_ranges).pack(side="left")
        ttk.Label(
            toolbar,
            text="Formatos: 12.5 · 00:12.500 · 01:02:03.250 · Vacío = video completo sin efectos",
            style="Hint.TLabel",
        ).pack(side="right")

        list_container = ttk.Frame(ranges_frame)
        list_container.pack(fill="both", expand=True)

        canvas_background = ttk.Style(self).lookup("TFrame", "background") or "#f0f0f0"
        self.canvas = tk.Canvas(
            list_container, highlightthickness=0, height=px(90), background=canvas_background
        )
        scrollbar = ttk.Scrollbar(list_container, orient="vertical", command=self.canvas.yview)
        self.scrollable_frame = ttk.Frame(self.canvas)

        self.scrollable_frame.bind(
            "<Configure>",
            lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        )

        self.canvas_window = self.canvas.create_window((0, 0), window=self.scrollable_frame, anchor="nw")
        self.canvas.bind(
            "<Configure>",
            lambda e: self.canvas.itemconfigure(self.canvas_window, width=e.width),
        )
        self.canvas.configure(yscrollcommand=scrollbar.set)

        self.canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        self.bind_all("<MouseWheel>", self._on_mouse_wheel)

        if not self.rows:
            self.add_row()

    def _on_mouse_wheel(self, event):
        if str(self.canvas.winfo_containing(event.x_root, event.y_root)).startswith(str(self.canvas)):
            self.canvas.yview_scroll(-int(event.delta / 120), "units")

    def add_row(self, start="", end="", mute=False, blur=False):
        row = TimeRangeRow(self.scrollable_frame, self._remove_row)
        row.start_var.set(start)
        row.end_var.set(end)
        row.mute_var.set(mute)
        row.blur_var.set(blur)
        row.grid(row=len(self.rows), column=0, sticky="ew", pady=1)
        self.rows.append(row)
        self.scrollable_frame.columnconfigure(0, weight=1)
        self._renumber_rows()

    def _remove_row(self, row):
        if row in self.rows:
            self.rows.remove(row)
            self._repack_rows()
        if not self.rows:
            self.add_row()

    def _repack_rows(self):
        for idx, row in enumerate(self.rows):
            row.frame.grid_forget()
            row.grid(row=idx, column=0, sticky="ew", pady=1)
        self._renumber_rows()

    def _renumber_rows(self):
        for idx, row in enumerate(self.rows, start=1):
            row.set_index(idx)

    def clear_rows(self):
        for row in self.rows[:]:
            row.frame.destroy()
        self.rows.clear()
        self.add_row()

    def select_video(self):
        path = filedialog.askopenfilename(
            title="Selecciona un video",
            filetypes=[
                ("Videos", "*.mp4 *.mov *.mkv *.avi *.wmv *.m4v"),
                ("Todos los archivos", "*.*")
            ]
        )
        if path:
            self.video_path.set(path)
            self._show_video_duration(path)
            if not self.output_path.get().strip():
                base, ext = os.path.splitext(path)
                if self.compress_render_var.get():
                    self.output_path.set(f"{base}_comprimido.mp4")
                else:
                    self.output_path.set(f"{base}_editado{ext or '.mp4'}")

    def _show_video_duration(self, path: str):
        try:
            ffprobe_path = find_ffmpeg_tool("ffprobe")
            if not ffprobe_path:
                return
            duration = get_video_duration_seconds(path, ffprobe_path)
        except Exception:
            return
        self.video_duration_seconds = duration
        message = f"Duración del video: {format_time(duration)}. Los rangos deben estar dentro de este tiempo."
        try:
            self._validate_ranges_within_duration(self._get_ranges(validate=False), duration)
        except ValueError as ex:
            message = f"{message}\nAviso: {ex}"
        self.set_status(message)

    def select_output(self):
        path = filedialog.asksaveasfilename(
            title="Guardar video procesado",
            defaultextension=".mp4",
            filetypes=[
                ("MP4", "*.mp4"),
                ("MKV", "*.mkv"),
                ("MOV", "*.mov"),
                ("Todos los archivos", "*.*")
            ]
        )
        if path:
            self.output_path.set(self._apply_compressed_extension(path))

    def _apply_compressed_extension(self, path: str) -> str:
        if not path or not self.compress_render_var.get():
            return path
        base, ext = os.path.splitext(path)
        return path if ext.lower() == ".mp4" else f"{base}.mp4"

    def _on_compressed_render_toggle(self):
        current = self.output_path.get().strip()
        updated = self._apply_compressed_extension(current)
        if updated != current:
            self.output_path.set(updated)

    def set_status(self, text):
        self.status_var.set(text)
        self.update_idletasks()

    def _set_progress(self, progress_value: float, text: str | None = None):
        bounded_value = max(0.0, min(100.0, progress_value))
        self.progress_var.set(bounded_value)
        self.progress_text_var.set(text or f"{bounded_value:.0f}%")
        self.update_idletasks()

    def _iter_widgets(self, parent=None):
        for child in (parent or self).winfo_children():
            yield child
            yield from self._iter_widgets(child)

    def _set_processing_state(self, is_processing: bool):
        if is_processing and not self.is_processing:
            self.is_processing = True
            self._locked_widgets = []
            self._saved_cursors = {self: self.cget("cursor")}
            for widget in self._iter_widgets():
                if isinstance(widget, LOCKABLE_WIDGETS) and not widget.instate(["disabled"]):
                    widget.state(["disabled"])
                    self._locked_widgets.append(widget)
                try:
                    self._saved_cursors[widget] = widget.cget("cursor")
                    widget.configure(cursor="watch")
                except tk.TclError:
                    pass
            self.configure(cursor="watch")
            self.focus_set()
        elif not is_processing and self.is_processing:
            self.is_processing = False
            for widget in self._locked_widgets:
                if widget.winfo_exists():
                    widget.state(["!disabled"])
            for widget, cursor in self._saved_cursors.items():
                if widget.winfo_exists():
                    widget.configure(cursor=cursor)
            self._locked_widgets = []
            self._saved_cursors = {}
        self.update_idletasks()

    def _on_close(self):
        if getattr(self, "_downloading_update", False):
            return
        if self.is_processing:
            if not messagebox.askyesno(
                "Procesando",
                "Hay un video en proceso. ¿Cancelar el procesamiento y salir?",
                icon="warning",
            ):
                return
            process = self.ffmpeg_process
            if process and process.poll() is None:
                process.terminate()
        self.destroy()

    def check_for_updates(self, manual: bool = False):
        if self.update_in_progress:
            if manual:
                messagebox.showinfo("Actualización", "Ya se está revisando o descargando una actualización.")
            return
        self.update_in_progress = True

        def worker():
            try:
                self.update_queue.put(("checked", (manual, updater.check_for_update())))
            except Exception as ex:
                self.update_queue.put(("check_error", (manual, str(ex))))

        threading.Thread(target=worker, daemon=True).start()
        self.after(200, self._poll_update_queue)

    def _poll_update_queue(self):
        try:
            while True:
                event, payload = self.update_queue.get_nowait()
                if event == "checked":
                    self._handle_update_result(*payload)
                elif event == "check_error":
                    manual, message = payload
                    self.update_in_progress = False
                    if manual:
                        messagebox.showerror("Actualización", message)
                elif event == "download_progress":
                    self._update_download_progress(*payload)
                elif event == "download_done":
                    self._finish_update_download(None)
                    return
                elif event == "download_error":
                    self._finish_update_download(payload)
                    return
        except queue.Empty:
            pass
        if self.update_in_progress:
            self.after(200, self._poll_update_queue)

    def _handle_update_result(self, manual: bool, info: dict | None):
        if info is None:
            self.update_in_progress = False
            if manual:
                messagebox.showinfo("Actualización", f"Ya tienes la versión más reciente (v{APP_VERSION}).")
            return
        if self.is_processing:
            self.after(3000, lambda: self._handle_update_result(manual, info))
            return

        notes = f"\n\nNovedades:\n{info['notes']}" if info.get("notes") else ""
        header = f"Hay una nueva versión disponible: v{info['version']} (tienes v{APP_VERSION})."
        if updater.can_self_update():
            if messagebox.askyesno(
                "Actualización disponible",
                f"{header}{notes}\n\n¿Descargar e instalar ahora? La aplicación se reiniciará.",
            ):
                self._start_update_download(info)
                return
        elif messagebox.askyesno(
            "Actualización disponible",
            f"{header}{notes}\n\n¿Abrir la descarga en el navegador?",
        ):
            webbrowser.open(info["url"])
        self.update_in_progress = False

    def _start_update_download(self, info: dict):
        px = self._px
        dialog = tk.Toplevel(self)
        dialog.title("Actualizando")
        dialog.transient(self)
        dialog.resizable(False, False)
        dialog.protocol("WM_DELETE_WINDOW", lambda: None)
        frame = ttk.Frame(dialog, padding=px(14))
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text=f"Descargando la versión v{info['version']}...").pack(anchor="w")
        self.download_var = tk.DoubleVar(value=0.0)
        ttk.Progressbar(frame, variable=self.download_var, maximum=100, length=px(320)).pack(
            fill="x", pady=(px(8), px(4))
        )
        self.download_text_var = tk.StringVar(value="0%")
        ttk.Label(frame, textvariable=self.download_text_var, style="Hint.TLabel").pack(anchor="w")
        dialog.update_idletasks()
        dialog.geometry(
            f"+{self.winfo_rootx() + (self.winfo_width() - dialog.winfo_width()) // 2}"
            f"+{self.winfo_rooty() + (self.winfo_height() - dialog.winfo_height()) // 3}"
        )
        dialog.grab_set()
        self.download_dialog = dialog
        self._downloading_update = True

        def progress(received, total):
            self.update_queue.put(("download_progress", (received, total)))

        def worker():
            try:
                updater.stage_windows_update(info, progress)
                self.update_queue.put(("download_done", None))
            except Exception as ex:
                self.update_queue.put(("download_error", str(ex)))

        threading.Thread(target=worker, daemon=True).start()

    def _update_download_progress(self, received: int, total: int):
        if total:
            percent = received * 100.0 / total
            self.download_var.set(percent)
            self.download_text_var.set(
                f"{percent:.0f}%  ·  {self._format_size(received)} de {self._format_size(total)}"
            )
        else:
            self.download_text_var.set(self._format_size(received))

    def _finish_update_download(self, error: str | None):
        self._downloading_update = False
        self.update_in_progress = False
        dialog = getattr(self, "download_dialog", None)
        if dialog is not None and dialog.winfo_exists():
            dialog.grab_release()
            dialog.destroy()
        if error:
            messagebox.showerror("Actualización", f"No se pudo actualizar.\n\n{error}")
            return
        self.destroy()

    @staticmethod
    def _format_size(size_bytes: int) -> str:
        megabytes = size_bytes / (1024 * 1024)
        if megabytes >= 1024:
            return f"{megabytes / 1024:.2f} GB"
        return f"{megabytes:.1f} MB"

    def _build_size_summary(self, output_path: str) -> str:
        try:
            final_size = os.path.getsize(output_path)
        except OSError:
            return ""

        if not self.source_size_bytes:
            return f"Tamaño final: {self._format_size(final_size)}"

        ratio = final_size / self.source_size_bytes
        if ratio < 0.995:
            change = f"{(1 - ratio) * 100:.0f}% más ligero"
        elif ratio > 1.005:
            change = f"{(ratio - 1) * 100:.0f}% más pesado"
        else:
            change = "prácticamente igual"

        return (
            f"Tamaño: {self._format_size(self.source_size_bytes)} → "
            f"{self._format_size(final_size)} ({change})"
        )

    def _parse_ffmpeg_progress_seconds(self, progress_data: dict[str, str]) -> float | None:
        for key in ("out_time_us", "out_time_ms"):
            raw_value = progress_data.get(key)
            if raw_value:
                try:
                    return float(raw_value) / 1_000_000.0
                except ValueError:
                    pass

        out_time = progress_data.get("out_time")
        if not out_time or out_time == "N/A":
            return None

        try:
            hours_text, minutes_text, seconds_text = out_time.split(":")
            return int(hours_text) * 3600 + int(minutes_text) * 60 + float(seconds_text)
        except ValueError:
            return None

    def _poll_progress_queue(self):
        try:
            while True:
                event_type, payload = self.progress_queue.get_nowait()

                if event_type == "progress":
                    progress_value = payload
                    self._set_progress(progress_value, f"{progress_value:.0f}%")
                    continue

                self.ffmpeg_process = None
                self._set_processing_state(False)

                if event_type == "success":
                    output_path = payload
                    self._set_progress(100.0, "100%")
                    summary = self._build_size_summary(output_path)
                    self.set_status(f"Completado: {output_path}\n{summary}")
                    messagebox.showinfo("Éxito", f"Video generado:\n{output_path}\n\n{summary}")
                elif event_type == "error":
                    self._set_progress(0.0, "0%")
                    self.set_status(f"Error: {payload}")
                    messagebox.showerror("Error", payload)
                return
        except queue.Empty:
            pass

        if self.processing_thread and self.processing_thread.is_alive():
            self.after(100, self._poll_progress_queue)

    def _run_ffmpeg_process(self, cmd: list[str], output_path: str):
        progress_data = {}
        error_lines = []

        try:
            self.ffmpeg_process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
            )

            assert self.ffmpeg_process.stdout is not None

            for raw_line in self.ffmpeg_process.stdout:
                line = raw_line.strip()
                if not line:
                    continue

                if "=" in line:
                    key, value = line.split("=", 1)
                    progress_data[key] = value

                    if key == "progress":
                        seconds = self._parse_ffmpeg_progress_seconds(progress_data)
                        if self.video_duration_seconds and seconds is not None:
                            progress_value = (seconds / self.video_duration_seconds) * 100.0
                            self.progress_queue.put(("progress", min(progress_value, 99.0)))
                        progress_data = {}
                    continue

                error_lines.append(line)

            return_code = self.ffmpeg_process.wait()

            if return_code != 0:
                error_text = "\n".join(error_lines[-12:]).strip() or "Error FFmpeg"
                self.progress_queue.put(("error", error_text))
                return

            self.progress_queue.put(("success", output_path))
        except Exception as ex:
            self.progress_queue.put(("error", str(ex)))

    def save_ranges(self):
        try:
            ranges = self._get_ranges(validate=False)
            with open(self.config_file, "w", encoding="utf-8") as f:
                json.dump(ranges, f, ensure_ascii=False, indent=2)
            messagebox.showinfo("Guardar", "Rangos guardados correctamente.")
        except Exception as ex:
            messagebox.showerror("Error", str(ex))

    def _load_saved_ranges(self):
        if not os.path.exists(self.config_file):
            return
        try:
            with open(self.config_file, "r", encoding="utf-8") as f:
                ranges = json.load(f)
            if isinstance(ranges, list) and ranges:
                self.clear_rows()
                for idx, item in enumerate(ranges):
                    start = str(item.get("start", ""))
                    end = str(item.get("end", ""))
                    mute = bool(item.get("mute", False))
                    blur = bool(item.get("blur", False))
                    if idx == 0:
                        self.rows[0].start_var.set(start)
                        self.rows[0].end_var.set(end)
                        self.rows[0].mute_var.set(mute)
                        self.rows[0].blur_var.set(blur)
                    else:
                        self.add_row(start, end, mute, blur)
        except Exception:
            pass

    def _parse_time_to_seconds(self, value: str) -> float:
        return parse_time_to_seconds(value)

    def _seconds_to_ffmpeg_time(self, seconds: float) -> str:
        if seconds < 0:
            raise ValueError("Los tiempos no pueden ser negativos.")
        return f"{seconds:.3f}"

    def _get_ranges(self, validate=True):
        ranges = []
        for idx, row in enumerate(self.rows, start=1):
            start_text, end_text = row.get_values()
            mute, blur = row.get_effects()
            if not start_text and not end_text:
                continue
            if not start_text or not end_text:
                raise ValueError(f"El rango {idx} debe tener inicio y fin.")

            try:
                start_sec = self._parse_time_to_seconds(start_text)
            except ValueError:
                raise ValueError(f"El rango {idx} tiene un inicio inválido: '{start_text}'.\n{TIME_FORMAT_HELP}")
            try:
                end_sec = self._parse_time_to_seconds(end_text)
            except ValueError:
                raise ValueError(f"El rango {idx} tiene un fin inválido: '{end_text}'.\n{TIME_FORMAT_HELP}")

            if validate and start_sec < 0:
                raise ValueError(f"El rango {idx} tiene un inicio negativo.")

            if validate and end_sec <= start_sec:
                raise ValueError(f"El rango {idx} es inválido: fin debe ser mayor que inicio.")

            ranges.append({
                "index": idx,
                "start": self._seconds_to_ffmpeg_time(start_sec),
                "end": self._seconds_to_ffmpeg_time(end_sec),
                "start_sec": start_sec,
                "end_sec": end_sec,
                "mute": mute,
                "blur": blur,
            })

        ranges.sort(key=lambda x: x["start_sec"])

        if validate:
            for i in range(1, len(ranges)):
                prev = ranges[i - 1]
                curr = ranges[i]
                if curr["start_sec"] < prev["end_sec"]:
                    raise ValueError(
                        f"Los rangos {prev['index']} ({format_time(prev['start_sec'])} - {format_time(prev['end_sec'])}) "
                        f"y {curr['index']} ({format_time(curr['start_sec'])} - {format_time(curr['end_sec'])}) "
                        "se traslapan. Ajusta los tiempos para que no se superpongan."
                    )

        return ranges

    @staticmethod
    def _validate_ranges_within_duration(ranges, duration_seconds: float):
        limit = format_time(duration_seconds)
        for item in ranges:
            if item["start_sec"] >= duration_seconds:
                raise ValueError(
                    f"El rango {item['index']} inicia en {format_time(item['start_sec'])}, "
                    f"fuera del video (duración: {limit})."
                )
            if item["end_sec"] > duration_seconds:
                raise ValueError(
                    f"El rango {item['index']} termina en {format_time(item['end_sec'])}, "
                    f"después del final del video (duración: {limit})."
                )

    def _ensure_ffmpeg(self):
        ffmpeg_path = find_ffmpeg_tool("ffmpeg")
        if ffmpeg_path is None:
            raise RuntimeError(
                "FFmpeg no está instalado o no está en el PATH de Windows.\n\n"
                "Instálalo o reinicia la terminal para que el PATH se actualice."
            )
        ffprobe_path = find_ffmpeg_tool("ffprobe")
        if ffprobe_path is None:
            raise RuntimeError(
                "FFprobe no está instalado o no está en el PATH de Windows.\n\n"
                "Normalmente viene junto con FFmpeg."
            )

        self.ffmpeg_path = ffmpeg_path
        self.ffprobe_path = ffprobe_path

    def _build_audio_filter(self, ranges):
        return ",".join(
            f"volume=0:enable='between(t,{item['start']},{item['end']})'"
            for item in ranges if item["mute"]
        )

    def _build_video_filter(self, ranges):
        return ",".join(
            f"gblur=sigma=50:enable='between(t,{item['start']},{item['end']})'"
            for item in ranges if item["blur"]
        )

    def _build_codec_args(self, audio_filter: str, video_filter: str, bitrates: dict | None = None) -> list[str]:
        args = []
        bitrates = bitrates or {}
        source_video_bitrate = bitrates.get("video")
        source_audio_bitrate = bitrates.get("audio")

        if video_filter:
            args.extend(["-vf", video_filter])
        if audio_filter:
            args.extend(["-af", audio_filter])

        compress = self.compress_render_var.get()
        reencodes_video = bool(video_filter) or compress
        reencodes_audio = bool(audio_filter) or compress

        if reencodes_video:
            args.extend([
                "-c:v", "libx264",
                "-preset", "medium",
                "-crf", "20" if compress else "23",
            ])
            if source_video_bitrate:
                capped_video_bitrate = max(int(source_video_bitrate * BITRATE_SAFETY_FACTOR), 1)
                args.extend([
                    "-maxrate", str(capped_video_bitrate),
                    "-bufsize", str(capped_video_bitrate * 2),
                ])
            if compress:
                args.extend(["-pix_fmt", "yuv420p", "-profile:v", "high"])
        else:
            args.extend(["-c:v", "copy"])

        if reencodes_audio:
            args.extend(["-c:a", "aac"])
            target_audio_bitrate = COMPRESSED_AUDIO_BITRATE if compress else DEFAULT_AUDIO_BITRATE
            if source_audio_bitrate:
                source_audio_limit = int(source_audio_bitrate * BITRATE_SAFETY_FACTOR)
                target_audio_bitrate = min(target_audio_bitrate, max(source_audio_limit, 1))
            args.extend(["-b:a", str(target_audio_bitrate)])
            if compress:
                args.extend(["-ar", "48000"])
        else:
            args.extend(["-c:a", "copy"])

        if compress:
            args.extend(["-movflags", "+faststart"])

        return args

    def process_video(self):
        try:
            self._ensure_ffmpeg()
            input_path = self.video_path.get().strip()
            output_path = self._apply_compressed_extension(self.output_path.get().strip())
            ranges = self._get_ranges(validate=True)

            if not input_path:
                raise ValueError("Selecciona un video de entrada.")
            if not os.path.isfile(input_path):
                raise ValueError("El video de entrada no existe.")
            if not output_path:
                raise ValueError("Indica la ruta del video de salida.")
            if os.path.abspath(input_path) == os.path.abspath(output_path):
                raise ValueError("El video de salida debe ser distinto al de entrada.")

            self.output_path.set(output_path)

            self.video_duration_seconds = get_video_duration_seconds(input_path, self.ffprobe_path)
            self._validate_ranges_within_duration(ranges, self.video_duration_seconds)

            os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)

            cmd = [
                self.ffmpeg_path,
                "-y",
                "-hide_banner",
                "-nostats",
                "-progress", "pipe:1",
                "-i", input_path,
            ]
            audio_filter = self._build_audio_filter(ranges)
            video_filter = self._build_video_filter(ranges)
            bitrates = get_stream_bitrates(input_path, self.ffprobe_path, self.video_duration_seconds)
            cmd.extend(self._build_codec_args(audio_filter, video_filter, bitrates))
            cmd.append(output_path)

            self.source_size_bytes = os.path.getsize(input_path)

            applied = []
            if audio_filter:
                applied.append("muteo")
            if video_filter:
                applied.append("desenfoque")
            if self.compress_render_var.get():
                applied.append("compresión")

            self.set_status(
                f"Procesando ({' + '.join(applied) or 'copia sin cambios'})... "
                f"duración: {self.video_duration_seconds:.3f}s"
            )
            self._set_progress(0.0, "0%")
            self._set_processing_state(True)
            self.processing_thread = threading.Thread(
                target=self._run_ffmpeg_process,
                args=(cmd, output_path),
                daemon=True,
            )
            self.processing_thread.start()
            self.after(100, self._poll_progress_queue)

        except Exception as ex:
            self._set_processing_state(False)
            self._set_progress(0.0, "0%")
            self.set_status(f"Error: {ex}")
            messagebox.showerror("Error", str(ex))


def enable_high_dpi():
    if sys.platform != "win32":
        return
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass


if __name__ == "__main__":
    enable_high_dpi()
    app = App(check_updates=True)
    app.mainloop()
