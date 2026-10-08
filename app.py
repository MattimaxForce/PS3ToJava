#!/usr/bin/env python3
"""PS3ToPC desktop application.

A dependency-light graphical frontend for the PS3 Legacy Console Edition to
Minecraft Java Edition converter.  The conversion engine in ``core/`` is kept
as the known-good implementation; this file only adds the user interface and
process orchestration.
"""
from __future__ import annotations

import os
import queue
import re
import subprocess
import sys
import threading
from pathlib import Path

APP_NAME = "PS3ToPC"
APP_VERSION = "1.0.0"
MIN_PYTHON = (3, 10)

ROOT = Path(__file__).resolve().parent
CORE = ROOT / "core"
CONVERTER = CORE / "ps3_converter.py"
WORKER = CORE / "convert_region_worker.py"


def _run_worker(gamedata: str, region_name: str, output_dir: str) -> int:
    """Run one region worker when the packaged executable is invoked internally."""
    from core.ps3_converter import run_region_worker
    run_region_worker(gamedata, region_name, output_dir)
    return 0


def _run_convert(gamedata: str, output_dir: str) -> int:
    """Run the complete converter for the packaged executable."""
    from core.ps3_converter import convert_gamedata
    convert_gamedata(gamedata, output_dir)
    return 0


def _dispatch_internal() -> bool:
    """Handle internal subprocess modes used by source and packaged builds."""
    if len(sys.argv) < 2:
        return False

    mode = sys.argv[1]
    if mode == "--convert" and len(sys.argv) == 4:
        raise SystemExit(_run_convert(sys.argv[2], sys.argv[3]))
    if mode == "--worker" and len(sys.argv) == 5:
        raise SystemExit(_run_worker(sys.argv[2], sys.argv[3], sys.argv[4]))
    if mode == "--check":
        print(f"{APP_NAME} {APP_VERSION}")
        print(f"Python: {sys.version.split()[0]}")
        print(f"Core: {CORE}")
        print(f"Converter: {'OK' if CONVERTER.is_file() else 'MISSING'}")
        print(f"Worker: {'OK' if WORKER.is_file() else 'MISSING'}")
        return True
    return False


if _dispatch_internal():
    raise SystemExit(0)


def safe_name(value: str) -> str:
    value = re.sub(r"[^A-Za-z0-9._ -]+", "_", value).strip(" .")
    return value or "ConvertedWorld"


def likely_gamedata(path: Path) -> bool:
    """Quickly reject non-GAMEDATA files without reading the whole save."""
    try:
        if not path.is_file() or path.stat().st_size < 8:
            return False
        with path.open("rb") as f:
            head = f.read(8)
        index_offset = int.from_bytes(head[0:4], "big")
        file_count = int.from_bytes(head[4:8], "big")
        size = path.stat().st_size
        return 0 < file_count < 100000 and 8 <= index_offset < size and index_offset + 144 <= size
    except OSError:
        return False


def find_gamedata(folder: Path) -> list[Path]:
    """Find GAMEDATA automatically anywhere below the selected folder."""
    folder = folder.resolve()
    if folder.is_file() and folder.name.lower() == "gamedata" and likely_gamedata(folder):
        return [folder]

    candidates: list[Path] = []
    try:
        for root, dirs, files in os.walk(folder):
            dirs[:] = [d for d in dirs if d not in {".git", ".hg", ".svn", "__pycache__"}]
            for name in files:
                if name.lower() == "gamedata":
                    candidate = Path(root) / name
                    if likely_gamedata(candidate):
                        candidates.append(candidate.resolve())
    except OSError:
        pass

    return sorted(set(candidates), key=lambda p: (len(p.parts), str(p).lower()))


def source_display_name(folder: Path, gamedata: Path) -> str:
    if folder.name.lower() == "gamedata":
        return folder.parent.name or "PS3 World"
    if folder.name.lower() == "savedata":
        return gamedata.parent.name or folder.name
    return folder.name or gamedata.parent.name or "PS3 World"


class RoundedPanel:
    """Simple rounded card built from a Canvas + child Frame, no dependencies."""
    def __init__(self, parent, width=800, height=120, radius=18, bg="#17191f", border="#2a2e38"):
        import tkinter as tk
        self.canvas = tk.Canvas(parent, width=width, height=height, bg=parent.cget("bg"), highlightthickness=0)
        self.canvas.pack_propagate(False)
        self.radius = radius
        self.bg = bg
        self.border = border
        self.frame = tk.Frame(self.canvas, bg=bg)
        self.canvas.create_round_rect = getattr(self.canvas, "create_round_rect", None)
        self._draw()
        self.window = self.canvas.create_window((10, 10), window=self.frame, anchor="nw", width=width-20, height=height-20)

    def _round_points(self, x1, y1, x2, y2, r):
        return [x1+r,y1, x2-r,y1, x2,y1+r, x2,y2-r, x2-r,y2, x1+r,y2, x1,y2-r, x1,y1+r]

    def _draw(self):
        self.canvas.delete("card")
        w = int(float(self.canvas.cget("width")))
        h = int(float(self.canvas.cget("height")))
        r = self.radius
        # 8-segment polygon with smooth splines approximates a rounded card.
        pts = self._round_points(1, 1, w-1, h-1, r)
        self.canvas.create_polygon(*pts, smooth=True, splinesteps=24, fill=self.border, outline=self.border, tags="card")
        pts2 = self._round_points(2, 2, w-2, h-2, max(2, r-1))
        self.canvas.create_polygon(*pts2, smooth=True, splinesteps=24, fill=self.bg, outline=self.bg, tags="card")
        self.canvas.tag_lower("card")


class App:
    BG = "#0f1115"
    CARD = "#17191f"
    BORDER = "#2a2e38"
    TEXT = "#eef0f5"
    MUTED = "#9ba2b0"
    ACCENT = "#e0434c"
    ACCENT_HOVER = "#f05b63"
    SUCCESS = "#52c48b"
    WARNING = "#e4ae52"

    def __init__(self):
        import tkinter as tk
        from tkinter import ttk
        self.tk = tk
        self.ttk = ttk
        self.root = tk.Tk()
        self.root.title(f"{APP_NAME} — PS3 → Java")
        self.root.geometry("980x710")
        self.root.minsize(980, 650)
        self.root.configure(bg=self.BG)
        try:
            self.root.iconname(APP_NAME)
        except Exception:
            pass

        self.source_folder: Path | None = None
        self.gamedata_path: Path | None = None
        self.output_parent: Path | None = None
        self.running = False
        self.log_queue: queue.Queue = queue.Queue()
        self.process: subprocess.Popen[str] | None = None
        self.region_total: int | None = None
        self.last_output: Path | None = None

        self._configure_style()
        self._build()
        self.root.after(120, self._poll_log)
        self.root.protocol("WM_DELETE_WINDOW", self._close)

    def _configure_style(self):
        style = self.ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except Exception:
            pass
        style.configure("Dark.Horizontal.TProgressbar", troughcolor="#23262f", background=self.ACCENT,
                        bordercolor="#23262f", lightcolor=self.ACCENT, darkcolor=self.ACCENT)

    def _build(self):
        import tkinter as tk

        outer = tk.Frame(self.root, bg=self.BG)
        outer.pack(fill="both", expand=True, padx=34, pady=28)

        header = tk.Frame(outer, bg=self.BG)
        header.pack(fill="x", pady=(0, 22))
        tk.Label(header, text="M", fg=self.ACCENT, bg=self.BG, font=("Segoe UI", 28, "bold")).pack(side="left", padx=(0, 12))
        title_box = tk.Frame(header, bg=self.BG)
        title_box.pack(side="left")
        tk.Label(title_box, text="PS3ToPC", fg=self.TEXT, bg=self.BG, font=("Segoe UI", 22, "bold")).pack(anchor="w")
        tk.Label(title_box, text="PS3 Legacy Console Edition  →  Minecraft Java Edition", fg=self.MUTED,
                 bg=self.BG, font=("Segoe UI", 10)).pack(anchor="w", pady=(2, 0))

        cards = tk.Frame(outer, bg=self.BG)
        cards.pack(fill="x")

        # Source card
        source_card = RoundedPanel(cards, width=900, height=158, bg=self.CARD, border=self.BORDER)
        source_card.canvas.pack(fill="x", pady=(0, 12))
        tk.Label(source_card.frame, text="1  ·  PS3 WORLD", fg=self.TEXT, bg=self.CARD,
                 font=("Segoe UI", 11, "bold")).pack(anchor="w", padx=18, pady=(12, 4))
        row = tk.Frame(source_card.frame, bg=self.CARD)
        row.pack(fill="x", padx=18)
        self.source_var = tk.StringVar(value="No folder selected")
        tk.Entry(row, textvariable=self.source_var, bg="#111319", fg=self.TEXT, insertbackground=self.TEXT,
                 relief="flat", highlightthickness=1, highlightbackground=self.BORDER,
                 highlightcolor=self.ACCENT, font=("Segoe UI", 10)).pack(side="left", fill="x", expand=True, ipady=9)
        self.source_btn = tk.Button(row, text="Choose folder", command=self._choose_source,
                                    bg="#262a33", fg=self.TEXT, activebackground="#333844", activeforeground=self.TEXT,
                                    relief="flat", borderwidth=0, font=("Segoe UI", 10, "bold"), padx=16, pady=9, cursor="hand2")
        self.source_btn.pack(side="left", padx=(10, 0))
        self.source_status = tk.Label(source_card.frame, text="The program will automatically search for GAMEDATA.",
                                      fg=self.MUTED, bg=self.CARD, font=("Segoe UI", 9))
        self.source_status.pack(anchor="w", padx=18, pady=(8, 0))

        # Output card
        output_card = RoundedPanel(cards, width=900, height=158, bg=self.CARD, border=self.BORDER)
        output_card.canvas.pack(fill="x", pady=(0, 12))
        tk.Label(output_card.frame, text="2  ·  DESTINATION", fg=self.TEXT, bg=self.CARD,
                 font=("Segoe UI", 11, "bold")).pack(anchor="w", padx=18, pady=(12, 4))
        row2 = tk.Frame(output_card.frame, bg=self.CARD)
        row2.pack(fill="x", padx=18)
        self.output_var = tk.StringVar(value="No destination selected")
        tk.Entry(row2, textvariable=self.output_var, bg="#111319", fg=self.TEXT, insertbackground=self.TEXT,
                 relief="flat", highlightthickness=1, highlightbackground=self.BORDER,
                 highlightcolor=self.ACCENT, font=("Segoe UI", 10)).pack(side="left", fill="x", expand=True, ipady=9)
        self.output_btn = tk.Button(row2, text="Choose folder", command=self._choose_output,
                                    bg="#262a33", fg=self.TEXT, activebackground="#333844", activeforeground=self.TEXT,
                                    relief="flat", borderwidth=0, font=("Segoe UI", 10, "bold"), padx=16, pady=9, cursor="hand2")
        self.output_btn.pack(side="left", padx=(10, 0))
        self.output_status = tk.Label(output_card.frame, text="A new output folder will be created inside it.",
                                      fg=self.MUTED, bg=self.CARD, font=("Segoe UI", 9))
        self.output_status.pack(anchor="w", padx=18, pady=(8, 0))

        # Progress card
        progress_card = RoundedPanel(cards, width=900, height=172, bg=self.CARD, border=self.BORDER)
        progress_card.canvas.pack(fill="x")
        top = tk.Frame(progress_card.frame, bg=self.CARD)
        top.pack(fill="x", padx=18, pady=(12, 0))
        self.status_var = tk.StringVar(value="Ready")
        tk.Label(top, textvariable=self.status_var, fg=self.TEXT, bg=self.CARD,
                 font=("Segoe UI", 11, "bold")).pack(side="left")
        self.percent_var = tk.StringVar(value="0%")
        tk.Label(top, textvariable=self.percent_var, fg=self.MUTED, bg=self.CARD,
                 font=("Segoe UI", 10)).pack(side="right")

        self.progress = self.ttk.Progressbar(progress_card.frame, style="Dark.Horizontal.TProgressbar",
                                             orient="horizontal", mode="determinate", maximum=100, value=0)
        self.progress.pack(fill="x", padx=18, pady=(12, 10))

        self.convert_btn = tk.Button(progress_card.frame, text="CONVERT WORLD", command=self._start_conversion,
                                     bg=self.ACCENT, fg="white", activebackground=self.ACCENT_HOVER, activeforeground="white",
                                     disabledforeground="#747986", relief="flat", borderwidth=0,
                                     font=("Segoe UI", 11, "bold"), padx=22, pady=10, cursor="hand2")
        self.convert_btn.pack(side="left", padx=(18, 8))
        self.open_btn = tk.Button(progress_card.frame, text="Open output", command=self._open_output,
                                  bg="#262a33", fg=self.TEXT, activebackground="#333844", activeforeground=self.TEXT,
                                  relief="flat", borderwidth=0, font=("Segoe UI", 10, "bold"), padx=18, pady=9,
                                  state="disabled", cursor="hand2")
        self.open_btn.pack(side="left")

        footer = tk.Frame(outer, bg=self.BG)
        footer.pack(fill="x", pady=(12, 0))
        self.detail_var = tk.StringVar(value="")
        tk.Label(footer, textvariable=self.detail_var, fg=self.MUTED, bg=self.BG,
                 font=("Segoe UI", 9)).pack(side="left")
        tk.Label(footer, text=f"v{APP_VERSION}  ·  M.INC.", fg="#636a78", bg=self.BG,
                 font=("Segoe UI", 9)).pack(side="right")

        self._update_convert_state()

    def _choose_source(self):
        from tkinter import filedialog
        selected = filedialog.askdirectory(title="Choose the PS3 save folder")
        if not selected:
            return
        folder = Path(selected)
        self.source_folder = folder
        self.source_var.set(str(folder))
        self.source_status.config(text="Searching for GAMEDATA…", fg=self.MUTED)
        self.status_var.set("Analyzing save…")
        self.progress["value"] = 0
        self.percent_var.set("0%")
        self.open_btn.config(state="disabled")
        self.gamedata_path = None

        def worker():
            candidates = find_gamedata(folder)
            self.log_queue.put(("source", candidates))

        threading.Thread(target=worker, daemon=True).start()

    def _choose_output(self):
        from tkinter import filedialog
        selected = filedialog.askdirectory(title="Choose the destination folder")
        if not selected:
            return
        self.output_parent = Path(selected)
        self.output_var.set(str(self.output_parent))
        self.output_status.config(text="Destination ready.", fg=self.SUCCESS)
        self._update_convert_state()

    def _update_convert_state(self):
        ready = self.gamedata_path is not None and self.output_parent is not None and not self.running
        self.convert_btn.config(state="normal" if ready else "disabled")

    def _start_conversion(self):
        from tkinter import messagebox
        if not self.gamedata_path or not self.output_parent or self.running:
            return

        label = source_display_name(self.source_folder or self.gamedata_path.parent, self.gamedata_path)
        folder_name = safe_name(f"{label}_Java")
        output_dir = self.output_parent / folder_name
        counter = 2
        while output_dir.exists() and any(output_dir.iterdir()):
            output_dir = self.output_parent / safe_name(f"{label}_Java_{counter}")
            counter += 1

        try:
            output_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            messagebox.showerror(APP_NAME, f"Unable to create destination:\n{e}")
            return

        self.last_output = output_dir
        self.running = True
        self.region_total = None
        self.progress["value"] = 0
        self.percent_var.set("0%")
        self.status_var.set("Converting…")
        self.detail_var.set(f"Output: {output_dir}")
        self.open_btn.config(state="disabled")
        self._update_convert_state()

        if getattr(sys, "frozen", False):
            cmd = [sys.executable, "--convert", str(self.gamedata_path), str(output_dir)]
        else:
            cmd = [sys.executable, "-u", str(CONVERTER), str(self.gamedata_path), str(output_dir)]

        def runner():
            env = os.environ.copy()
            env["PYTHONUNBUFFERED"] = "1"
            try:
                self.process = subprocess.Popen(cmd, cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                                text=True, bufsize=1, encoding="utf-8", errors="replace",
                                                creationflags=(getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0), env=env)
                assert self.process.stdout is not None
                for line in self.process.stdout:
                    self.log_queue.put(("line", line.rstrip()))
                code = self.process.wait()
                self.log_queue.put(("done", code))
            except Exception as e:
                self.log_queue.put(("error", str(e)))

        threading.Thread(target=runner, daemon=True).start()

    def _handle_source_result(self, candidates):
        from tkinter import messagebox
        if len(candidates) == 0:
            self.gamedata_path = None
            self.source_status.config(text="GAMEDATA not found. Choose the parent folder of the PS3 save.", fg=self.WARNING)
            self.status_var.set("Save not recognized")
            self._update_convert_state()
            return
        if len(candidates) == 1:
            self.gamedata_path = candidates[0]
        else:
            # Prefer the first candidate and make the full path visible.
            self.gamedata_path = candidates[0]
            extra = len(candidates) - 1
            self.source_status.config(text=f"Found {len(candidates)} GAMEDATA files. Using the candidate closest to the selected folder.", fg=self.WARNING)
            messagebox.showinfo(APP_NAME, "Multiple PS3 saves were found in the folder.\n\n"
                                 f"Will automatically use:\n{candidates[0]}\n\n"
                                 f"Others found: {extra}")
            self._finalize_source_status()
            return
        self._finalize_source_status()

    def _finalize_source_status(self):
        if self.gamedata_path:
            self.source_status.config(text=f"✓ GAMEDATA found: {self.gamedata_path}", fg=self.SUCCESS)
            self.status_var.set("Save recognized")
            if self.output_parent:
                self.detail_var.set(f"Ready: {self.gamedata_path.name}")
        self._update_convert_state()

    def _handle_line(self, line: str):
        if not line:
            return
        m = re.search(r"Found\s+(\d+)\s+MCR region files", line)
        if m:
            self.region_total = int(m.group(1))
            self.status_var.set(f"Converting {self.region_total} regions…")
            return

        m = re.search(r"\[#+-]+\]\s+(\d+)\/(\d+)\s+\((\d+)%\)", line)
        if m:
            done, total, pct = map(int, m.groups())
            # Reserve a small tail for post-processing and metadata generation.
            shown = min(92, int(pct * 0.92))
            self.progress["value"] = shown
            self.percent_var.set(f"{shown}%")
            self.detail_var.set(f"Regions: {done}/{total}")
            return

        if "Region conversion complete" in line:
            self.status_var.set("Finalizing world…")
            self.progress["value"] = 93
            self.percent_var.set("93%")
            return
        if "Generated level.dat" in line or "Successfully extracted" in line:
            self.progress["value"] = max(float(self.progress["value"]), 96)
            self.percent_var.set(f"{int(float(self.progress['value']))}%")
        if "Warning" in line:
            self.detail_var.set(line)
        elif line.startswith("Extracted player"):
            self.detail_var.set(line.replace("Extracted player", "Player:"))

    def _poll_log(self):
        try:
            while True:
                item = self.log_queue.get_nowait()
                kind = item[0]
                if kind == "source":
                    self._handle_source_result(item[1])
                elif kind == "line":
                    self._handle_line(item[1])
                elif kind == "done":
                    self._finish(item[1])
                elif kind == "error":
                    self._finish(-1, item[1])
        except queue.Empty:
            pass
        self.root.after(120, self._poll_log)

    def _finish(self, code: int, error: str | None = None):
        self.running = False
        self.process = None
        if code == 0:
            self.progress["value"] = 100
            self.percent_var.set("100%")
            self.status_var.set("✓ Conversion complete")
            self.detail_var.set(f"Java world ready: {self.last_output}")
            self.open_btn.config(state="normal")
        else:
            self.progress["value"] = 0
            self.percent_var.set("0%")
            self.status_var.set("Conversion failed")
            self.detail_var.set(error or "Check the GAMEDATA path and the terminal log.")
        self._update_convert_state()

    def _open_output(self):
        if not self.last_output or not self.last_output.exists():
            return
        try:
            if sys.platform.startswith("win"):
                os.startfile(str(self.last_output))
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(self.last_output)])
            else:
                subprocess.Popen(["xdg-open", str(self.last_output)])
        except Exception:
            pass

    def _close(self):
        from tkinter import messagebox
        if self.running:
            if not messagebox.askyesno(APP_NAME, "Conversion is still in progress. Do you want to close anyway?"):
                return
        self.root.destroy()

    def run(self):
        self.root.mainloop()


def main():
    if sys.version_info < MIN_PYTHON:
        raise SystemExit(f"Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]} or higher is required.")
    try:
        App().run()
    except ImportError as e:
        if "tkinter" in str(e).lower():
            raise SystemExit("Tkinter is not available. On Linux, install the python3-tk package and try again.")
        raise


if __name__ == "__main__":
    main()