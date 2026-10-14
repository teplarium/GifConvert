"""UI rendering with Pillow: background, cards and "liquid glass".

The glass here is not a flat blur but a lens: the background under an element is
slightly magnified and refracted near the edges (like looking through a drop of water),
with a faint chromatic fringe and highlights. Everything is pre-rendered into images;
tkinter only displays them and draws the text on top.
"""

from __future__ import annotations

import math

from PIL import Image, ImageChops, ImageDraw, ImageEnhance, ImageFilter

# ---- palette: airy light gray, black pills, peach → lavender
BG = (239, 238, 242)
INK = (13, 13, 18)
MUTED = (139, 139, 150)
FAINT = (184, 184, 194)
CARD = (255, 255, 255)
FIELD = (242, 242, 246)
LINE = (236, 236, 241)
PEACH = (247, 190, 172)
LAVENDER = (184, 194, 248)
PINK = (255, 92, 150)
BLUE = (47, 123, 246)
RED = (229, 72, 77)


def hex_color(rgb) -> str:
    return "#%02x%02x%02x" % tuple(rgb[:3])


# ---------------------------------------------------------------- basic shapes

def rr_mask(w: int, h: int, r: float, ss: int = 3) -> Image.Image:
    """Anti-aliased rounded rectangle mask (mode L)."""
    w, h = max(1, int(w)), max(1, int(h))
    big = Image.new("L", (w * ss, h * ss), 0)
    ImageDraw.Draw(big).rounded_rectangle((0, 0, w * ss - 1, h * ss - 1),
                                          radius=max(0, r * ss), fill=255)
    return big.resize((w, h), Image.LANCZOS)


def solid(w: int, h: int, r: float, color, alpha: int = 255) -> Image.Image:
    img = Image.new("RGBA", (max(1, int(w)), max(1, int(h))), tuple(color[:3]) + (0,))
    img.putalpha(rr_mask(w, h, r).point(lambda a: a * alpha // 255))
    return img


def drop_shadow(base: Image.Image, box, r: float, s: float, *, blur=16, dy=8, alpha=26,
                tight_alpha=14):
    """Soft shadow under an element: a wide blurred one plus a tight contact shadow."""
    x0, y0, x1, y1 = (int(round(v)) for v in box)
    w, h = x1 - x0, y1 - y0
    for b, oy, a in ((blur * s, dy * s, alpha), (2 * s, 1 * s, tight_alpha)):
        pad = int(b * 2.5) + 2
        small = 2 if b > 6 else 1
        mw, mh = (w + 2 * pad) // small, (h + 2 * pad) // small
        m = Image.new("L", (max(1, mw), max(1, mh)), 0)
        ImageDraw.Draw(m).rounded_rectangle(
            (pad // small, pad // small, (pad + w) // small, (pad + h) // small),
            radius=r / small, fill=a)
        m = m.filter(ImageFilter.GaussianBlur(b / small))
        if small > 1:
            m = m.resize((w + 2 * pad, h + 2 * pad), Image.BILINEAR)
        black = Image.new("RGB", m.size, (20, 18, 30))
        base.paste(black, (x0 - pad, int(y0 - pad + oy)), m)


def card(base: Image.Image, box, r: float, s: float, color=CARD):
    drop_shadow(base, box, r, s, blur=18, dy=8, alpha=22, tight_alpha=12)
    x0, y0, x1, y1 = (int(round(v)) for v in box)
    base.paste(solid(x1 - x0, y1 - y0, r, color), (x0, y0), rr_mask(x1 - x0, y1 - y0, r))


# ---------------------------------------------------------------- background

def background(w: int, h: int, s: float) -> Image.Image:
    """Light background with pastel blobs and a dot "perforation" — its distortion is what makes the glass visible."""
    w, h = max(1, int(w)), max(1, int(h))
    k = 10
    sw, sh = max(8, w // k), max(8, h // k)
    small = Image.new("RGB", (sw, sh), BG)
    d = ImageDraw.Draw(small)

    def blob(cx, cy, rx, ry, color):
        d.ellipse((cx * sw - rx * sw, cy * sh - ry * sw, cx * sw + rx * sw, cy * sh + ry * sw),
                  fill=color)
    blob(0.98, 0.02, 0.30, 0.22, (226, 228, 250))       # lavender glow, top right
    blob(0.10, 1.02, 0.46, 0.40, PEACH)                 # peach, bottom left
    blob(0.92, 0.98, 0.46, 0.40, LAVENDER)              # lavender, bottom right
    blob(0.52, 1.10, 0.20, 0.16, (255, 160, 190))       # a spark of pink
    small = small.filter(ImageFilter.GaussianBlur(sw * 0.11))
    img = small.resize((w, h), Image.BICUBIC)
    img = Image.blend(Image.new("RGB", (w, h), BG), img, 0.92)

    # perforation: a hexagonal grid of dots (like perforated metal)
    ss = 2
    dots = Image.new("L", (w * ss, h * ss), 0)
    dd = ImageDraw.Draw(dots)
    step = 13 * s * ss
    row = step * 0.866
    rad = 1.05 * s * ss
    y, i = row / 2, 0
    while y < h * ss:
        x = step / 2 + (step / 2 if i % 2 else 0)
        while x < w * ss:
            dd.ellipse((x - rad, y - rad, x + rad, y + rad), fill=255)
            x += step
        y += row
        i += 1
    dots = dots.resize((w, h), Image.LANCZOS).point(lambda a: a * 34 // 255)
    img.paste(Image.new("RGB", (w, h), (70, 64, 96)), (0, 0), dots)
    return img


# ---------------------------------------------------------------- glass

def _sdf(x: float, y: float, hx: float, hy: float, r: float):
    """Signed distance to a rounded-rectangle edge (<0 inside) and the outward normal."""
    qx, qy = abs(x) - (hx - r), abs(y) - (hy - r)
    sx, sy = (1 if x >= 0 else -1), (1 if y >= 0 else -1)
    if qx > 0 and qy > 0:
        ln = math.hypot(qx, qy)
        return ln - r, sx * qx / ln, sy * qy / ln
    if qx > qy:
        return qx - r, sx, 0.0
    return qy - r, 0.0, sy


def _lens_channel(src: Image.Image, w: int, h: int, r: float, m: int, refraction: float,
                  bezel: float, magnify: float, step: int) -> Image.Image:
    hx, hy = w / 2, h / 2
    cols = list(range(0, w, step)) + [w]
    rows = list(range(0, h, step)) + [h]
    pts = {}
    for v in rows:
        for u in cols:
            x, y = u - hx, v - hy
            dist, nx, ny = _sdf(x, y, hx, hy, r)
            depth = max(0.0, -dist)
            e = max(0.0, 1.0 - depth / bezel)
            push = refraction * e * e
            pts[u, v] = (hx + m + x / magnify + nx * push, hy + m + y / magnify + ny * push)
    mesh = []
    for j in range(len(rows) - 1):
        v0, v1 = rows[j], rows[j + 1]
        for i in range(len(cols) - 1):
            u0, u1 = cols[i], cols[i + 1]
            a, b, c, d = pts[u0, v0], pts[u0, v1], pts[u1, v1], pts[u1, v0]
            mesh.append(((u0, v0, u1, v1), (*a, *b, *c, *d)))
    return src.transform((w, h), Image.MESH, mesh, Image.BICUBIC)


def glass(base: Image.Image, box, r: float, s: float, *, refraction=12.0, bezel=16.0,
          magnify=1.05, aberration=0.22, tint=0.10, shadow=True, gloss=1.0, frost=0.35):
    """Draw a glass "drop" onto base (RGB) inside box."""
    x0, y0, x1, y1 = (int(round(v)) for v in box)
    w, h = x1 - x0, y1 - y0
    if w < 4 or h < 4:
        return
    r = min(r, w / 2, h / 2)
    if shadow:
        drop_shadow(base, box, r, s, blur=14, dy=7, alpha=30, tight_alpha=10)

    refr, bz = refraction * s, max(4.0, bezel * s)
    m = int(refr * (1 + aberration) + 4)
    src = base.crop((x0 - m, y0 - m, x1 + m, y1 + m))
    step = max(4, int(round(5 * s)))

    if aberration:
        chans = []
        for band, k in zip(src.split(), (1 + aberration, 1.0, 1 - aberration)):
            chans.append(_lens_channel(band, w, h, r, m, refr * k, bz, magnify, step))
        lens = Image.merge("RGB", chans)
    else:
        lens = _lens_channel(src, w, h, r, m, refr, bz, magnify, step)

    if frost:
        lens = lens.filter(ImageFilter.GaussianBlur(frost * s))
    lens = ImageEnhance.Color(lens).enhance(1.18)            # glass makes colors a bit more vivid
    if tint:
        lens = Image.blend(lens, Image.new("RGB", (w, h), (255, 255, 255)), tint)

    out = lens.convert("RGBA")
    mask = rr_mask(w, h, r)

    # inner shadow near the bottom edge — gives volume
    inner = ImageChops.subtract(mask, _erode(mask, 5 * s)).filter(ImageFilter.GaussianBlur(3 * s))
    inner = ImageChops.multiply(inner, _vgradient(w, h, 0.15, 1.0))
    out = Image.composite(Image.new("RGBA", (w, h), (40, 30, 70, 255)), out,
                          inner.point(lambda a: a * 16 // 255))

    # gloss on top
    if gloss:
        top = ImageChops.multiply(mask, _vgradient(w, h, 1.0, 0.0, stop=0.55))
        out = Image.composite(Image.new("RGBA", (w, h), (255, 255, 255, 255)), out,
                              top.point(lambda a: int(a * 0.22 * gloss)))

    # rim light: bright on top, weaker at the bottom
    rim = ImageChops.subtract(mask, _erode(mask, 1.3 * s)).filter(ImageFilter.GaussianBlur(0.4 * s))
    rim = ImageChops.multiply(rim, _vgradient(w, h, 1.0, 0.45))
    out = Image.composite(Image.new("RGBA", (w, h), (255, 255, 255, 255)), out,
                          rim.point(lambda a: a * 235 // 255))
    # a thin dark outer edge so the glass reads on a light background
    edge = ImageChops.subtract(_dilate(mask, 1.0 * s), mask).point(lambda a: a * 20 // 255)
    base.paste(Image.new("RGB", (w, h), (40, 36, 60)), (x0, y0), edge)

    out.putalpha(mask)
    base.paste(out, (x0, y0), out)


def _erode(mask: Image.Image, px: float) -> Image.Image:
    px = max(1, int(round(px)))
    size = px * 2 + 1
    return mask.filter(ImageFilter.MinFilter(size)) if size <= 9 else \
        mask.filter(ImageFilter.GaussianBlur(px / 2)).point(lambda a: 255 if a > 200 else a * 0)


def _dilate(mask: Image.Image, px: float) -> Image.Image:
    px = max(1, int(round(px)))
    return mask.filter(ImageFilter.MaxFilter(px * 2 + 1))


def _vgradient(w: int, h: int, top: float, bottom: float, stop: float = 1.0) -> Image.Image:
    col = Image.new("L", (1, max(1, h)))
    for y in range(h):
        t = min(1.0, y / max(1, (h - 1) * stop))
        col.putpixel((0, y), int(255 * (top + (bottom - top) * t)))
    return col.resize((w, h))


# ---------------------------------------------------------------- buttons and progress

def black_pill(w: int, h: int, s: float, state: str = "normal") -> Image.Image:
    """Black pill button with a subtle gloss."""
    w, h = int(w), int(h)
    color = {"normal": (13, 13, 18), "hover": (40, 40, 48), "disabled": (120, 120, 130)}[state]
    img = solid(w, h, h / 2, color)
    mask = rr_mask(w, h, h / 2)
    gloss = ImageChops.multiply(mask, _vgradient(w, h, 1.0, 0.0, stop=0.5))
    img = Image.composite(Image.new("RGBA", (w, h), (255, 255, 255, 255)), img,
                          gloss.point(lambda a: a * 30 // 255))
    rim = ImageChops.subtract(mask, _erode(mask, 1 * s))
    rim = ImageChops.multiply(rim, _vgradient(w, h, 1.0, 0.0, stop=0.6))
    img = Image.composite(Image.new("RGBA", (w, h), (255, 255, 255, 255)), img,
                          rim.point(lambda a: a * 70 // 255))
    img.putalpha(mask)
    return img


def progress_tube(back: Image.Image, frac: float, s: float) -> Image.Image:
    """Progress "tube": a recessed track with a liquid peach → pink → lavender fill."""
    w, h = back.size
    out = back.convert("RGBA")
    mask = rr_mask(w, h, h / 2)
    track = Image.new("RGBA", (w, h), (30, 26, 50, 255))
    out = Image.composite(track, out, mask.point(lambda a: a * 18 // 255))
    shade = ImageChops.multiply(mask, _vgradient(w, h, 1.0, 0.0, stop=0.6))
    out = Image.composite(track, out, shade.point(lambda a: a * 22 // 255))
    fw = int(round(w * max(0.0, min(1.0, frac))))
    if fw >= 2:
        fw = max(fw, h)
        grad = _hgradient(fw, h, (PEACH, PINK, LAVENDER))
        fmask = rr_mask(fw, h, h / 2)
        hl = ImageChops.multiply(fmask, _vgradient(fw, h, 1.0, 0.0, stop=0.55))
        grad = Image.composite(Image.new("RGB", (fw, h), (255, 255, 255)), grad,
                               hl.point(lambda a: a * 110 // 255))
        out.paste(grad, (0, 0), fmask)
    return out.convert("RGB")


def _hgradient(w: int, h: int, stops) -> Image.Image:
    row = Image.new("RGB", (max(1, w), 1))
    n = len(stops) - 1
    for x in range(w):
        t = x / max(1, w - 1) * n
        i = min(n - 1, int(t))
        f = t - i
        a, b = stops[i], stops[i + 1]
        row.putpixel((x, 0), tuple(int(a[c] + (b[c] - a[c]) * f) for c in range(3)))
    return row.resize((max(1, w), max(1, h)))


def fill_rr(base: Image.Image, box, r: float, color) -> None:
    """Paste a solid anti-aliased rounded rectangle onto base."""
    x0, y0, x1, y1 = (int(round(v)) for v in box)
    w, h = x1 - x0, y1 - y0
    base.paste(Image.new("RGB", (max(1, w), max(1, h)), tuple(color[:3])), (x0, y0),
               rr_mask(w, h, r))


def status_pill(w: int, h: int, bg, frac: float | None = None, back=CARD) -> Image.Image:
    """A status pill for the file list (optionally with a progress fill), on a solid background."""
    w, h = max(2, int(w)), max(2, int(h))
    out = Image.new("RGB", (w, h), tuple(back[:3]))
    fill_rr(out, (0, 0, w, h), h / 2, bg)
    if frac is not None and frac > 0:
        fw = max(h, int(round(w * min(1.0, frac))))
        grad = _hgradient(fw, h, (PEACH, PINK, LAVENDER))
        grad = Image.blend(Image.new("RGB", (fw, h), (255, 255, 255)), grad, 0.55)
        out.paste(grad, (0, 0), rr_mask(fw, h, h / 2))
    return out
