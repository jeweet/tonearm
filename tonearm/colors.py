"""Dominant colour of a cover (pure Python, so it is testable without GTK)."""
from __future__ import annotations

import colorsys


def dominant_color(pixels: bytes, width: int, height: int, rowstride: int,
                   channels: int = 4) -> tuple[float, float, float] | None:
    """The cover's most present *vivid* colour as (r, g, b) in 0…1.

    Pixels are binned coarsely (4 bits per channel); each bin is weighted by how many
    pixels fall in it and by how colourful they are, so a big grey or black area
    doesn't beat a smaller strong colour. Mostly grey covers fall back to their
    average. None for an empty image."""
    if width <= 0 or height <= 0:
        return None
    bins: dict[int, list[float]] = {}
    total = [0.0, 0.0, 0.0, 0]
    for y in range(height):
        row = y * rowstride
        for x in range(width):
            i = row + x * channels
            r, g, b = pixels[i], pixels[i + 1], pixels[i + 2]
            if channels == 4 and pixels[i + 3] < 128:
                continue  # transparent
            total[0] += r
            total[1] += g
            total[2] += b
            total[3] += 1
            _h, light, sat = colorsys.rgb_to_hls(r / 255, g / 255, b / 255)
            if light < 0.08 or light > 0.94 or sat < 0.18:
                continue  # near black / white / grey: no colour to speak of
            weight = 0.3 + sat * (1 - abs(light - 0.5))
            key = (r >> 4) << 8 | (g >> 4) << 4 | (b >> 4)
            acc = bins.setdefault(key, [0.0, 0.0, 0.0, 0.0])
            acc[0] += r * weight
            acc[1] += g * weight
            acc[2] += b * weight
            acc[3] += weight
    if total[3] == 0:
        return None
    if bins:
        best = max(bins.values(), key=lambda a: a[3])
        if best[3] >= total[3] * 0.02:  # a colour worth calling dominant
            return best[0] / best[3] / 255, best[1] / best[3] / 255, best[2] / best[3] / 255
    n = total[3]
    return total[0] / n / 255, total[1] / n / 255, total[2] / n / 255


def tune_for_glow(rgb: tuple[float, float, float], dark: bool) -> tuple[float, float, float]:
    """Keep a glow colour pleasant behind text: not too dark on a dark card, not too
    pale on a light one, and a little more saturated."""
    h, light, sat = colorsys.rgb_to_hls(*rgb)
    sat = min(1.0, sat * 1.15 + 0.05) if sat > 0.1 else sat
    light = max(0.35, min(0.62, light)) if dark else max(0.45, min(0.72, light))
    return colorsys.hls_to_rgb(h, light, sat)


# -- tile colours ---------------------------------------------------------------
TILE_COLORS = 14   # .c0 … .c13 for .radio-tile / .decade-tile / .genre-hero (style.css)


def stable_index(key: str, n: int = TILE_COLORS) -> int:
    """A colour that belongs to ``key`` (the same on every visit)."""
    return sum(map(ord, (key or "").lower())) % n


def distinct_colors(preferred: list[int], n: int = TILE_COLORS) -> list[int]:
    """One colour per tile: each its preferred one unless already taken on the page,
    else the next free one — no colour twice while there are enough. (More tiles
    than colours: then at least not the same as the tile before.)"""
    out: list[int] = []
    used: set[int] = set()
    for want in preferred:
        c = want % n
        if len(used) < n:
            while c in used:
                c = (c + 1) % n
        elif out and c == out[-1]:
            c = (c + 1) % n
        out.append(c)
        used.add(c)
    return out
