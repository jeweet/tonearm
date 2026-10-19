#!/usr/bin/env python3
"""Generate the app's bundled 16×16 symbolic icons (GNOME/Adwaita style).

Everything is drawn with *filled* shapes only — GTK recolours symbolic icons by
overriding `fill`, so strokes would stay dark in dark mode.

    python3 build-aux/make_icons.py             # writes tonearm/icons/…
    python3 build-aux/make_icons.py --preview p.png   # contact sheet (needs Pillow)
"""
from __future__ import annotations

import math
import os
import sys

OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "tonearm", "icons", "hicolor", "scalable", "actions")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP_ID = "io.github.jeweet.Tonearm"
W = 1.75  # line weight, like Adwaita's 16px symbolics
FILL = "#2e3436"


class Icon:
    def __init__(self):
        self.shapes = []  # ("poly", pts) | ("circle", cx, cy, r) | ("ring", cx, cy, ro, ri) | ("frame", ...)

    # -- primitives ------------------------------------------------------
    def poly(self, pts):
        self.shapes.append(("poly", list(pts)))
        return self

    def circle(self, cx, cy, r):
        self.shapes.append(("circle", cx, cy, r))
        return self

    def ring(self, cx, cy, ro, ri):
        self.shapes.append(("ring", cx, cy, ro, ri))
        return self

    def rect(self, x, y, w, h, r=0.0):
        return self.poly(_rrect_pts(x, y, w, h, r))

    def frame(self, x, y, w, h, t=W, r=2.0):
        """Rounded rectangle outline (as an even-odd filled shape)."""
        self.shapes.append(("frame", _rrect_pts(x, y, w, h, r),
                            _rrect_pts(x + t, y + t, w - 2 * t, h - 2 * t, max(0.0, r - t))))
        return self

    def line(self, x1, y1, x2, y2, w=W, caps=True):
        dx, dy = x2 - x1, y2 - y1
        n = math.hypot(dx, dy) or 1.0
        ox, oy = -dy / n * w / 2, dx / n * w / 2
        self.poly([(x1 + ox, y1 + oy), (x2 + ox, y2 + oy), (x2 - ox, y2 - oy), (x1 - ox, y1 - oy)])
        if caps:
            self.circle(x1, y1, w / 2)
            self.circle(x2, y2, w / 2)
        return self

    def polyline(self, pts, w=W):
        for a, b in zip(pts, pts[1:]):
            self.line(*a, *b, w=w)
        return self

    def arc(self, cx, cy, r, a0, a1, w=W, steps=None):
        steps = steps or max(4, int(abs(a1 - a0) / 12))
        pts = [(cx + r * math.cos(math.radians(a0 + (a1 - a0) * i / steps)),
                cy + r * math.sin(math.radians(a0 + (a1 - a0) * i / steps))) for i in range(steps + 1)]
        return self.polyline(pts, w)

    # -- output ----------------------------------------------------------
    def svg(self) -> str:
        parts = []
        for s in self.shapes:
            if s[0] == "poly":
                parts.append(f'<path d="{_d(s[1])}"/>')
            elif s[0] == "circle":
                parts.append(f'<circle cx="{s[1]:.3f}" cy="{s[2]:.3f}" r="{s[3]:.3f}"/>')
            elif s[0] == "ring":
                _, cx, cy, ro, ri = s
                parts.append(f'<path fill-rule="evenodd" d="{_circle_d(cx, cy, ro)} {_circle_d(cx, cy, ri)}"/>')
            elif s[0] == "frame":
                parts.append(f'<path fill-rule="evenodd" d="{_d(s[1])} {_d(s[2])}"/>')
        body = "\n  ".join(parts)
        return (f'<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 16 16">\n'
                f'  <g fill="{FILL}">\n  {body}\n  </g>\n</svg>\n')

    def raster(self, scale=8):
        from PIL import Image, ImageChops, ImageDraw
        size = 16 * scale
        out = Image.new("L", (size, size), 0)
        for s in self.shapes:
            m = Image.new("L", (size, size), 0)
            d = ImageDraw.Draw(m)
            if s[0] == "poly":
                d.polygon([(x * scale, y * scale) for x, y in s[1]], fill=255)
            elif s[0] == "circle":
                _, cx, cy, r = s
                d.ellipse([(cx - r) * scale, (cy - r) * scale, (cx + r) * scale, (cy + r) * scale], fill=255)
            elif s[0] == "ring":
                _, cx, cy, ro, ri = s
                d.ellipse([(cx - ro) * scale, (cy - ro) * scale, (cx + ro) * scale, (cy + ro) * scale], fill=255)
                d.ellipse([(cx - ri) * scale, (cy - ri) * scale, (cx + ri) * scale, (cy + ri) * scale], fill=0)
            elif s[0] == "frame":
                d.polygon([(x * scale, y * scale) for x, y in s[1]], fill=255)
                d.polygon([(x * scale, y * scale) for x, y in s[2]], fill=0)
            out = ImageChops.lighter(out, m)
        return out


def _rrect_pts(x, y, w, h, r):
    r = max(0.0, min(r, w / 2, h / 2))
    if r == 0:
        return [(x, y), (x + w, y), (x + w, y + h), (x, y + h)]
    pts = []
    for cx, cy, a0 in ((x + w - r, y + r, -90), (x + w - r, y + h - r, 0),
                       (x + r, y + h - r, 90), (x + r, y + r, 180)):
        for i in range(7):
            a = math.radians(a0 + 90 * i / 6)
            pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
    return pts


def _d(pts):
    return "M" + " L".join(f"{x:.3f},{y:.3f}" for x, y in pts) + " Z"


def _circle_d(cx, cy, r):
    return (f"M{cx - r:.3f},{cy:.3f} a{r:.3f},{r:.3f} 0 1,0 {2 * r:.3f},0 "
            f"a{r:.3f},{r:.3f} 0 1,0 {-2 * r:.3f},0 Z")


def _mirror(icon: Icon) -> Icon:
    m = Icon()
    for s in icon.shapes:
        if s[0] == "poly":
            m.shapes.append(("poly", [(16 - x, y) for x, y in s[1]]))
        elif s[0] in ("circle", "ring"):
            m.shapes.append((s[0], 16 - s[1], *s[2:]))
        elif s[0] == "frame":
            m.shapes.append(("frame", [(16 - x, y) for x, y in s[1]], [(16 - x, y) for x, y in s[2]]))
    return m


def _arrow_head(x, y, direction, size=3.0):
    """Filled triangle pointing in direction ('r','l','u','d') with its tip at (x, y)."""
    s = size
    return {"r": [(x - s, y - s), (x, y), (x - s, y + s)],
            "l": [(x + s, y - s), (x, y), (x + s, y + s)],
            "u": [(x - s, y + s), (x, y), (x + s, y + s)],
            "d": [(x - s, y - s), (x, y), (x + s, y - s)]}[direction]


def _spotlight() -> Icon:
    """A star in a circle (GNOME style: a filled shape inside a 1.5 px ring)."""
    i = Icon().ring(8, 8, 7.5, 6.0)
    cx, cy, ro, ri = 8, 8.35, 4.1, 1.75        # a touch low: looks centred
    pts = []
    for k in range(10):
        r = ro if k % 2 == 0 else ri
        a = math.radians(-90 + k * 36)
        pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
    i.poly(pts)
    return i


def _moon() -> Icon:
    """A crescent: a disc with an offset disc cut out of its upper right."""
    (cx, cy, r), (kx, ky, kr) = (7.6, 8.4, 6.6), (11.2, 4.6, 5.6)
    inside_cut = lambda x, y: (x - kx) ** 2 + (y - ky) ** 2 < kr ** 2   # noqa: E731
    inside_disc = lambda x, y: (x - cx) ** 2 + (y - cy) ** 2 < r ** 2   # noqa: E731
    n = 180
    outer = [(cx + r * math.cos(2 * math.pi * k / n), cy + r * math.sin(2 * math.pi * k / n))
             for k in range(n)]
    keep = [not inside_cut(x, y) for x, y in outer]
    start = next(k for k in range(n) if keep[k] and not keep[k - 1])   # just past the cut
    rim = [outer[(start + k) % n] for k in range(n) if keep[(start + k) % n]]
    inner = [(kx + kr * math.cos(2 * math.pi * k / n), ky + kr * math.sin(2 * math.pi * k / n))
             for k in range(n)]
    bite = [pt for pt in inner if inside_disc(*pt)]
    # walk the bite from the rim's end back to its start (nearest end first)
    if bite and math.dist(bite[0], rim[-1]) > math.dist(bite[-1], rim[-1]):
        bite.reverse()
    # the bite's points may wrap around the list: order them along the arc
    return Icon().poly(rim + _arc_order(bite, rim[-1]))


def _arc_order(pts, first):
    """Points of one arc, in order along it, starting nearest ``first``."""
    out, rest = [], list(pts)
    cur = first
    while rest:
        nxt = min(rest, key=lambda p: math.dist(p, cur))
        out.append(nxt)
        rest.remove(nxt)
        cur = nxt
    return out


def _app_symbolic() -> Icon:
    """Tonearm's symbolic app icon: a record with its label, and the tonearm resting
    on it from the top right."""
    i = Icon().ring(7, 9, 6.6, 5.1).circle(7, 9, 1.7)
    i.circle(13.6, 2.4, 1.6)                       # the arm's pivot
    i.line(13.6, 2.4, 13.6, 7.2, w=1.4, caps=False)
    i.line(13.6, 7.2, 10.4, 10.6, w=1.4)           # down onto the record
    return i


def _repeat() -> Icon:
    i = Icon()
    # top run, left side, bottom run, right side — a rounded loop with two arrowheads
    i.line(5, 3.5, 10.5, 3.5).arc(5, 6, 2.5, 180, 270).line(2.5, 6, 2.5, 8.5)
    i.line(11, 12.5, 5.5, 12.5).arc(11, 10, 2.5, 0, 90).line(13.5, 10, 13.5, 7.5)
    i.poly(_arrow_head(14, 3.5, "r", 2.6))
    i.poly(_arrow_head(2, 12.5, "l", 2.6))
    return i


def build() -> dict[str, Icon]:
    I = {}  # noqa: E741

    I["play"] = Icon().poly([(4.5, 2.2), (13.8, 8), (4.5, 13.8)])
    I["pause"] = Icon().rect(3.5, 2.5, 3.25, 11, 1).rect(9.25, 2.5, 3.25, 11, 1)
    nxt = Icon().poly([(2.5, 3), (10.5, 8), (2.5, 13)]).rect(11.25, 3, 2.25, 10, 1)
    I["next"] = nxt
    I["previous"] = _mirror(nxt)

    sh = Icon()
    sh.polyline([(1.5, 4.5), (4.5, 4.5), (10, 11.5), (12, 11.5)])
    sh.polyline([(1.5, 11.5), (4.5, 11.5), (10, 4.5), (12, 4.5)])
    sh.poly(_arrow_head(15, 4.5, "r", 2.6)).poly(_arrow_head(15, 11.5, "r", 2.6))
    I["shuffle"] = sh

    I["repeat"] = _repeat()
    one = _repeat()
    one.rect(7.1, 5.5, 1.8, 5.2, 0.4).poly([(7.1, 5.5), (8.9, 5.5), (6.2, 7.2), (6.2, 6.4)])
    I["repeat-one"] = one
    con = Icon()
    con.line(2, 4, 13, 4).line(2, 8, 9.5, 8).poly(_arrow_head(14, 8, "r", 3)).line(2, 12, 13, 12)
    I["consecutive"] = con

    I["album"] = Icon().ring(8, 8, 7, 5.25).ring(8, 8, 2.6, 1.0)

    art = Icon().circle(8, 4.6, 3.1)
    art.poly([(8 + 5.6 * math.cos(math.radians(a)), 15 + 6.2 * math.sin(math.radians(a)))
              for a in range(180, 361, 15)])
    I["artist"] = art

    I["song"] = (Icon().rect(10.2, 1.5, 1.8, 10, 0.5)
                 .poly([(10.2, 1.5), (14.5, 3.0), (14.5, 5.6), (12.0, 4.8)])
                 .circle(7.8, 11.8, 3.0))

    I["spotlight"] = _spotlight()   # a star in a circle

    dl = Icon().line(8, 1.5, 8, 8.5).poly(_arrow_head(8, 11.2, "d", 3.6))
    dl.polyline([(2, 10), (2, 14), (14, 14), (14, 10)])
    I["lidarr"] = dl

    I["search"] = Icon().ring(6.5, 6.5, 5.25, 3.5).line(10.4, 10.4, 14, 14, w=2.2)
    I["more"] = Icon().circle(3, 8, 1.6).circle(8, 8, 1.6).circle(13, 8, 1.6)
    I["menu"] = Icon().rect(2, 3, 12, 1.9, 0.9).rect(2, 7.05, 12, 1.9, 0.9).rect(2, 11.1, 12, 1.9, 0.9)
    # most played: a bar chart, rising to the right
    I["most-played"] = Icon().rect(1.5, 9, 3.4, 5.5, 1).rect(6.3, 5.5, 3.4, 9, 1).rect(11.1, 1.5, 3.4, 13, 1)
    # sort (Genres page): bars getting shorter, like Adwaita's view-sort-descending
    I["sort"] = Icon().rect(2, 3, 12, 1.9, 0.9).rect(2, 7.05, 8.5, 1.9, 0.9).rect(2, 11.1, 5, 1.9, 0.9)

    # grid, list and starred: the stock Adwaita shapes (as in Crate), see icons-raw/

    ref = Icon().arc(8, 8, 5.6, 0, 270)       # right → bottom → left → top (clockwise)
    ref.poly(_arrow_head(12.2, 2.4, "r", 2.8))   # arrow continues the arc to the right
    I["refresh"] = ref

    I["add"] = Icon().rect(7.1, 2, 1.8, 12, 0.9).rect(2, 7.1, 12, 1.8, 0.9)
    I["remove"] = Icon().rect(2, 7.1, 12, 1.8, 0.9)

    pen = Icon()
    pen.poly([(10.6, 2.2), (13.8, 5.4), (6, 13.2), (2.8, 10)])
    pen.poly([(2.8, 10), (6, 13.2), (1.8, 14.2)])
    I["edit"] = pen

    I["go-next"] = Icon().polyline([(5.5, 2.5), (11, 8), (5.5, 13.5)], w=2)

    I["sidebar"] = Icon().frame(1, 2, 14, 12, W, 2.5).rect(5.3, 2.5, 1.75, 11)

    sl = Icon()
    for y, x in ((3.5, 10.5), (8, 5), (12.5, 9)):
        sl.line(2, y, 14, y, w=1.5).circle(x, y, 2.3)
    I["settings"] = sl
    I["sleep"] = _moon()  # sleep timer: a crescent moon
    # drag handle (queue rows): two columns of three dots
    I["drag"] = Icon().circle(5.5, 3.5, 1.4).circle(10.5, 3.5, 1.4).circle(5.5, 8, 1.4) \
        .circle(10.5, 8, 1.4).circle(5.5, 12.5, 1.4).circle(10.5, 12.5, 1.4)

    srv = Icon().frame(1.5, 1.5, 13, 5.75, W, 1.8).frame(1.5, 8.75, 13, 5.75, W, 1.8)
    srv.circle(11.2, 4.4, 1.0).circle(11.2, 11.6, 1.0)
    I["server"] = srv

    rem = Icon().frame(4, 0.75, 8, 14.5, W, 2.6).circle(8, 5, 1.9)
    rem.circle(6.4, 9.6, 0.95).circle(9.6, 9.6, 0.95).circle(6.4, 12.3, 0.95).circle(9.6, 12.3, 0.95)
    I["remote"] = rem

    spk = Icon().poly([(1.5, 5.8), (4.4, 5.8), (8.2, 2.2), (8.2, 13.8), (4.4, 10.2), (1.5, 10.2)])
    vol = Icon()
    vol.shapes = list(spk.shapes)
    vol.arc(8.2, 8, 3.0, -45, 45, w=1.6).arc(8.2, 8, 5.8, -50, 50, w=1.6)
    I["volume"] = vol
    mute = Icon()
    mute.shapes = list(spk.shapes)
    mute.line(10.5, 5.5, 14.5, 10.5, w=1.6).line(14.5, 5.5, 10.5, 10.5, w=1.6)
    I["volume-muted"] = mute

    web = Icon().ring(8, 8, 7, 5.25)
    web.frame(4.9, 1.6, 6.2, 12.8, 1.5, 3.1)          # meridian
    web.line(1.8, 8, 14.2, 8, w=1.5, caps=False)       # equator
    I["web"] = web

    I["top"] = Icon().polyline([(2.5, 10.75), (8, 5.25), (13.5, 10.75)], w=2)  # like GNOME's go-up
    I["status"] = Icon().circle(8, 8, 4)

    hist = Icon().arc(8, 8, 6.1, -200, 125)                 # history: open circle…
    hist.poly(_arrow_head(2.25, 13.0, "d", 2.5))            # …turning back at the lower left
    hist.line(8, 4.6, 8, 8.2).line(8, 8.2, 10.4, 9.8)
    I["played"] = hist

    tag = Icon()
    tag.shapes.append(("frame",
                       [(1.5, 2.5), (8.2, 2.5), (14.5, 8.8), (9.3, 14), (1.5, 6.2)],
                       [(3.25, 4.25), (7.5, 4.25), (12.0, 8.8), (9.3, 11.5), (3.25, 6.9)]))
    tag.circle(5.4, 6.2, 1.25)
    I["genre"] = tag

    cal = Icon().frame(1.5, 2.5, 13, 12, W, 2.2).rect(1.5, 2.5, 13, 3.6, 1.6)
    cal.rect(4, 0.8, 1.8, 3.2, 0.9).rect(10.2, 0.8, 1.8, 3.2, 0.9)
    for x in (4.2, 7.2, 10.2):
        for y in (8, 10.9):
            cal.rect(x, y, 1.8, 1.7, 0.4)
    I["decade"] = cal

    pl = (Icon().rect(1.5, 3, 12, 1.9, 0.9).rect(1.5, 7.05, 6.5, 1.9, 0.9)
          .rect(1.5, 11.1, 6.5, 1.9, 0.9).poly([(10, 7), (15, 10.25), (10, 13.5)]))
    I["playlist"] = pl

    radio = Icon().circle(8, 8, 2.2)                         # "what should I play?": a broadcast
    for r in (4.6, 7.0):
        radio.arc(8, 8, r, -45, 45, w=1.6).arc(8, 8, r, 135, 225, w=1.6)
    I["radio"] = radio
    return I


def main():
    icons = build()
    os.makedirs(OUT, exist_ok=True)
    for name, icon in icons.items():
        with open(os.path.join(OUT, f"tonearm-{name}-symbolic.svg"), "w") as f:
            f.write(icon.svg())
    theme_dir = os.path.dirname(os.path.dirname(OUT))
    with open(os.path.join(theme_dir, "index.theme"), "w") as f:
        f.write("[Icon Theme]\nName=Hicolor\nComment=Tonearm bundled icons\n"
                "Directories=scalable/actions,scalable/apps\n\n"
                "[scalable/actions]\nSize=16\nMinSize=8\nMaxSize=512\nType=Scalable\n"
                "Context=Actions\n\n"
                "[scalable/apps]\nSize=128\nMinSize=16\nMaxSize=512\nType=Scalable\n"
                "Context=Applications\n")
    # the app icon (and its symbolic version) bundled too: the About dialog and the
    # window find it when running from the source tree, before anything is installed
    data_icons = os.path.join(ROOT, "data", "icons")
    with open(os.path.join(data_icons, f"{APP_ID}-symbolic.svg"), "w") as f:
        f.write(_app_symbolic().svg())
    apps = os.path.join(theme_dir, "scalable", "apps")
    os.makedirs(apps, exist_ok=True)
    for name in (f"{APP_ID}.svg", f"{APP_ID}-symbolic.svg"):
        with open(os.path.join(data_icons, name)) as src, \
                open(os.path.join(apps, name), "w") as dst:
            dst.write(src.read())
    # hand-drawn / stock Adwaita symbolics, copied as they are
    raw = os.path.join(os.path.dirname(os.path.abspath(__file__)), "icons-raw")
    for fname in sorted(os.listdir(raw)):
        if fname.endswith(".svg"):
            with open(os.path.join(raw, fname)) as src, \
                    open(os.path.join(OUT, f"tonearm-{fname[:-4]}-symbolic.svg"), "w") as dst:
                dst.write(src.read())
    print(f"wrote {len(icons)} icons (+ icons-raw) to {OUT}")
    if "--preview" in sys.argv:
        from PIL import Image, ImageDraw, ImageOps
        path = sys.argv[sys.argv.index("--preview") + 1]
        cell, cols = 160, 8
        rows = (len(icons) + cols - 1) // cols
        sheet = Image.new("RGB", (cols * cell, rows * cell), "white")
        d = ImageDraw.Draw(sheet)
        for n, (name, icon) in enumerate(icons.items()):
            x, y = (n % cols) * cell, (n // cols) * cell
            m = icon.raster(8)
            sheet.paste(ImageOps.invert(m.convert("RGB")), (x + 16, y + 8), m)
            small = icon.raster(2).resize((32, 32))
            sheet.paste(ImageOps.invert(small.convert("RGB")), (x + 120, y + 8), small)
            d.text((x + 8, y + 140), name, fill="black")
        sheet.save(path)
        print("preview:", path)


if __name__ == "__main__":
    main()
