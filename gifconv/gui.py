"""The application window: a flat two-column UI drawn on a tkinter canvas.

Rounded shapes are rendered with Pillow (see draw.py); text, icons and input fields are
regular tkinter items on top, so they stay crisp at any display scaling.
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
from . import draw as D

HEX = D.hex_color
WIDTH_CHIPS = [("Original", None), ("480", 480), ("640", 640), ("800", 800)]
FPS_CHIPS = [("Source", None), ("10", 10.0), ("15", 15.0), ("24", 24.0)]
SEGMENTS = {"1": "Maximum", "2": "Balanced", "3": "Compact"}
CUSTOM = "custom"


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
    except OSError:
        pass


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
    """A borderless entry with placeholder text."""

    def __init__(self, master, placeholder: str, font, bg, fg, muted, justify="left"):
        super().__init__(master, relief="flat", bd=0, highlightthickness=0, font=font,
                         justify=justify, bg=HEX(bg), fg=HEX(fg), insertbackground=HEX(fg),
                         disabledbackground=HEX(bg), disabledforeground=HEX(muted),
                         selectbackground=HEX(D.PEACH), selectforeground=HEX(D.NAVY))
        self.placeholder = placeholder
        self.colors = (HEX(fg), HEX(muted))
        self._ph = False
        self.bind("<FocusIn>", self._focus_in)
        self.bind("<FocusOut>", lambda e: self._show_placeholder())
        self._show_placeholder()

    def _show_placeholder(self):
        if not super().get():
            self._ph = True
            self.configure(fg=self.colors[1])
            self.insert(0, self.placeholder)

    def _focus_in(self, _e=None):
        if self._ph:
            self.delete(0, "end")
            self.configure(fg=self.colors[0])
            self._ph = False

    def value(self) -> str:
        return "" if self._ph else super().get().strip()

    def set_value(self, text: str):
        state = self.cget("state")
        self.configure(state="normal")
        self.delete(0, "end")
        self._ph = False
        self.configure(fg=self.colors[0])
        if text:
            self.insert(0, text)
        else:
            self._show_placeholder()
        self.configure(state=state)


# ---------------------------------------------------------------- window

class App:
    BASE_W, BASE_H = 980, 660
    MIN_W, MIN_H = 860, 580
    SIDEBAR_W = 352

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
        self.width_choice: int | str = 0
        self.fps_choice: int | str = 0
        self.status = ""
        self.status_error = False
        self.overall = 0.0
        self.hot: str | None = None
        self.list_hot: str | None = None
        self.drop_hover = False
        self._cache: dict = {}
        self._size = (0, 0)
        self._list_dirty = True

        self.ws = root.tk.call("tk", "windowingsystem")  # win32 / aqua / x11
        self.S = 1.0 if self.ws == "aqua" else max(1.0, root.winfo_fpixels("1i") / 96.0)
        root.title(APP_NAME)
        root.configure(bg=HEX(D.BG))
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
        self._set_status(self._ready_text())
        self._initial_geometry()

    # --- setup
    def _set_icon(self):
        icon = core.resource_path("assets/icon.png")
        self._icon_pil = None
        if icon.is_file():
            try:
                self._icon_pil = Image.open(icon).convert("RGBA")
                if self.ws != "aqua":  # macOS takes the icon from the .app
                    self._icon_img = tk.PhotoImage(file=str(icon))
                    self.root.iconphoto(True, self._icon_img)
            except (OSError, tk.TclError):
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
        self.f_title = f(21, "bold")
        self.f_sub = f(12)
        self.f_h2 = f(16, "bold")
        self.f_body = f(13)
        self.f_body_b = f(13, "bold")
        self.f_small = f(12)
        self.f_small_b = f(12, "bold")
        self.f_label = f(11)
        self.f_badge = f(10, "bold")
        self.f_button = f(15, "bold")
        self.f_empty = f(18, "bold")

    def _px(self, v: float) -> int:
        return int(round(v * self.S))

    def _photo(self, key, make):
        """A cached PhotoImage; make() returns a PIL image."""
        img = self._cache.get(key)
        if img is None:
            if len(self._cache) > 600:
                self._cache.clear()
            img = self._cache[key] = ImageTk.PhotoImage(make())
        return img

    def _rr(self, w, h, r, color, bg):
        w, h = int(round(w)), int(round(h))
        return self._photo(("rr", w, h, int(r), color, bg), lambda: D.rounded(w, h, r, color, bg))

    def _build(self):
        c = self.canvas = tk.Canvas(self.root, bg=HEX(D.BG), highlightthickness=0, bd=0)
        c.pack(fill="both", expand=True)

        def img(tag="static"):
            return c.create_image(0, 0, anchor="nw", tags=(tag,))

        def text(font, fill, anchor="nw", tag="static", **kw):
            return c.create_text(0, 0, anchor=anchor, font=font, fill=HEX(fill), tags=(tag,), **kw)

        self.i_logo = img()
        self.t_title = text(self.f_title, D.NAVY, text=APP_NAME)
        self.t_sub = text(self.f_sub, D.MUTED,
                          text=f"Video to GIF in the best possible quality · {core.FORMATS_HINT}")
        self.i_files = img()
        self.i_side = img()
        self.t_files = text(self.f_h2, D.NAVY, text="Files")
        self.t_count = text(self.f_small, D.MUTED)
        self.t_clear = text(self.f_small_b, D.MUTED, anchor="ne", text="Clear list")
        self.l_footer = c.create_line(0, 0, 0, 0, fill=HEX(D.LINE), tags=("static",))
        self.i_folder = img()
        lw = max(1.5, 1.6 * self.S)
        self.i_folder_icon = c.create_line(0, 0, 0, 0, width=lw, fill=HEX(D.NAVY),
                                           joinstyle="round", capstyle="round", tags=("static",))
        self.t_save_label = text(self.f_label, D.MUTED, anchor="sw", text="Save GIFs to")
        self.t_save_value = text(self.f_body, D.INK, anchor="nw")
        self.t_reset = text(self.f_small_b, D.MUTED, anchor="e", text="Reset")

        self.t_q_label = text(self.f_label, D.SB_MUTED, text="Quality")
        self.i_seg_track = img()
        self.i_seg_pill = img()
        self.t_segs = [text(self.f_small_b, D.SB_TEXT, anchor="center", text=t)
                       for t in SEGMENTS.values()]
        self.t_hint = text(self.f_label, D.SB_MUTED)
        self.t_w_label = text(self.f_label, D.SB_MUTED, text="Width, px")
        self.t_f_label = text(self.f_label, D.SB_MUTED, text="Frame rate, fps")
        self.t_start_label = text(self.f_label, D.SB_MUTED, text="Start")
        self.t_dur_label = text(self.f_label, D.SB_MUTED, text="Duration, s")
        self.i_start = img()
        self.i_dur = img()
        self.t_status = text(self.f_small, D.SB_MUTED, anchor="sw")
        self.i_prog = img()
        self.i_go = img()
        self.t_go = text(self.f_button, D.NAVY, anchor="center", text="Convert")

        # file list: a separate canvas so it can scroll
        self.list = tk.Canvas(c, bg="#ffffff", highlightthickness=0, bd=0, takefocus=1)
        self.list_win = c.create_window(0, 0, anchor="nw", window=self.list)
        self.list.bind("<Configure>", lambda e: self._mark_list())
        self.list.bind("<Motion>", self._list_motion)
        self.list.bind("<Leave>", lambda e: self._set_list_hot(None))
        self.list.bind("<Button-1>", self._list_click)
        self.list.bind("<Double-1>", self._list_double)
        self.list.bind("<Delete>", lambda e: self.remove_selected())
        self.list.bind("<BackSpace>", lambda e: self.remove_selected())
        if self.ws == "x11":
            self.list.bind("<Button-4>", lambda e: self._scroll(-1))
            self.list.bind("<Button-5>", lambda e: self._scroll(1))
        else:
            self.list.bind("<MouseWheel>", self._wheel)

        # input fields on the navy panel
        on_navy = dict(bg=D.SB_FIELD, fg=D.SB_TEXT, muted=D.SB_MUTED)
        self.e_start = PlaceholderEntry(c, "0:00", self.f_body, **on_navy)
        self.e_dur = PlaceholderEntry(c, "full length", self.f_body, **on_navy)
        on_peach = dict(bg=D.PEACH, fg=D.NAVY, muted=(150, 110, 100), justify="center")
        self.e_wcustom = PlaceholderEntry(c, "px", self.f_small_b, **on_peach)
        self.e_fcustom = PlaceholderEntry(c, "fps", self.f_small_b, **on_peach)
        self.entries = [self.e_start, self.e_dur, self.e_wcustom, self.e_fcustom]
        self.w_start = c.create_window(0, 0, anchor="w", window=self.e_start)
        self.w_dur = c.create_window(0, 0, anchor="w", window=self.e_dur)
        self.w_wcustom = c.create_window(0, 0, anchor="center", window=self.e_wcustom,
                                         state="hidden")
        self.w_fcustom = c.create_window(0, 0, anchor="center", window=self.e_fcustom,
                                         state="hidden")
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
    def _chip_w(self, label):
        return self.f_small_b.measure(label) + self._px(22)

    def _flow(self, labels, x0, x1, y):
        """Lay chips out left to right, wrapping to a new row when needed."""
        p = self._px
        h, gap = p(32), p(6)
        rects, x = [], x0
        for label in labels:
            w = self._chip_w(label)
            if x > x0 and x + w > x1:
                x, y = x0, y + h + gap
            rects.append((x, y, x + w, y + h))
            x += w + gap
        return rects, y + h

    def _layout(self, W: int, H: int) -> dict:
        p = self._px
        M, CT = p(24), p(84)
        R = {"logo": (M, p(20), p(40)), "content_top": CT}
        sx0, sx1 = W - M - p(self.SIDEBAR_W), W - M
        R["side"] = (sx0, CT, sx1, H - M)
        R["files"] = files = (M, CT, sx0 - p(16), H - M)
        fx0, fy0, fx1, fy1 = files
        R["footer_y"] = fy1 - p(66)
        R["folder"] = (fx0 + p(20), fy1 - p(51), fx0 + p(58), fy1 - p(13))
        R["list"] = (fx0 + p(12), fy0 + p(58), fx1 - p(12), R["footer_y"] - p(6))

        ix0, ix1 = sx0 + p(24), sx1 - p(24)
        y = CT + p(24)
        R["q_label"] = (ix0, y)
        y += p(22)
        R["seg"] = (ix0, y, ix1, y + p(42))
        y += p(42) + p(10)
        R["hint"] = (ix0, y)
        y += p(18) + p(20)
        R["w_label"] = (ix0, y)
        y += p(22)
        labels_w = [lab for lab, _ in WIDTH_CHIPS] + ["Custom"]
        R["w_chips"], y = self._flow(labels_w, ix0, ix1, y)
        y += p(20)
        R["f_label"] = (ix0, y)
        y += p(22)
        labels_f = [lab for lab, _ in FPS_CHIPS] + ["Custom"]
        R["f_chips"], y = self._flow(labels_f, ix0, ix1, y)
        y += p(20)
        half = (ix1 - ix0 - p(12)) / 2
        R["start_label"], R["dur_label"] = (ix0, y), (ix1 - half, y)
        y += p(22)
        R["start"] = (ix0, y, ix0 + half, y + p(38))
        R["dur"] = (ix1 - half, y, ix1, y + p(38))
        sy1 = H - M
        R["go"] = (ix0, sy1 - p(24) - p(52), ix1, sy1 - p(24))
        R["prog"] = (ix0, R["go"][1] - p(18) - p(6), ix1, R["go"][1] - p(18))
        R["status"] = (ix0, R["prog"][1] - p(10))
        return R

    def _on_configure(self, event):
        size = (event.width, event.height)
        if size[0] < 200 or size[1] < 200 or size == self._size:
            return
        self._size = size
        self.R = self._layout(*size)
        self._draw_static()
        self._refresh()
        self._mark_list()

    def _draw_static(self):
        """Cards, header and positions — everything that depends only on the window size."""
        c, R, p = self.canvas, self.R, self._px
        lx, ly, ls = R["logo"]
        if self._icon_pil is not None:
            logo = self._photo(("logo", ls), lambda: self._logo_image(ls))
            c.itemconfigure(self.i_logo, image=logo)
            c.coords(self.i_logo, lx, ly)
        tx = lx + ls + p(12)
        c.coords(self.t_title, tx, p(19))
        c.coords(self.t_sub, tx, p(48))

        fx0, fy0, fx1, fy1 = R["files"]
        c.itemconfigure(self.i_files, image=self._rr(fx1 - fx0, fy1 - fy0, p(24), D.CARD, D.BG))
        c.coords(self.i_files, fx0, fy0)
        sx0, sy0, sx1, sy1 = R["side"]
        c.itemconfigure(self.i_side, image=self._rr(sx1 - sx0, sy1 - sy0, p(24), D.NAVY, D.BG))
        c.coords(self.i_side, sx0, sy0)

        c.coords(self.t_files, fx0 + p(24), fy0 + p(20))
        c.coords(self.t_count, fx0 + p(24) + self.f_h2.measure("Files") + p(10), fy0 + p(25))
        c.coords(self.t_clear, fx1 - p(24), fy0 + p(24))
        lx0, ly0, lx1, ly1 = R["list"]
        c.coords(self.list_win, lx0, ly0)
        c.itemconfigure(self.list_win, width=lx1 - lx0, height=max(10, ly1 - ly0))

        fy = R["footer_y"]
        c.coords(self.l_footer, fx0 + p(24), fy, fx1 - p(24), fy)
        b = R["folder"]
        cy = (b[1] + b[3]) / 2
        c.coords(self.t_save_label, b[2] + p(12), cy - p(1))
        c.coords(self.t_save_value, b[2] + p(12), cy)
        c.coords(self.t_reset, fx1 - p(24), cy)
        cx = (b[0] + b[2]) / 2
        w2, h2 = p(8.5), p(6.5)
        c.coords(self.i_folder_icon,
                 cx - w2, cy - h2 + p(1.5), cx - w2, cy + h2, cx + w2, cy + h2, cx + w2, cy - h2 + p(3.5),
                 cx - p(0.5), cy - h2 + p(3.5), cx - p(3), cy - h2 + p(0.5), cx - w2 + p(1), cy - h2 + p(0.5),
                 cx - w2, cy - h2 + p(1.5))

        c.coords(self.t_q_label, *R["q_label"])
        s = R["seg"]
        c.itemconfigure(self.i_seg_track, image=self._rr(s[2] - s[0], s[3] - s[1], (s[3] - s[1]) / 2,
                                                         D.SB_FIELD, D.NAVY))
        c.coords(self.i_seg_track, s[0], s[1])
        c.coords(self.t_hint, *R["hint"])
        c.coords(self.t_w_label, *R["w_label"])
        c.coords(self.t_f_label, *R["f_label"])
        c.coords(self.t_start_label, *R["start_label"])
        c.coords(self.t_dur_label, *R["dur_label"])
        for item, win, box in ((self.i_start, self.w_start, R["start"]),
                               (self.i_dur, self.w_dur, R["dur"])):
            c.itemconfigure(item, image=self._rr(box[2] - box[0], box[3] - box[1], p(12),
                                                 D.SB_FIELD, D.NAVY))
            c.coords(item, box[0], box[1])
            c.coords(win, box[0] + p(14), (box[1] + box[3]) / 2)
            c.itemconfigure(win, width=box[2] - box[0] - p(28),
                            height=self.f_body.metrics("linespace") + p(2))
        c.coords(self.t_status, *R["status"])
        g = R["go"]
        c.coords(self.t_go, (g[0] + g[2]) / 2, (g[1] + g[3]) / 2)

    def _logo_image(self, size):
        icon = self._icon_pil.resize((size, size), Image.LANCZOS)
        out = Image.new("RGB", (size, size), D.BG)
        out.paste(icon, (0, 0), icon)
        return out

    # --- state-dependent drawing
    def _refresh(self):
        """Redraw everything that depends on state (cheap: cached images, a few items)."""
        if not hasattr(self, "R"):
            return
        c, R, p = self.canvas, self.R, self._px
        busy = self._busy()
        c.delete("dyn")

        # files header and footer
        n = len(self.order)
        c.itemconfigure(self.t_count, text=f"{n} video{'s' if n != 1 else ''}" if n else "")
        c.itemconfigure(self.t_clear, state="normal" if n and not busy else "hidden",
                        fill=HEX(D.NAVY if self.hot == "clear" else D.MUTED))
        b = R["folder"]
        color = D.ROW_SELECTED if self.hot == "folder" else D.BG
        c.itemconfigure(self.i_folder, image=self._rr(b[2] - b[0], b[3] - b[1], p(10), color, D.CARD))
        c.coords(self.i_folder, b[0], b[1])
        right = R["files"][2] - p(24)
        if self.out_dir:
            right -= self.f_small_b.measure("Reset") + p(16)
            value = str(self.out_dir)
        else:
            value = f"a “{core.OUTPUT_DIR_NAME}” folder next to each video"
        c.itemconfigure(self.t_save_value,
                        text=_fit(value, self.f_body, right - b[2] - p(12), middle=True))
        c.itemconfigure(self.t_reset, state="normal" if self.out_dir and not busy else "hidden",
                        fill=HEX(D.NAVY if self.hot == "reset" else D.MUTED))

        # quality segments
        s = R["seg"]
        segw = (s[2] - s[0] - p(8)) / 3
        i = int(self.mode) - 1
        x0 = s[0] + p(4) + i * segw
        c.itemconfigure(self.i_seg_pill, image=self._rr(segw, s[3] - s[1] - p(8),
                                                        (s[3] - s[1] - p(8)) / 2, D.PEACH, D.SB_FIELD))
        c.coords(self.i_seg_pill, x0, s[1] + p(4))
        for k, t in enumerate(self.t_segs):
            c.coords(t, s[0] + p(4) + (k + 0.5) * segw, (s[1] + s[3]) / 2)
            active = k == i
            hover = self.hot == f"seg{k}" and not active
            c.itemconfigure(t, fill=HEX(D.NAVY if active else (D.SB_TEXT if hover or not busy
                                                                else D.SB_MUTED)))
        c.itemconfigure(self.t_hint, text=core.MODES[self.mode].hint.capitalize())

        # chips
        self._draw_chips("w", WIDTH_CHIPS, self.width_choice, R["w_chips"], self.w_wcustom,
                         self.e_wcustom, busy)
        self._draw_chips("f", FPS_CHIPS, self.fps_choice, R["f_chips"], self.w_fcustom,
                         self.e_fcustom, busy)

        # status, progress, convert button
        c.itemconfigure(self.t_status, fill=HEX(D.SB_ERROR if self.status_error else D.SB_MUTED),
                        text=_fit(self.status, self.f_small, R["go"][2] - R["go"][0]))
        pr = R["prog"]
        prog = self._photo(("prog", pr[2] - pr[0], pr[3] - pr[1], round(self.overall, 3)),
                           lambda: D.progress_pill(pr[2] - pr[0], pr[3] - pr[1], self.overall,
                                                   D.SB_FIELD, D.PEACH, D.NAVY))
        c.itemconfigure(self.i_prog, image=prog)
        c.coords(self.i_prog, pr[0], pr[1])
        g = R["go"]
        hover = self.hot == "go"
        if busy:
            bg, fg, label = ((236, 240, 249) if hover else D.CARD), D.NAVY, "Stop"
        elif not self.order:
            bg, fg, label = D.SB_CHIP, D.SB_MUTED, "Convert"
        else:
            all_done = all(self.items[i].state == "done" for i in self.order)
            bg, fg = (D.PEACH_HOVER if hover else D.PEACH), D.NAVY
            label = "Convert again" if all_done else "Convert"
        c.itemconfigure(self.i_go, image=self._rr(g[2] - g[0], g[3] - g[1], (g[3] - g[1]) / 2, bg, D.NAVY))
        c.coords(self.i_go, g[0], g[1])
        c.itemconfigure(self.t_go, text=label, fill=HEX(fg))

        for e in self.entries:
            e.configure(state="disabled" if busy else "normal")
        c.tag_raise("dyn")
        c.tag_raise(self.t_go)

    def _draw_chips(self, group, chips, choice, rects, custom_win, custom_entry, busy):
        c, p = self.canvas, self._px
        labels = [lab for lab, _ in chips] + ["Custom"]
        for k, (label, box) in enumerate(zip(labels, rects)):
            key = CUSTOM if k == len(chips) else k
            active = choice == key
            hover = self.hot == f"{group}chip{k}" and not busy
            bg = D.PEACH if active else (D.SB_CHIP_HOVER if hover else D.SB_CHIP)
            w, h = box[2] - box[0], box[3] - box[1]
            c.create_image(box[0], box[1], anchor="nw", tags=("dyn",),
                           image=self._rr(w, h, h / 2, bg, D.NAVY))
            cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
            if key == CUSTOM and active:
                c.coords(custom_win, cx, cy)
                c.itemconfigure(custom_win, state="normal", width=w - p(16),
                                height=self.f_small_b.metrics("linespace") + p(2))
            else:
                fg = D.NAVY if active else (D.SB_TEXT if not busy else D.SB_MUTED)
                c.create_text(cx, cy, text=label, font=self.f_small_b, fill=HEX(fg), tags=("dyn",))
        if choice != CUSTOM:
            c.itemconfigure(custom_win, state="hidden")

    def _set_status(self, text: str, error: bool = False):
        self.status, self.status_error = text, error
        self._refresh()

    # --- hover and clicks on the main canvas
    def _hotspots(self):
        if not hasattr(self, "R"):
            return []
        R, p = self.R, self._px
        spots = [("go", R["go"])]
        if self._busy():
            return spots
        s = R["seg"]
        segw = (s[2] - s[0]) / 3
        spots += [(f"seg{k}", (s[0] + k * segw, s[1], s[0] + (k + 1) * segw, s[3])) for k in range(3)]
        spots += [(f"wchip{k}", r) for k, r in enumerate(R["w_chips"])]
        spots += [(f"fchip{k}", r) for k, r in enumerate(R["f_chips"])]
        f = R["files"]
        b = R["folder"]
        spots.append(("folder", (b[0], b[1], f[2] - p(24) - (self.f_small_b.measure("Reset") + p(16)
                                                              if self.out_dir else 0), b[3])))
        cy = (b[1] + b[3]) / 2
        if self.out_dir:
            w = self.f_small_b.measure("Reset")
            spots.append(("reset", (f[2] - p(24) - w - p(6), cy - p(12), f[2] - p(18), cy + p(12))))
        if self.order:
            w = self.f_small_b.measure("Clear list")
            spots.append(("clear", (f[2] - p(24) - w - p(6), f[1] + p(16), f[2] - p(18), f[1] + p(44))))
        return spots

    def _hit(self, x, y):
        for name, (x0, y0, x1, y1) in reversed(self._hotspots()):
            if x0 <= x <= x1 and y0 <= y <= y1:
                return name
        return None

    def _motion(self, event):
        self._set_hot(self._hit(event.x, event.y))

    def _set_hot(self, name):
        if name == self.hot:
            return
        self.hot = name
        clickable = name and not (name == "go" and not self.order and not self._busy())
        self.canvas.configure(cursor="hand2" if clickable else "")
        self._refresh()

    def _click(self, event):
        name = self._hit(event.x, event.y)
        if not name or not (name.startswith(("wchip", "fchip"))):
            self.canvas.focus_set()
        if name == "go":
            self.toggle_run()
        elif name and name.startswith("seg"):
            self.mode = str(int(name[3:]) + 1)
            self._refresh()
        elif name and name.startswith("wchip"):
            self._choose_chip("w", int(name[5:]))
        elif name and name.startswith("fchip"):
            self._choose_chip("f", int(name[5:]))
        elif name == "folder":
            self.choose_out_dir()
        elif name == "reset":
            self.out_dir = None
            self._remember()
            self._refresh()
        elif name == "clear":
            self.clear()

    def _choose_chip(self, group, k):
        chips = WIDTH_CHIPS if group == "w" else FPS_CHIPS
        entry = self.e_wcustom if group == "w" else self.e_fcustom
        choice = CUSTOM if k == len(chips) else k
        if group == "w":
            self.width_choice = choice
        else:
            self.fps_choice = choice
        self._refresh()
        if choice == CUSTOM:
            entry.focus_set()
        else:
            self.canvas.focus_set()

    # --- the file list
    def _mark_list(self):
        self._list_dirty = True

    def _row_h(self):
        return self._px(60)

    def _drop_rect(self, w, h):
        """The drop zone inside the list canvas (canvas coordinates)."""
        p = self._px
        if not self.order:
            return (p(4), p(4), w - p(4), max(h - p(4), p(120)))
        y = len(self.order) * self._row_h() + p(10)
        return (p(4), y, w - p(4), y + p(62))

    def _draw_list(self):
        self._list_dirty = False
        L, p = self.list, self._px
        L.delete("all")
        w, h = L.winfo_width(), L.winfo_height()
        if w < 50:
            return
        RH = self._row_h()
        for i, iid in enumerate(self.order):
            self._draw_row(i, iid, w, RH)
        self._draw_drop(w, h)
        dz = self._drop_rect(w, h)
        L.configure(scrollregion=(0, 0, w, max(h, dz[3] + p(4))))
        self._draw_scrollbar()

    def _draw_row(self, i, iid, w, RH):
        L, p = self.list, self._px
        it = self.items[iid]
        y = i * RH
        row_bg = D.CARD
        if iid in self.selected:
            row_bg = D.ROW_SELECTED
        elif self.list_hot in (iid, f"pill:{iid}"):
            row_bg = D.ROW_HOVER
        if row_bg != D.CARD:
            L.create_image(p(2), y + p(3), anchor="nw",
                           image=self._rr(w - p(4), RH - p(6), p(14), row_bg, D.CARD))
        bx, by = p(12), y + (RH - p(40)) / 2
        L.create_image(bx, by, anchor="nw", image=self._rr(p(40), p(40), p(12), D.BG, row_bg))
        ext = it.path.suffix.lstrip(".").upper()[:4] or "?"
        L.create_text(bx + p(20), by + p(20), text=ext, font=self.f_badge, fill=HEX(D.NAVY))

        # status pill on the right
        kind = it.state
        pill_hot = self.list_hot == f"pill:{iid}"
        if kind == "done" and it.result:
            label = "Show in folder" if pill_hot else f"Done · {core.format_size(it.result.size)}"
            bg, fg = (D.SB_CHIP if pill_hot else D.NAVY), D.SB_TEXT
        elif kind == "work":
            label, bg, fg = f"{int(it.progress * 100)}%", None, D.INK
        elif kind == "error":
            label, bg, fg = "Error", D.RED_SOFT, D.RED
        else:
            label = "Canceled" if kind == "cancelled" else "Waiting"
            bg, fg = D.BG, D.MUTED
        pw = max(p(84), self.f_small_b.measure(label) + p(28))
        ph = p(28)
        px, py = w - p(14) - pw, y + (RH - ph) / 2
        if bg is None:
            frac = round(it.progress, 2)
            img = self._photo(("pp", pw, ph, frac, row_bg),
                              lambda: D.progress_pill(pw, ph, frac, D.PEACH_SOFT, D.PEACH, row_bg))
        else:
            img = self._rr(pw, ph, ph / 2, bg, row_bg)
        L.create_image(px, py, anchor="nw", image=img)
        L.create_text(px + pw / 2, py + ph / 2, text=label, font=self.f_small_b, fill=HEX(fg))

        tx = p(66)
        avail = px - tx - p(16)
        L.create_text(tx, y + p(12), anchor="nw", font=self.f_body_b, fill=HEX(D.INK),
                      text=_fit(it.path.name, self.f_body_b, avail, middle=True))
        if it.state == "error" and it.error:
            meta, color = it.error.splitlines()[-1], D.RED
        elif it.info is None:
            meta, color = "Reading…", D.MUTED
        elif it.info.has_video:
            meta, color = it.info.describe(), D.MUTED
        else:
            meta, color = "Not a video or unsupported format", D.RED
        L.create_text(tx, y + p(33), anchor="nw", font=self.f_label, fill=HEX(color),
                      text=_fit(meta, self.f_label, avail))
        if i < len(self.order) - 1:
            L.create_line(tx, y + RH - 1, w - p(14), y + RH - 1, fill=HEX(D.LINE))

    def _draw_drop(self, w, h):
        L, p = self.list, self._px
        x0, y0, x1, y1 = self._drop_rect(w, h)
        hot = self.drop_hover or self.list_hot == "drop"
        border = D.NAVY if hot else (196, 200, 210)
        fill = (246, 247, 251) if hot else (251, 251, 252)
        key = ("dash", x1 - x0, y1 - y0, border, fill)
        img = self._photo(key, lambda: D.dashed_rounded(x1 - x0, y1 - y0, p(16), border, D.CARD,
                                                        fill=fill, width=1.4 * self.S,
                                                        dash=7 * self.S, gap=5 * self.S))
        L.create_image(x0, y0, anchor="nw", image=img)
        lw = max(1.5, 1.8 * self.S)
        if not self.order:
            cx, cy = (x0 + x1) / 2, (y0 + y1) / 2 - p(26)
            r = p(30)
            L.create_image(cx - r, cy - r, anchor="nw",
                           image=self._rr(2 * r, 2 * r, r, D.PEACH_SOFT if not hot else D.PEACH, fill))
            L.create_line(cx, cy + p(9), cx, cy - p(9), width=lw, fill=HEX(D.NAVY), capstyle="round")
            L.create_line(cx - p(7), cy - p(2), cx, cy - p(9), cx + p(7), cy - p(2), width=lw,
                          fill=HEX(D.NAVY), capstyle="round", joinstyle="round")
            title = "Release to add" if self.drop_hover else \
                ("Drop videos here" if self.dnd else "Click to add videos")
            L.create_text(cx, cy + r + p(24), text=title, font=self.f_empty, fill=HEX(D.NAVY))
            sub = f"or click to browse · {core.FORMATS_HINT}" if self.dnd else core.FORMATS_HINT
            L.create_text(cx, cy + r + p(50), text=sub, font=self.f_small, fill=HEX(D.MUTED))
        else:
            cy = (y0 + y1) / 2
            r = p(14)
            ix = x0 + p(16)
            L.create_image(ix, cy - r, anchor="nw",
                           image=self._rr(2 * r, 2 * r, r, D.PEACH_SOFT if not hot else D.PEACH, fill))
            cx = ix + r
            L.create_line(cx - p(5), cy, cx + p(5), cy, width=lw, fill=HEX(D.NAVY), capstyle="round")
            L.create_line(cx, cy - p(5), cx, cy + p(5), width=lw, fill=HEX(D.NAVY), capstyle="round")
            title = "Release to add" if self.drop_hover else "Add more videos"
            tx = ix + 2 * r + p(12)
            L.create_text(tx, cy, anchor="w", text=title, font=self.f_body_b, fill=HEX(D.NAVY))
            if not self.drop_hover:
                hint = "drop here or click to browse" if self.dnd else "click to browse"
                L.create_text(tx + self.f_body_b.measure(title) + p(10), cy, anchor="w",
                              text=hint, font=self.f_small, fill=HEX(D.MUTED))

    def _draw_scrollbar(self):
        L, p = self.list, self._px
        L.delete("sb")
        f0, f1 = L.yview()
        if f1 - f0 >= 0.999:
            return
        h = L.winfo_height()
        top = L.canvasy(0)
        x = L.winfo_width() - p(3)
        L.create_line(x, top + p(6) + f0 * (h - p(12)), x, top + p(6) + f1 * (h - p(12)),
                      width=p(4), fill="#c4c8d2", capstyle="round", tags=("sb",))

    def _scroll(self, units):
        self.list.yview_scroll(units, "units")
        self._draw_scrollbar()

    def _wheel(self, event):
        d = event.delta / 120 if self.ws == "win32" else event.delta
        if d:
            self._scroll(-int(d) if abs(d) >= 1 else (-1 if d > 0 else 1))

    def _list_target(self, event):
        """What is under the mouse in the list: a row id, 'pill:<id>', 'drop' or None."""
        p = self._px
        x, y = self.list.canvasx(event.x), self.list.canvasy(event.y)
        w, h = self.list.winfo_width(), self.list.winfo_height()
        x0, y0, x1, y1 = self._drop_rect(w, h)
        if x0 <= x <= x1 and y0 <= y <= y1:
            return "drop"
        i = int(y // self._row_h())
        if not (0 <= i < len(self.order)):
            return None
        iid = self.order[i]
        it = self.items[iid]
        if it.state == "done" and it.result:
            label = f"Done · {core.format_size(it.result.size)}"
            pw = max(p(84), self.f_small_b.measure("Show in folder") + p(28),
                     self.f_small_b.measure(label) + p(28))
            if x >= w - p(14) - pw:
                return f"pill:{iid}"
        return iid

    def _set_list_hot(self, target):
        if target != self.list_hot:
            self.list_hot = target
            self._mark_list()
            self._draw_list()

    def _list_motion(self, event):
        target = self._list_target(event)
        self._set_list_hot(target)
        hand = target == "drop" or (target or "").startswith("pill:")
        self.list.configure(cursor="hand2" if hand else "")

    def _list_click(self, event):
        self.list.focus_set()
        target = self._list_target(event)
        if target == "drop":
            self.choose_files()
            return
        if target and target.startswith("pill:"):
            it = self.items.get(target[5:])
            if it and it.result and it.result.path.exists():
                _reveal(it.result.path)
            return
        ctrl = bool(event.state & 0x0004) or (self.ws == "aqua" and bool(event.state & 0x0008))
        if target is None:
            self.selected.clear()
        elif ctrl:
            self.selected ^= {target}
        else:
            self.selected = {target}
        if len(self.selected) == 1 and not self._busy():
            it = self.items[next(iter(self.selected))]
            if it.state == "error":
                self._set_status(f"{it.path.name}: {it.error.splitlines()[-1]}", error=True)
        self._mark_list()

    def _list_double(self, event):
        target = self._list_target(event)
        it = self.items.get(target) if target else None
        if it and it.result and it.result.path.exists():
            _open_path(it.result.path)

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
        self._refresh()
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
            self._remember()
            self._refresh()

    # --- settings
    def _load_settings(self):
        c = self.cfg
        if c.get("mode") in core.MODES:
            self.mode = c["mode"]
        width, fps = c.get("width"), c.get("fps")
        values_w = [v for _, v in WIDTH_CHIPS]
        values_f = [v for _, v in FPS_CHIPS]
        if width in values_w:
            self.width_choice = values_w.index(width)
        elif width:
            self.width_choice = CUSTOM
            self.e_wcustom.set_value(str(width))
        if fps in values_f:
            self.fps_choice = values_f.index(fps)
        elif fps:
            self.fps_choice = CUSTOM
            self.e_fcustom.set_value(core.format_number(float(fps)))
        if c.get("out_dir") and Path(c["out_dir"]).is_dir():
            self.out_dir = Path(c["out_dir"])

    def _remember(self, s: core.Settings | None = None):
        if s is not None:
            self.cfg.update(mode=s.mode, width=s.width, fps=s.fps)
        self.cfg["out_dir"] = str(self.out_dir) if self.out_dir else None
        config.save(self.cfg)

    def _read_settings(self) -> core.Settings | None:
        def bad(msg, entry=None):
            self._set_status(msg, error=True)
            if entry is not None:
                entry.focus_set()
            return None

        width = None
        if self.width_choice == CUSTOM:
            raw = self.e_wcustom.value().lower().replace("px", "").strip()
            try:
                width = int(float(raw.replace(",", ".")))
                if width <= 0:
                    raise ValueError
            except ValueError:
                return bad("Enter a custom width in pixels, e.g. 720", self.e_wcustom)
        else:
            width = WIDTH_CHIPS[self.width_choice][1]

        fps = None
        if self.fps_choice == CUSTOM:
            raw = self.e_fcustom.value().lower().replace("fps", "").strip()
            try:
                fps = float(raw.replace(",", "."))
                if not 0 < fps < float("inf"):
                    raise ValueError
            except ValueError:
                return bad("Enter a custom frame rate, e.g. 12", self.e_fcustom)
            if fps > core.MAX_GIF_FPS:
                fps = float(core.MAX_GIF_FPS)
                self.e_fcustom.set_value(str(core.MAX_GIF_FPS))
        else:
            fps = FPS_CHIPS[self.fps_choice][1]

        try:
            start = core.parse_time(self.e_start.value())
        except ValueError:
            return bad("Start: seconds (12.5) or minutes:seconds (1:30)", self.e_start)
        try:
            duration = core.parse_time(self.e_dur.value())
        except ValueError:
            return bad("Duration: a number of seconds, e.g. 5", self.e_dur)
        if duration == 0:
            return bad("Duration must be greater than zero", self.e_dur)
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
            self._set_status("Add videos first — drop them into the list")
            return
        s = self._read_settings()
        if s is None:
            return
        targets = [iid for iid in self.order if self.items[iid].state != "done"] or list(self.order)
        self._remember(s)
        self.converter.reset()
        for iid in targets:
            it = self.items[iid]
            it.state, it.progress, it.error = "wait", 0.0, ""
        self.overall = 0.0
        self.worker = threading.Thread(target=self._work, args=(targets, s), daemon=True)
        self.worker.start()
        self._set_status(f"Starting · {len(targets)} video{'s' if len(targets) != 1 else ''}")
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
        changed = False
        try:
            while True:
                changed |= bool(self._handle(*self.events.get_nowait()))
        except queue.Empty:
            pass
        if changed:
            self._refresh()
        if self._list_dirty:
            self._draw_list()
        self.root.after(100, self._poll)

    def _handle(self, kind, *a):
        """Apply an event from a worker thread. Returns True if the sidebar needs a redraw."""
        it = self.items.get(a[0]) if a and isinstance(a[0], str) else None
        if kind == "probed":
            if it:
                it.info = a[1]
                self._mark_list()
            return False
        if kind == "start":
            if it:
                it.state, it.progress = "work", 0.0
                self._scroll_to(a[0])
        elif kind == "progress":
            _, idx, n, frac, stage = a
            self.overall = (idx + frac) / n
            if it and it.state == "work":
                it.progress = frac
                self.status = f"{idx + 1}/{n} · {it.path.name} · {stage} · {int(frac * 100)}%"
                self.status_error = False
        elif kind == "done":
            if it:
                it.state, it.result, it.error = "done", a[1], ""
            self.last_result = a[1]
        elif kind == "error":
            if it:
                it.state, it.error = "error", a[1]
        elif kind == "cancelled":
            if it:
                it.state = "cancelled"
        elif kind == "finished":
            done, failed, size, cancelled = a
            if cancelled:
                self.status = f"Stopped · {done} GIF{'s' if done != 1 else ''} done"
            else:
                self.overall = 1.0
                self.status = f"Done · {done} GIF{'s' if done != 1 else ''}, {core.format_size(size)}"
                if failed:
                    self.status += f" · {failed} failed"
            self.status_error = bool(failed) and not cancelled
            self.root.after(150, self._refresh)  # the worker thread is gone by then
        self._mark_list()
        return True

    def _scroll_to(self, iid):
        if iid not in self.order:
            return
        RH = self._row_h()
        i = self.order.index(iid)
        total = len(self.order) * RH + self._px(80)
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
