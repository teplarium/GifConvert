"""The application window: a canvas-drawn UI with "liquid glass" elements.

Backgrounds, cards and glass are pre-rendered with Pillow (see glass.py) whenever the
window size changes; text, icons and input fields are regular tkinter items on top,
so they stay crisp at any scaling.
"""

from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
from dataclasses import dataclass
from pathlib import Path
from tkinter import filedialog, messagebox
from tkinter import font as tkfont

from PIL import Image, ImageTk

from . import APP_NAME, config, core
from . import glass as G

HEX = G.hex_color
WIDTH_PRESETS = ["320", "480", "640", "800", "1080"]
FPS_PRESETS = ["10", "12", "15", "20", "24", "25", "30", str(core.MAX_GIF_FPS)]
ERROR_BG = (253, 236, 236)


@dataclass
class Item:
    path: Path
    info: core.VideoInfo | None = None
    state: str = "wait"            # wait | work | done | error | cancelled
    result: core.Result | None = None
    error: str = ""
    progress: float = 0.0


# ---------------------------------------------------------------- system helpers

def _enable_windows_dpi_awareness() -> None:
    """Without this the window is blurry on displays scaled to 125–200%."""
    if sys.platform != "win32":
        return
    import ctypes
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def _open_path(path: Path) -> None:
    """Open a file or folder with the default application."""
    try:
        if sys.platform == "win32":
            os.startfile(str(path))  # noqa: S606 — intended
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)], env=core.clean_env())
    except OSError as e:
        messagebox.showerror(APP_NAME, f"Can't open:\n{path}\n\n{e}")


def _reveal(path: Path) -> None:
    """Show a file in Explorer / Finder (on Linux: open its folder)."""
    try:
        if sys.platform == "win32":
            subprocess.Popen(["explorer", "/select,", str(path)])
            return
        if sys.platform == "darwin":
            subprocess.Popen(["open", "-R", str(path)])
            return
    except OSError:
        pass
    _open_path(path.parent)


def _fit(text: str, font: tkfont.Font, width: float, middle: bool = False) -> str:
    """Shorten text with an ellipsis so that it fits into width pixels."""
    if width <= 0:
        return ""
    if font.measure(text) <= width:
        return text
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        cand = (text[: (mid + 1) // 2] + "…" + text[len(text) - mid // 2:]) if middle \
            else text[:mid] + "…"
        if font.measure(cand) <= width:
            lo = mid
        else:
            hi = mid - 1
    if middle:
        return text[: (lo + 1) // 2] + "…" + text[len(text) - lo // 2:]
    return text[:lo] + "…"


class PlaceholderEntry(tk.Entry):
    """A borderless entry with gray placeholder text."""

    def __init__(self, master, placeholder: str, font, **kw):
        super().__init__(master, relief="flat", bd=0, highlightthickness=0, font=font,
                         bg=HEX(G.FIELD), fg=HEX(G.INK), insertbackground=HEX(G.INK),
                         disabledbackground=HEX(G.FIELD), disabledforeground=HEX(G.FAINT),
                         selectbackground=HEX(G.LAVENDER), selectforeground=HEX(G.INK), **kw)
        self.placeholder = placeholder
        self._ph = False
        self.bind("<FocusIn>", self._focus_in)
        self.bind("<FocusOut>", self._focus_out)
        self._show_placeholder()

    def _show_placeholder(self):
        if not super().get():
            self._ph = True
            self.configure(fg=HEX(G.MUTED))
            self.insert(0, self.placeholder)

    def _focus_in(self, _e=None):
        if self._ph:
            self.delete(0, "end")
            self.configure(fg=HEX(G.INK))
            self._ph = False

    def _focus_out(self, _e=None):
        self._show_placeholder()

    def value(self) -> str:
        return "" if self._ph else super().get().strip()

    def set_value(self, text: str):
        state = self.cget("state")
        self.configure(state="normal")
        self.delete(0, "end")
        self._ph = False
        self.configure(fg=HEX(G.INK))
        if text:
            self.insert(0, text)
        else:
            self._show_placeholder()
        self.configure(state=state)


# ---------------------------------------------------------------- window

class App:
    BASE_W, BASE_H = 820, 760
    MIN_W, MIN_H = 700, 640

    def __init__(self, root: tk.Tk, ffmpeg: str, dnd: bool):
        self.root = root
        self.ffmpeg = ffmpeg
        self.dnd = dnd
        self.converter = core.Converter(ffmpeg)
        self.items: dict[str, Item] = {}
        self.order: list[str] = []
        self.selected: set[str] = set()
        self._next_id = 0
        self.events: queue.Queue = queue.Queue()
        self.probe_queue: queue.Queue = queue.Queue()
        self.worker: threading.Thread | None = None
        self.last_result: core.Result | None = None
        self.cfg = config.load()
        self.out_dir: Path | None = None
        self.mode = "1"
        self.status = "Add videos to get started"
        self.overall = 0.0
        self.hot: str | None = None
        self.list_hover: str | None = None
        self.drop_hover = False
        self._photos: dict = {}
        self._cache: dict = {}
        self._size = (0, 0)
        self._render_job = None
        self._list_dirty = True
        self._anim = None

        self.ws = root.tk.call("tk", "windowingsystem")  # win32 / aqua / x11
        self.S = 1.0 if self.ws == "aqua" else max(1.0, root.winfo_fpixels("1i") / 96.0)
        root.title(APP_NAME)
        root.configure(bg=HEX(G.BG))
        self._set_icon()
        self._make_fonts()
        self._build()
        self._load_settings()
        if self.dnd:
            self._setup_dnd()
        if self.ws == "aqua":
            # files dropped onto the app icon in the Dock / Finder
            root.createcommand("::tk::mac::OpenDocument", lambda *p: self.add_files(p))

        threading.Thread(target=self._probe_loop, daemon=True).start()
        root.protocol("WM_DELETE_WINDOW", self._on_close)
        root.after(100, self._poll)
        self._initial_geometry()

    # --- setup
    def _set_icon(self):
        icon = core.resource_path("assets/icon.png")
        if icon.is_file() and self.ws != "aqua":  # macOS takes the icon from the .app
            try:
                self._icon_img = tk.PhotoImage(file=str(icon))
                self.root.iconphoto(True, self._icon_img)
            except tk.TclError:
                pass

    def _make_fonts(self):
        families = set(tkfont.families(self.root))
        if self.ws == "aqua":
            family = tkfont.nametofont("TkDefaultFont").actual("family")
        elif self.ws == "win32":
            family = "Segoe UI"
        else:
            family = next((f for f in ("Inter", "Cantarell", "Noto Sans", "Ubuntu",
                                       "DejaVu Sans") if f in families),
                          tkfont.nametofont("TkDefaultFont").actual("family"))
        k = 1.0 if self.ws == "aqua" else self.S

        def f(size, weight="normal"):
            return tkfont.Font(self.root, family=family, size=-round(size * k), weight=weight)
        self.f_title = f(27, "bold")
        self.f_sub = f(12)
        self.f_h2 = f(15, "bold")
        self.f_body = f(13)
        self.f_body_b = f(13, "bold")
        self.f_small = f(11)
        self.f_small_b = f(11, "bold")
        self.f_badge = f(10, "bold")
        self.f_button = f(14, "bold")
        self.f_empty = f(17, "bold")

    def _px(self, v: float) -> int:
        return int(round(v * self.S))

    def _build(self):
        c = self.canvas = tk.Canvas(self.root, bg=HEX(G.BG), highlightthickness=0, bd=0)
        c.pack(fill="both", expand=True)
        self.bg_item = c.create_image(0, 0, anchor="nw", tags=("bgimg",))
        for name in ("seg", "plus_h", "folder_h", "go", "tube"):
            setattr(self, f"ov_{name}", c.create_image(0, 0, anchor="nw", tags=("ov",)))

        ink, muted = HEX(G.INK), HEX(G.MUTED)

        def text(font, fill=ink, anchor="nw", **kw):
            return c.create_text(0, 0, anchor=anchor, font=font, fill=fill, tags=("fg",), **kw)
        self.t_title = text(self.f_title, text="Video → GIF")
        self.t_sub = text(self.f_sub, muted, text=f"Best possible GIF quality · {core.FORMATS_HINT}")
        self.t_files = text(self.f_h2, text="Files")
        self.t_count = text(self.f_small, muted)
        self.t_clear = text(self.f_small_b, muted, text="Clear list", anchor="ne")
        self.t_seg = [(text(self.f_body_b, text=m.title), text(self.f_small, muted, text=m.hint))
                      for m in core.MODES.values()]
        self.t_labels = [text(self.f_small, muted, text=t) for t in
                         ("Width, px", "Frame rate, fps", "Start", "Duration, s")]
        self.t_out_label = text(self.f_small, muted, text="Save to", anchor="w")
        self.t_out_value = text(self.f_body, anchor="w")
        self.t_change = text(self.f_small_b, text="Change…", anchor="e")
        self.t_reset = text(self.f_small_b, muted, text="Reset", anchor="e")
        self.t_status = text(self.f_body_b, anchor="w")
        self.t_go = c.create_text(0, 0, font=self.f_button, fill="#ffffff", text="Convert",
                                  tags=("fg",))
        lw = max(1.6, 1.7 * self.S)
        self.i_plus = [c.create_line(0, 0, 0, 0, width=lw * 1.2, fill=ink, capstyle="round",
                                     tags=("fg",)) for _ in range(2)]
        self.i_folder = c.create_line(0, 0, 0, 0, width=lw, fill=ink, joinstyle="round",
                                      capstyle="round", tags=("fg",))
        self.i_chev = [c.create_line(0, 0, 0, 0, width=lw, fill=muted, capstyle="round",
                                     joinstyle="round", tags=("fg",)) for _ in range(2)]

        # file list: a separate canvas so it can scroll
        self.list = tk.Canvas(c, bg="#ffffff", highlightthickness=0, bd=0, takefocus=1)
        self.list_win = c.create_window(0, 0, anchor="nw", window=self.list)
        self.list.bind("<Configure>", lambda e: self._mark_list())
        self.list.bind("<Motion>", self._list_motion)
        self.list.bind("<Leave>", lambda e: self._set_list_hover(None))
        self.list.bind("<Button-1>", self._list_click)
        self.list.bind("<Double-1>", self._list_double)
        self.list.bind("<Delete>", lambda e: self.remove_selected())
        self.list.bind("<BackSpace>", lambda e: self.remove_selected())
        if self.ws == "x11":
            self.list.bind("<Button-4>", lambda e: self._scroll(-1))
            self.list.bind("<Button-5>", lambda e: self._scroll(1))
        else:
            self.list.bind("<MouseWheel>", self._wheel)

        # parameter fields
        self.e_width = PlaceholderEntry(c, "original", self.f_body)
        self.e_fps = PlaceholderEntry(c, "as source", self.f_body)
        self.e_start = PlaceholderEntry(c, "0:00", self.f_body)
        self.e_duration = PlaceholderEntry(c, "to the end", self.f_body)
        self.entries = [self.e_width, self.e_fps, self.e_start, self.e_duration]
        self.entry_wins = [c.create_window(0, 0, anchor="w", window=e) for e in self.entries]
        for e in self.entries:
            e.bind("<Return>", lambda ev: self.toggle_run())

        c.bind("<Configure>", self._on_configure)
        c.bind("<Motion>", self._motion)
        c.bind("<Leave>", lambda e: self._set_hot(None))
        c.bind("<Button-1>", self._click)

    def _initial_geometry(self):
        S = self.S
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        w = int(min(self.BASE_W * S, sw - 40 * S))
        h = int(min(self.BASE_H * S, sh - 90 * S))
        self.root.minsize(int(min(self.MIN_W * S, w)), int(min(self.MIN_H * S, h)))
        x, y = max(0, (sw - w) // 2), max(0, (sh - h) // 3)
        self.root.geometry(f"{w}x{h}+{x}+{y}")

    def _setup_dnd(self):
        try:
            from tkinterdnd2 import DND_FILES
            for w in (self.canvas, self.list):
                w.drop_target_register(DND_FILES)
                w.dnd_bind("<<DropEnter>>", self._drop_enter)
                w.dnd_bind("<<DropLeave>>", self._drop_leave)
                w.dnd_bind("<<Drop>>", self._on_drop)
        except Exception:
            self.dnd = False

    def _drop_enter(self, event):
        self.drop_hover = True
        self._mark_list()
        return event.action

    def _drop_leave(self, event):
        self.drop_hover = False
        self._mark_list()
        return event.action

    def _on_drop(self, event):
        self.drop_hover = False
        self.add_files(self.root.tk.splitlist(event.data))
        self._mark_list()
        return event.action

    # --- layout
    def _layout(self, W: int, H: int) -> dict:
        p = self._px
        M = p(22)
        R = {}
        R["plus"] = (W - M - p(46), p(18), W - M, p(64))
        bar = R["bar"] = (M, H - M - p(76), W - M, H - M)
        params_h, seg_h, gap = p(128), p(70), p(14)
        files_bottom = bar[1] - gap - params_h - gap - seg_h - gap
        files = R["files"] = (M, p(84), W - M, max(p(84) + p(160), files_bottom))
        seg = R["seg"] = (M, files[3] + gap, W - M, files[3] + gap + seg_h)
        params = R["params"] = (M, seg[3] + gap, W - M, seg[3] + gap + params_h)
        R["list"] = (files[0] + p(10), files[1] + p(52), files[2] - p(10), files[3] - p(10))
        inner = (seg[0] + p(6), seg[1] + p(6), seg[2] - p(6), seg[3] - p(6))
        sw = (inner[2] - inner[0]) / 3
        R["slots"] = [(inner[0] + i * sw, inner[1], inner[0] + (i + 1) * sw, inner[3])
                      for i in range(3)]
        px0, px1, fgap = params[0] + p(20), params[2] - p(20), p(12)
        cw = (px1 - px0 - 3 * fgap) / 4
        R["fields"] = [(px0 + i * (cw + fgap), params[1] + p(36),
                        px0 + i * (cw + fgap) + cw, params[1] + p(74)) for i in range(4)]
        R["out_y"] = params[1] + p(102)
        R["folder"] = (bar[0] + p(12), bar[1] + p(12), bar[0] + p(64), bar[3] - p(12))
        R["go"] = (bar[2] - p(12) - p(188), bar[1] + p(12), bar[2] - p(12), bar[3] - p(12))
        R["mid"] = (R["folder"][2] + p(16), R["go"][0] - p(22))
        R["tube"] = (int(R["mid"][0]), bar[1] + p(46), int(R["mid"][1]), bar[1] + p(54))
        return R

    def _on_configure(self, event):
        size = (event.width, event.height)
        if size[0] < 200 or size[1] < 200 or size == self._size:
            return
        self._size = size
        self.R = self._layout(*size)
        self._place()
        if self._render_job:
            self.root.after_cancel(self._render_job)
        self._render_job = self.root.after(90, self._render)

    def _place(self):
        """Move text, icons and widgets to their places (cheap; happens on every resize)."""
        c, R, p = self.canvas, self.R, self._px
        W, H = self._size
        c.coords(self.t_title, R["files"][0], p(14))
        c.coords(self.t_sub, R["files"][0] + p(1), p(54))
        cx, cy = (R["plus"][0] + R["plus"][2]) / 2, (R["plus"][1] + R["plus"][3]) / 2
        c.coords(self.i_plus[0], cx - p(8), cy, cx + p(8), cy)
        c.coords(self.i_plus[1], cx, cy - p(8), cx, cy + p(8))

        f = R["files"]
        c.coords(self.t_files, f[0] + p(22), f[1] + p(18))
        c.coords(self.t_count, f[0] + p(22) + self.f_h2.measure("Files") + p(10), f[1] + p(22))
        c.coords(self.t_clear, f[2] - p(22), f[1] + p(21))
        lx0, ly0, lx1, ly1 = R["list"]
        c.coords(self.list_win, lx0, ly0)
        c.itemconfigure(self.list_win, width=lx1 - lx0, height=ly1 - ly0)

        for (t_title, t_hint), slot in zip(self.t_seg, R["slots"]):
            c.coords(t_title, slot[0] + p(16), slot[1] + p(12))
            c.coords(t_hint, slot[0] + p(16), slot[1] + p(32))
            c.itemconfigure(t_hint, width=slot[2] - slot[0] - p(26))

        for i, (box, label, win) in enumerate(zip(R["fields"], self.t_labels, self.entry_wins)):
            c.coords(label, box[0] + p(4), box[1] - p(20))
            right = box[2] - p(16) - (p(20) if i < 2 else 0)
            c.coords(win, box[0] + p(16), (box[1] + box[3]) / 2)
            c.itemconfigure(win, width=max(10, right - box[0] - p(16)),
                            height=self.f_body.metrics("linespace") + p(2))
            if i < 2:
                x, y = box[2] - p(20), (box[1] + box[3]) / 2
                c.coords(self.i_chev[i], x - p(4), y - p(2), x, y + p(2), x + p(4), y - p(2))

        y = R["out_y"]
        x0 = R["params"][0] + p(24)
        c.coords(self.t_out_label, x0, y)
        c.coords(self.t_change, R["params"][2] - p(24), y)
        c.coords(self.t_reset, R["params"][2] - p(24) - self.f_small_b.measure("Change…") - p(18), y)
        self._update_out_label()

        fx0, fy0, fx1, fy1 = R["folder"]
        cx, cy = (fx0 + fx1) / 2, (fy0 + fy1) / 2
        w2, h2 = p(10), p(7.5)
        c.coords(self.i_folder,
                 cx - w2, cy - h2 + p(2), cx - w2, cy + h2, cx + w2, cy + h2, cx + w2, cy - h2 + p(4),
                 cx - p(1), cy - h2 + p(4), cx - p(3.5), cy - h2 + p(1), cx - w2 + p(1), cy - h2 + p(1),
                 cx - w2, cy - h2 + p(2))
        g = R["go"]
        c.coords(self.t_go, (g[0] + g[2]) / 2, (g[1] + g[3]) / 2)
        c.coords(self.t_status, R["mid"][0], R["bar"][1] + p(30))
        self._update_texts()
        self._mark_list()

    # --- rendering
    def _render(self):
        self._render_job = None
        W, H = self._size
        S, p, R = self.S, self._px, self.R
        P = p(48)

        def off(b):
            return (b[0] + P, b[1] + P, b[2] + P, b[3] + P)

        img = G.background(W + 2 * P, H + 2 * P, S)
        G.card(img, off(R["files"]), p(26), S)
        G.card(img, off(R["params"]), p(26), S)
        for box in R["fields"]:
            G.fill_rr(img, off(box), (box[3] - box[1]) / 2, G.FIELD)
        G.glass(img, off(R["seg"]), p(24), S, refraction=10, bezel=16, magnify=1.03,
                tint=0.22, frost=0.6)
        bar = R["bar"]
        G.glass(img, off(bar), (bar[3] - bar[1]) / 2, S, refraction=22, bezel=26,
                magnify=1.07, tint=0.08)

        button = dict(refraction=12, bezel=20, magnify=1.25, tint=0.12)
        hover = dict(refraction=12, bezel=20, magnify=1.32, tint=0.32)
        self._button_overlay(img, P, "folder", R["folder"], button, hover)
        self._button_overlay(img, P, "plus", R["plus"], button, hover)

        # parts that are re-rendered later on top of this base
        m = p(40)
        seg = R["seg"]
        self._seg_region = (seg[0] - m, seg[1] - m, seg[2] + m, seg[3] + m)
        self._seg_base = img.crop(off(self._seg_region))
        self._go_back = img.crop(off(R["go"]))
        self._tube_back = img.crop(off(R["tube"]))

        self._photo("bg", img.crop((P, P, P + W, P + H)), self.bg_item, 0, 0)
        self._draw_seg(float(int(self.mode) - 1))
        self._draw_go()
        self._draw_tube()
        self._restack()

    def _button_overlay(self, img, P, name, box, normal, hover):
        S, m = self.S, self._px(50)
        r = (box[2] - box[0]) / 2
        region = (box[0] - m + P, box[1] - m + P, box[2] + m + P, box[3] + m + P)
        local = (m, m, m + box[2] - box[0], m + box[3] - box[1])
        sub = img.crop(region)
        hov = sub.copy()
        G.glass(sub, local, r, S, **normal)
        G.glass(hov, local, r, S, **hover)
        img.paste(sub, region[:2])
        item = getattr(self, f"ov_{name}_h")
        self._photo(f"{name}_h", hov, item, region[0] - P, region[1] - P)
        self.canvas.itemconfigure(item, state="normal" if self.hot == name else "hidden")

    def _photo(self, key, pil_img, item, x, y):
        photo = ImageTk.PhotoImage(pil_img)
        self._photos[key] = photo
        self.canvas.itemconfigure(item, image=photo)
        self.canvas.coords(item, x, y)

    def _restack(self):
        c = self.canvas
        c.tag_lower("bgimg")
        c.tag_raise("ov")
        c.tag_raise("fg")

    def _draw_seg(self, pos: float):
        """The segmented control's glass lens over slot `pos` (0..2, fractions while animating)."""
        if not hasattr(self, "_seg_base"):
            return
        sub = self._seg_base.copy()
        rx0, ry0 = self._seg_region[:2]
        slots = self.R["slots"]
        i0 = min(1, int(pos)) if pos < 2 else 1
        frac = pos - i0
        x0 = slots[i0][0] + (slots[i0 + 1][0] - slots[i0][0]) * frac
        w = slots[0][2] - slots[0][0]
        box = (x0 - rx0, slots[0][1] - ry0, x0 + w - rx0, slots[0][3] - ry0)
        G.glass(sub, box, self._px(18), self.S, refraction=14, bezel=18, magnify=1.12,
                tint=0.55, frost=0.4, aberration=0.25)
        self._photo("seg", sub, self.ov_seg, rx0, ry0)

    def _draw_go(self):
        if not hasattr(self, "_go_back"):
            return
        state = "hover" if self.hot == "go" else "normal"
        back = self._go_back.copy()
        pill = G.black_pill(back.width, back.height, self.S, state)
        back.paste(pill, (0, 0), pill)
        self._photo("go", back, self.ov_go, self.R["go"][0], self.R["go"][1])

    def _draw_tube(self):
        if not hasattr(self, "_tube_back"):
            return
        img = G.progress_tube(self._tube_back, self.overall, self.S)
        self._photo("tube", img, self.ov_tube, self.R["tube"][0], self.R["tube"][1])

    def _animate_seg(self, old: int, new: int):
        if self._anim:
            self.root.after_cancel(self._anim)
        frames = 8

        def step(i=1):
            t = i / frames
            ease = 1 - (1 - t) ** 3
            self._draw_seg(old + (new - old) * ease)
            if i < frames:
                self._anim = self.root.after(16, step, i + 1)
            else:
                self._anim = None
        step()

    # --- texts that depend on state
    def _update_texts(self):
        c = self.canvas
        n = len(self.order)
        c.itemconfigure(self.t_count, text=f"{n} video{'s' if n != 1 else ''}" if n else "")
        c.itemconfigure(self.t_clear, state="normal" if n and not self._busy() else "hidden")
        busy = self._busy()
        c.itemconfigure(self.t_go, text="Stop" if busy else "Convert")
        for i, (t_title, t_hint) in enumerate(self.t_seg):
            active = str(i + 1) == self.mode
            c.itemconfigure(t_title, fill=HEX(G.INK) if active or not busy else HEX(G.MUTED))
            c.itemconfigure(t_hint, fill=HEX(G.MUTED) if active or not busy else HEX(G.FAINT))
        if hasattr(self, "R"):
            width = self.R["mid"][1] - self.R["mid"][0]
            c.itemconfigure(self.t_status, text=_fit(self.status, self.f_body_b, width))
            c.itemconfigure(self.t_reset, state="normal" if self.out_dir and not busy else "hidden")
            c.itemconfigure(self.t_change, fill=HEX(G.FAINT) if busy else
                            HEX(G.INK) if self.hot != "change" else HEX(G.MUTED))

    def _update_out_label(self):
        if not hasattr(self, "R"):
            return
        p = self._px
        x0 = self.R["params"][0] + p(24) + self.f_small.measure("Save to") + p(10)
        right = self.R["params"][2] - p(24) - self.f_small_b.measure("Change…") - p(18)
        if self.out_dir:
            right -= self.f_small_b.measure("Reset") + p(18)
            text = str(self.out_dir)
        else:
            text = f"a “{core.OUTPUT_DIR_NAME}” folder next to each video"
        self.canvas.coords(self.t_out_value, x0, self.R["out_y"])
        self.canvas.itemconfigure(self.t_out_value,
                                  text=_fit(text, self.f_body, right - x0, middle=True))

    def _set_status(self, text: str):
        self.status = text
        self._update_texts()

    # --- hover and clicks on the main canvas
    def _hotspots(self):
        if not hasattr(self, "R"):
            return []
        R, p = self.R, self._px
        busy = self._busy()
        spots = [("plus", R["plus"]), ("folder", R["folder"]), ("go", R["go"])]
        if not busy:
            spots += [(f"seg{i}", s) for i, s in enumerate(R["slots"])]
            for i in range(2):
                b = R["fields"][i]
                spots.append((f"chev{i}", (b[2] - p(38), b[1], b[2], b[3])))
            y = R["out_y"]
            cx = R["params"][2] - p(24)
            wch = self.f_small_b.measure("Change…")
            spots.append(("change", (cx - wch - p(6), y - p(12), cx + p(6), y + p(12))))
            if self.out_dir:
                rx = cx - wch - p(18)
                spots.append(("reset", (rx - self.f_small_b.measure("Reset") - p(6), y - p(12),
                                        rx + p(6), y + p(12))))
            if self.order:
                f = R["files"]
                spots.append(("clear", (f[2] - p(22) - self.f_small_b.measure("Clear list") - p(6),
                                        f[1] + p(14), f[2] - p(16), f[1] + p(40))))
        return spots

    def _hit(self, x, y):
        for name, (x0, y0, x1, y1) in self._hotspots():
            if x0 <= x <= x1 and y0 <= y <= y1:
                return name
        return None

    def _motion(self, event):
        self._set_hot(self._hit(event.x, event.y))

    def _set_hot(self, name):
        if name == self.hot:
            return
        old, self.hot = self.hot, name
        c = self.canvas
        c.configure(cursor="hand2" if name else "")
        for b in ("plus", "folder"):
            c.itemconfigure(getattr(self, f"ov_{b}_h"), state="normal" if name == b else "hidden")
        if "go" in (old, name):
            self._draw_go()
        c.itemconfigure(self.t_clear, fill=HEX(G.INK) if name == "clear" else HEX(G.MUTED))
        c.itemconfigure(self.t_reset, fill=HEX(G.INK) if name == "reset" else HEX(G.MUTED))
        for i in range(2):
            c.itemconfigure(self.i_chev[i], fill=HEX(G.INK) if name == f"chev{i}" else HEX(G.MUTED))
        self._update_texts()

    def _click(self, event):
        name = self._hit(event.x, event.y)
        self.canvas.focus_set()
        if name == "plus":
            self.choose_files()
        elif name == "folder":
            self.open_folder()
        elif name == "go":
            self.toggle_run()
        elif name and name.startswith("seg"):
            self.set_mode(str(int(name[3:]) + 1))
        elif name == "chev0":
            self._preset_menu(self.e_width, "Original", WIDTH_PRESETS, " px", event)
        elif name == "chev1":
            self._preset_menu(self.e_fps, "Same as source", FPS_PRESETS, " fps", event)
        elif name == "change":
            self.choose_out_dir()
        elif name == "reset":
            self.reset_out_dir()
        elif name == "clear":
            self.clear()

    def _preset_menu(self, entry: PlaceholderEntry, first: str, values, suffix, event):
        menu = tk.Menu(self.root, tearoff=0, font=self.f_body, bg="#ffffff", fg=HEX(G.INK),
                       activebackground=HEX(G.FIELD), activeforeground=HEX(G.INK), bd=0)
        menu.add_command(label=first, command=lambda: entry.set_value(""))
        menu.add_separator()
        for v in values:
            menu.add_command(label=v + suffix, command=lambda v=v: entry.set_value(v))
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def set_mode(self, mode: str):
        if mode == self.mode or mode not in core.MODES:
            return
        old = int(self.mode) - 1
        self.mode = mode
        self._update_texts()
        self._animate_seg(old, int(mode) - 1)

    # --- the file list
    def _mark_list(self):
        self._list_dirty = True

    def _row_h(self):
        return self._px(60)

    def _pill_image(self, kind, w, h, frac=None):
        key = ("pill", kind, w, h, None if frac is None else round(frac, 2))
        img = self._cache.get(key)
        if img is None:
            bg = {"done": G.INK, "error": ERROR_BG}.get(kind, G.FIELD)
            img = ImageTk.PhotoImage(G.status_pill(w, h, bg, frac))
            if len(self._cache) > 400:
                self._cache.clear()
            self._cache[key] = img
        return img

    def _rr_image(self, w, h, r, color, back=(255, 255, 255)):
        key = ("rr", w, h, r, color, back)
        img = self._cache.get(key)
        if img is None:
            pil = Image.new("RGB", (max(1, w), max(1, h)), back)
            G.fill_rr(pil, (0, 0, w, h), r, color)
            img = self._cache[key] = ImageTk.PhotoImage(pil)
        return img

    def _draw_list(self):
        self._list_dirty = False
        L, p = self.list, self._px
        L.delete("all")
        w, h = L.winfo_width(), L.winfo_height()
        if w < 50:
            return
        L.configure(bg="#F7F6FF" if self.drop_hover else "#ffffff")
        back = (247, 246, 255) if self.drop_hover else (255, 255, 255)
        if not self.order:
            self._draw_empty(w, h, back)
            L.configure(scrollregion=(0, 0, w, h))
            return
        RH = self._row_h()
        for i, iid in enumerate(self.order):
            it = self.items[iid]
            y = i * RH
            if iid in self.selected or iid == self.list_hover:
                color = (238, 240, 253) if iid in self.selected else (246, 246, 249)
                L.create_image(p(2), y + p(3), anchor="nw",
                               image=self._rr_image(w - p(4), RH - p(6), p(16), color, back))
            bx, by = p(12), y + (RH - p(40)) / 2
            L.create_image(bx, by, anchor="nw", image=self._rr_image(p(40), p(40), p(12), G.FIELD,
                                                                     back if iid not in self.selected and iid != self.list_hover else
                                                                     ((238, 240, 253) if iid in self.selected else (246, 246, 249))))
            ext = it.path.suffix.lstrip(".").upper()[:4] or "?"
            L.create_text(bx + p(20), by + p(20), text=ext, font=self.f_badge, fill=HEX(G.INK))

            # status pill on the right
            kind, frac = it.state, None
            if kind == "done" and it.result:
                label = f"Done · {core.format_size(it.result.size)}"
            elif kind == "work":
                label, frac = f"{int(it.progress * 100)}%", it.progress
            else:
                label = {"wait": "Waiting", "error": "Error", "cancelled": "Canceled"}[kind]
            pw = max(p(76), self.f_small_b.measure(label) + p(26))
            ph = p(26)
            px = w - p(14) - pw
            L.create_image(px, y + (RH - ph) / 2, anchor="nw", image=self._pill_image(kind, pw, ph, frac))
            fg = {"done": "#ffffff", "error": HEX(G.RED), "work": HEX(G.INK)}.get(kind, HEX(G.MUTED))
            L.create_text(px + pw / 2, y + RH / 2, text=label, font=self.f_small_b, fill=fg)

            tx = p(66)
            avail = px - tx - p(16)
            L.create_text(tx, y + p(13), anchor="nw", font=self.f_body_b, fill=HEX(G.INK),
                          text=_fit(it.path.name, self.f_body_b, avail, middle=True))
            if it.info is None:
                meta = "Reading…"
            elif it.info.has_video:
                meta = it.info.describe()
            else:
                meta = "Not a video or unsupported format"
            if it.state == "error" and it.error:
                meta = it.error.splitlines()[-1] if it.error else meta
            L.create_text(tx, y + p(34), anchor="nw", font=self.f_small,
                          fill=HEX(G.RED) if it.state == "error" else HEX(G.MUTED),
                          text=_fit(meta, self.f_small, avail))
            if i < len(self.order) - 1:
                L.create_line(tx, y + RH - 1, w - p(14), y + RH - 1, fill=HEX(G.LINE))
        total = len(self.order) * RH
        L.configure(scrollregion=(0, 0, w, max(h, total)))
        self._draw_scrollbar()

    def _draw_empty(self, w, h, back):
        L, p = self.list, self._px
        cx, cy = w / 2, h / 2 - p(22)
        r = p(30)
        color = G.LAVENDER if self.drop_hover else G.FIELD
        L.create_image(cx - r, cy - r, anchor="nw", image=self._rr_image(2 * r, 2 * r, r, color, back))
        lw = max(1.6, 2.0 * self.S)
        L.create_line(cx, cy + p(9), cx, cy - p(9), width=lw, fill=HEX(G.INK), capstyle="round")
        L.create_line(cx - p(7), cy - p(2), cx, cy - p(9), cx + p(7), cy - p(2), width=lw,
                      fill=HEX(G.INK), capstyle="round", joinstyle="round")
        title = "Release to add" if self.drop_hover else \
            ("Drop videos here" if self.dnd else "Click to add videos")
        L.create_text(cx, cy + r + p(22), text=title, font=self.f_empty, fill=HEX(G.INK))
        L.create_text(cx, cy + r + p(48), text=f"{core.FORMATS_HINT} · or click to browse",
                      font=self.f_small, fill=HEX(G.MUTED))

    def _draw_scrollbar(self):
        L, p = self.list, self._px
        L.delete("sb")
        f0, f1 = L.yview()
        if f1 - f0 >= 0.999:
            return
        h = L.winfo_height()
        top = L.canvasy(0)
        x = L.winfo_width() - p(4)
        L.create_line(x, top + p(6) + f0 * (h - p(12)), x, top + p(6) + f1 * (h - p(12)),
                      width=p(4), fill=HEX(G.FAINT), capstyle="round", tags=("sb",))

    def _scroll(self, units):
        if self.order:
            self.list.yview_scroll(units, "units")
            self._draw_scrollbar()

    def _wheel(self, event):
        d = event.delta
        if self.ws == "win32":
            d = d / 120
        self._scroll(-int(d) if d else 0)

    def _row_at(self, event):
        if not self.order:
            return None
        i = int(self.list.canvasy(event.y) // self._row_h())
        return self.order[i] if 0 <= i < len(self.order) else None

    def _set_list_hover(self, iid):
        if iid != self.list_hover:
            self.list_hover = iid
            self._mark_list()

    def _list_motion(self, event):
        iid = self._row_at(event)
        self._set_list_hover(iid)
        self.list.configure(cursor="hand2" if not self.order else "")

    def _list_click(self, event):
        self.list.focus_set()
        if not self.order:
            self.choose_files()
            return
        iid = self._row_at(event)
        ctrl = bool(event.state & 0x0004) or (self.ws == "aqua" and bool(event.state & 0x0008))
        if iid is None:
            self.selected.clear()
        elif ctrl:
            self.selected ^= {iid}
        else:
            self.selected = {iid}
        if len(self.selected) == 1 and not self._busy():
            it = self.items[next(iter(self.selected))]
            if it.state == "error":
                self._set_status(f"{it.path.name}: {it.error.splitlines()[-1] if it.error else 'error'}")
            elif it.result:
                self._set_status(f"{it.result.path}  ({core.format_size(it.result.size)})")
        self._mark_list()

    def _list_double(self, event):
        iid = self._row_at(event)
        it = self.items.get(iid) if iid else None
        if not it:
            return
        if it.result and it.result.path.exists():
            _open_path(it.result.path)
        elif it.state == "error":
            messagebox.showerror(APP_NAME, f"{it.path.name}\n\n{it.error}", parent=self.root)

    # --- adding and removing files
    def choose_files(self):
        exts = [e for e in core.VIDEO_EXTENSIONS if e != ".gif"]
        if self.ws == "x11":  # file masks are case-sensitive in the Linux dialog
            exts += [e.upper() for e in exts]
        last = self.cfg.get("last_dir")
        names = filedialog.askopenfilenames(
            parent=self.root, title="Choose videos",
            initialdir=last if last and Path(last).is_dir() else None,
            filetypes=[("Videos", " ".join("*" + e for e in exts)), ("All files", "*")])
        if names:
            self.add_files(self.root.tk.splitlist(names))

    def add_files(self, paths):
        known = {it.path for it in self.items.values()}
        new = []
        for raw in paths:
            path = Path(str(raw))
            if path.is_dir():
                new += sorted(c for c in path.iterdir() if c.is_file()
                              and c.suffix.lower() in core.VIDEO_EXTENSIONS
                              and c.suffix.lower() != ".gif")
            elif path.is_file():
                new.append(path)
        added = 0
        for path in new:
            path = path.absolute()
            if path in known:
                continue
            known.add(path)
            self._next_id += 1
            iid = f"f{self._next_id}"
            self.items[iid] = Item(path)
            self.order.append(iid)
            self.probe_queue.put((iid, path))
            added += 1
        if new:
            self.cfg["last_dir"] = str(new[-1].parent)
        if added and not self._busy():
            self._set_status(self._ready_text())
        self._update_texts()
        self._mark_list()

    def _ready_text(self):
        pending = sum(1 for it in self.items.values() if it.state != "done")
        if not self.items:
            return "Add videos to get started"
        if not pending:
            return "All videos are converted"
        return f"Ready · {pending} video{'s' if pending != 1 else ''} in the queue"

    def remove_selected(self):
        if self._busy() or not self.selected:
            return
        self.order = [i for i in self.order if i not in self.selected]
        for iid in self.selected:
            self.items.pop(iid, None)
        self.selected.clear()
        self._set_status(self._ready_text())
        self._mark_list()

    def clear(self):
        if self._busy():
            return
        self.order.clear()
        self.items.clear()
        self.selected.clear()
        self.overall = 0.0
        self._draw_tube()
        self._set_status(self._ready_text())
        self._mark_list()

    def _probe_loop(self):
        while True:
            iid, path = self.probe_queue.get()
            try:
                info = core.probe(self.ffmpeg, path)
            except Exception:
                info = core.VideoInfo()
            self.events.put(("probed", iid, info))

    # --- output folder
    def choose_out_dir(self):
        start = self.out_dir or (Path(self.cfg["last_dir"]) if self.cfg.get("last_dir") else None)
        d = filedialog.askdirectory(parent=self.root, title="Where to save GIFs",
                                    initialdir=str(start) if start and start.is_dir() else None)
        if d:
            self.out_dir = Path(d)
            self._update_out_label()
            self._update_texts()
            self._remember()

    def reset_out_dir(self):
        self.out_dir = None
        self._update_out_label()
        self._update_texts()
        self._remember()

    def open_folder(self):
        for iid in self.selected:
            it = self.items.get(iid)
            if it and it.result and it.result.path.exists():
                _reveal(it.result.path)
                return
        if self.last_result and self.last_result.path.exists():
            _reveal(self.last_result.path)
            return
        folder = self.out_dir
        if folder is None and self.order:
            folder = self.items[self.order[0]].path.parent / core.OUTPUT_DIR_NAME
        if folder and folder.is_dir():
            _open_path(folder)
        else:
            messagebox.showinfo(APP_NAME, "There is no GIF folder yet — convert a video first.",
                                parent=self.root)

    # --- settings
    def _load_settings(self):
        c = self.cfg
        if c.get("mode") in core.MODES:
            self.mode = c["mode"]
        if c.get("width"):
            self.e_width.set_value(str(c["width"]))
        if c.get("fps"):
            self.e_fps.set_value(core.format_number(float(c["fps"])))
        if c.get("out_dir") and Path(c["out_dir"]).is_dir():
            self.out_dir = Path(c["out_dir"])
        self._update_texts()

    def _remember(self, s: core.Settings | None = None):
        if s is not None:
            self.cfg.update(mode=s.mode, width=s.width, fps=s.fps)
        self.cfg["out_dir"] = str(self.out_dir) if self.out_dir else None
        config.save(self.cfg)

    def _read_settings(self) -> core.Settings | None:
        def bad(msg):
            messagebox.showerror(APP_NAME, msg, parent=self.root)
            return None

        width = None
        raw = self.e_width.value().lower().replace("px", "").strip()
        if raw:
            try:
                width = int(float(raw.replace(",", ".")))
                if width <= 0:
                    raise ValueError
            except ValueError:
                return bad("Width must be a whole number of pixels, e.g. 480.\n"
                           "Leave it empty to keep the original size.")
        fps = None
        raw = self.e_fps.value().lower().replace("fps", "").strip()
        if raw:
            try:
                fps = float(raw.replace(",", "."))
                if not 0 < fps < float("inf"):
                    raise ValueError
            except ValueError:
                return bad("Frame rate must be a number, e.g. 15.\n"
                           "Leave it empty to keep the source frame rate.")
            if fps > core.MAX_GIF_FPS:
                fps = core.MAX_GIF_FPS
                self.e_fps.set_value(str(core.MAX_GIF_FPS))
        try:
            start = core.parse_time(self.e_start.value())
        except ValueError:
            return bad("Start must be seconds (12.5) or minutes:seconds (1:30).")
        try:
            duration = core.parse_time(self.e_duration.value())
        except ValueError:
            return bad("Duration must be a number of seconds, e.g. 5.")
        if duration == 0:
            return bad("Duration must be greater than zero.")
        return core.Settings(mode=self.mode, width=width, fps=fps, start=start,
                             duration=duration, out_dir=self.out_dir)

    # --- conversion
    def _busy(self) -> bool:
        return self.worker is not None and self.worker.is_alive()

    def toggle_run(self):
        if self._busy():
            self.converter.cancel()
            self._set_status("Stopping…")
            return
        self.start()

    def start(self):
        if not self.items:
            self.choose_files()
            return
        s = self._read_settings()
        if s is None:
            return
        targets = [iid for iid in self.order if self.items[iid].state != "done"]
        if not targets:
            if not messagebox.askyesno(APP_NAME, "All videos are already converted.\n"
                                                 "Make the GIFs again?", parent=self.root):
                return
            targets = list(self.order)
        self._remember(s)
        self.converter.reset()
        for iid in targets:
            it = self.items[iid]
            it.state, it.progress, it.error = "wait", 0.0, ""
        self.overall = 0.0
        self._draw_tube()
        self.worker = threading.Thread(target=self._work, args=(targets, s), daemon=True)
        self.worker.start()
        self._set_busy(True)

    def _set_busy(self, busy: bool):
        for e in self.entries:
            e.configure(state="disabled" if busy else "normal")
        self._update_texts()
        self._mark_list()

    def _work(self, targets, s: core.Settings):
        done = failed = 0
        total_size = 0
        n = len(targets)
        for idx, iid in enumerate(targets):
            item = self.items.get(iid)
            if item is None:
                continue
            if self.converter.cancelled:
                break
            self.events.put(("start", iid, idx, n))

            def progress(frac, stage, iid=iid, idx=idx):
                self.events.put(("progress", iid, idx, n, frac, stage))
            try:
                result = self.converter.convert(item.path, s, progress, item.info)
                self.events.put(("done", iid, result))
                done += 1
                total_size += result.size
            except core.Cancelled:
                self.events.put(("cancelled", iid))
                break
            except core.ConversionError as e:
                self.events.put(("error", iid, str(e)))
                failed += 1
            except Exception as e:  # one broken file must not stop the queue
                self.events.put(("error", iid, f"{type(e).__name__}: {e}"))
                failed += 1
        self.events.put(("finished", done, failed, total_size, self.converter.cancelled))

    def _poll(self):
        tube_dirty = False
        try:
            while True:
                tube_dirty |= bool(self._handle(*self.events.get_nowait()))
        except queue.Empty:
            pass
        if tube_dirty:
            self._draw_tube()
        if self._list_dirty:
            self._draw_list()
        self.root.after(100, self._poll)

    def _handle(self, kind, *a):
        """Apply an event from a worker thread. Returns True if the progress bar changed."""
        it = self.items.get(a[0]) if a and isinstance(a[0], str) else None
        if kind == "probed":
            if it:
                it.info = a[1]
                self._mark_list()
        elif kind == "start":
            _, idx, n = a
            if it:
                it.state, it.progress = "work", 0.0
                self._scroll_to(a[0])
            self._mark_list()
        elif kind == "progress":
            _, idx, n, frac, stage = a
            self.overall = (idx + frac) / n
            if it and it.state == "work":
                it.progress = frac
                name = it.path.name
                self._set_status(f"{idx + 1}/{n} · {name} · {stage} · {int(frac * 100)}%")
            self._mark_list()
            return True
        elif kind == "done":
            if it:
                it.state, it.result, it.error = "done", a[1], ""
            self.last_result = a[1]
            self._mark_list()
        elif kind == "error":
            if it:
                it.state, it.error = "error", a[1]
            self._mark_list()
        elif kind == "cancelled":
            if it:
                it.state = "cancelled"
            self._mark_list()
        elif kind == "finished":
            done, failed, size, cancelled = a
            self._set_busy(False)
            if cancelled:
                self._set_status(f"Stopped · {done} GIF{'s' if done != 1 else ''} done")
            else:
                self.overall = 1.0
                msg = f"Done · {done} GIF{'s' if done != 1 else ''}, {core.format_size(size)}"
                if failed:
                    msg += f" · {failed} failed — click a file to see why"
                self._set_status(msg)
            return True
        return False

    def _scroll_to(self, iid):
        if iid not in self.order:
            return
        RH = self._row_h()
        i = self.order.index(iid)
        total = len(self.order) * RH
        h = self.list.winfo_height()
        if total <= h:
            return
        top, bottom = self.list.canvasy(0), self.list.canvasy(h)
        if i * RH < top or (i + 1) * RH > bottom:
            self.list.yview_moveto(max(0, (i * RH - h / 3)) / total)

    def _on_close(self):
        if self._busy():
            if not messagebox.askyesno(APP_NAME, "A conversion is running. Stop it and quit?",
                                       parent=self.root):
                return
            self.converter.cancel()
            self.worker.join(timeout=5)
        self._remember()
        self.root.destroy()


def _create_root() -> tuple[tk.Tk, bool]:
    root = tk.Tk(className=APP_NAME)
    try:
        from tkinterdnd2 import TkinterDnD
        root.TkdndVersion = TkinterDnD._require(root)  # drag & drop of files into the window
        return root, True
    except Exception:
        return root, False  # works without tkinterdnd2 too, just without drag & drop


def run(files=(), ffmpeg: str | None = None) -> int:
    _enable_windows_dpi_awareness()
    root, dnd = _create_root()
    if not ffmpeg:
        root.withdraw()
        messagebox.showerror(
            APP_NAME,
            "ffmpeg was not found.\n\n"
            "If you run the script from source, install it with\n"
            "    pip install imageio-ffmpeg\n"
            "or put an ffmpeg executable next to the program.")
        root.destroy()
        return 1
    app = App(root, ffmpeg, dnd)
    if files:
        app.add_files(files)
    root.lift()
    root.focus_force()
    root.mainloop()
    return 0
