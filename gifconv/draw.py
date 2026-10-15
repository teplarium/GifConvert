"""Flat UI shapes rendered with Pillow (tkinter's canvas can't draw anti-aliased rounded shapes).

Every shape is rendered onto its known solid background color, so the images need no alpha.
"""

from __future__ import annotations

import bisect
import math

from PIL import Image, ImageDraw

# ---- brand palette
BG = (240, 240, 240)          # #f0f0f0 — window background
NAVY = (42, 65, 121)          # #2a4179 — UI color
PEACH = (254, 181, 139)       # #feb58b — accent and logo
CARD = (255, 255, 255)

INK = (27, 35, 56)            # body text on light backgrounds
MUTED = (134, 140, 156)
LINE = (234, 234, 237)
ROW_HOVER = (247, 247, 249)
ROW_SELECTED = (236, 240, 249)
PEACH_SOFT = (255, 236, 224)
PEACH_HOVER = (253, 164, 116)
RED = (206, 64, 64)
RED_SOFT = (252, 234, 234)

# on the navy panel
SB_TEXT = (255, 255, 255)
SB_MUTED = (170, 182, 212)
SB_CHIP = (59, 82, 138)
SB_CHIP_HOVER = (72, 97, 155)
SB_FIELD = (33, 53, 102)
SB_ERROR = (255, 184, 176)


def hex_color(rgb) -> str:
    return "#%02x%02x%02x" % tuple(rgb[:3])


def _mask(w: int, h: int, r: float, ss: int = 3) -> Image.Image:
    big = Image.new("L", (w * ss, h * ss), 0)
    ImageDraw.Draw(big).rounded_rectangle((0, 0, w * ss - 1, h * ss - 1),
                                          radius=max(0, r * ss), fill=255)
    return big.resize((w, h), Image.LANCZOS)


def rounded(w, h, r, color, bg) -> Image.Image:
    """A solid rounded rectangle on a solid background."""
    w, h = max(1, int(round(w))), max(1, int(round(h)))
    img = Image.new("RGB", (w, h), tuple(bg))
    img.paste(Image.new("RGB", (w, h), tuple(color)), (0, 0), _mask(w, h, min(r, w / 2, h / 2)))
    return img


def progress_pill(w, h, frac, track, fill, bg) -> Image.Image:
    """A pill-shaped progress bar."""
    img = rounded(w, h, h / 2, track, bg)
    fw = int(round(img.width * max(0.0, min(1.0, frac))))
    if fw > 0:
        fw = max(fw, img.height)
        part = rounded(fw, img.height, img.height / 2, fill, track)
        mask = _mask(fw, img.height, img.height / 2)
        img.paste(part, (0, 0), mask)
    return img


def dashed_rounded(w, h, r, color, bg, fill=None, width=1.5, dash=7.0, gap=5.0) -> Image.Image:
    """A rounded rectangle with an evenly dashed outline (the drop zone)."""
    w, h = max(2, int(round(w))), max(2, int(round(h)))
    ss = 3
    W, H = w * ss, h * ss
    lw = max(1, int(round(width * ss)))
    img = Image.new("RGB", (W, H), tuple(bg))
    d = ImageDraw.Draw(img)
    R = min(r, w / 2, h / 2) * ss
    if fill is not None:
        d.rounded_rectangle((0, 0, W - 1, H - 1), radius=R, fill=tuple(fill))
    inset = lw / 2 + 1
    pts = _rr_path(inset, inset, W - 1 - inset, H - 1 - inset, max(0.0, R - inset))
    cum = [0.0]
    for a, b in zip(pts, pts[1:]):
        cum.append(cum[-1] + math.hypot(b[0] - a[0], b[1] - a[1]))
    total = cum[-1]
    count = max(1, round(total / ((dash + gap) * ss)))
    period = total / count
    dash_len = period * dash / (dash + gap)

    def point_at(dist):
        i = min(len(cum) - 2, max(0, bisect.bisect_right(cum, dist) - 1))
        seg = cum[i + 1] - cum[i] or 1.0
        t = (dist - cum[i]) / seg
        (ax, ay), (bx, by) = pts[i], pts[i + 1]
        return ax + (bx - ax) * t, ay + (by - ay) * t

    for k in range(count):
        a, b = k * period, k * period + dash_len
        line = [point_at(a)]
        i = bisect.bisect_right(cum, a)
        while i < len(cum) and cum[i] < b:
            line.append(pts[i])
            i += 1
        line.append(point_at(b))
        d.line(line, fill=tuple(color), width=lw, joint="curve")
    return img.resize((w, h), Image.LANCZOS)


def _rr_path(x0, y0, x1, y1, r, n=16):
    """Points along a rounded rectangle outline (closed)."""
    pts = []
    corners = [((x1 - r, y0 + r), -90), ((x1 - r, y1 - r), 0),
               ((x0 + r, y1 - r), 90), ((x0 + r, y0 + r), 180)]
    for (cx, cy), start in corners:
        for i in range(n + 1):
            a = math.radians(start + 90 * i / n)
            pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
    pts.append(pts[0])
    return pts
