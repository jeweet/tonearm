"""Genre maths for the Genres page and a genre's own page (no GTK: unit-testable).

Genres come from the albums' tags ("Rock / Pop", "Jazz; Fusion" → two genres), case-
insensitively ("rock" and "Rock" are one genre, shown as "Rock").
"""
from __future__ import annotations

from collections import Counter

from .colors import distinct_colors, stable_index


def split(genre: str | list | None) -> list[str]:
    """The genres in one tag string (or Kodi's list of them), in order, no repeats."""
    parts = genre if isinstance(genre, list) else \
        (genre or "").replace(" / ", ",").replace(";", ",").split(",")
    out: list[str] = []
    for part in parts:
        g = (part or "").strip()
        if g and g.lower() not in (o.lower() for o in out):
            out.append(g)
    return out


def _better(name: str, current: str | None) -> bool:
    """Prefer "Rock" over "rock" as the shown spelling."""
    return current is None or (current.islower() and not name.islower())


def overview(rows, plays=None, covers_per: int = 3) -> list[dict]:
    """One entry per genre from album rows ``(genre tag, thumbnail, artist)``, newest
    album first: {name, count, covers (up to ``covers_per``, different artists),
    plays}. ``plays``: songs with "genre" and "playcount" (Kodi's most played)."""
    names: dict[str, str] = {}
    counts: Counter = Counter()
    covers: dict[str, list] = {}
    seen: dict[str, set] = {}
    for tag, thumb, artist in rows:
        for g in split(tag):
            key = g.lower()
            if _better(g, names.get(key)):
                names[key] = g
            counts[key] += 1
            got = covers.setdefault(key, [])
            who = seen.setdefault(key, set())
            art = (artist or "").lower()
            if thumb and len(got) < covers_per and art not in who:
                got.append(thumb)
                who.add(art)
    played: Counter = Counter()
    for s in plays or []:
        weight = max(1, int(s.get("playcount") or 0))
        for g in split(s.get("genre"))[:2]:
            if g.lower() in counts:
                played[g.lower()] += weight
    return [{"name": names[k], "count": counts[k], "covers": covers.get(k, []),
             "plays": played.get(k, 0)} for k in counts]


def top(stats: list[dict], n: int = 8) -> list[dict]:
    """The genres to feature: most played first (when Kodi has play counts), then the
    biggest; ties A→Z."""
    return sorted(stats, key=lambda s: (-s["plays"], -s["count"], s["name"].lower()))[:n]


def ordered(stats: list[dict], sort: str = "name", hide_small: bool = False) -> list[dict]:
    """All genres for the list: A→Z (``"name"``) or biggest first (``"count"``);
    ``hide_small`` leaves out genres with a single album."""
    keep = [s for s in stats if not hide_small or s["count"] > 1]
    if sort == "count":
        return sorted(keep, key=lambda s: (-s["count"], s["name"].lower()))
    return sorted(keep, key=lambda s: s["name"].lower())


def initial(name: str) -> str:
    c = (name or "#")[0].upper()
    return c if "A" <= c <= "Z" else "#"


def by_letter(stats: list[dict]) -> list[tuple[str, list[dict]]]:
    """A→Z stats grouped under their first letter ("#" for digits and symbols)."""
    groups: list[tuple[str, list[dict]]] = []
    for s in stats:
        letter = initial(s["name"])
        if not groups or groups[-1][0] != letter:
            groups.append((letter, []))
        groups[-1][1].append(s)
    return groups


def related(tags: list, genre: str, limit: int = 6) -> list[tuple[str, int]]:
    """Genres that share albums with ``genre``: (name, shared albums), most first.
    ``tags``: the genre tag of each album of that genre."""
    want = genre.lower()
    names: dict[str, str] = {}
    counts: Counter = Counter()
    for tag in tags:
        for g in split(tag):
            key = g.lower()
            if key == want:
                continue
            if _better(g, names.get(key)):
                names[key] = g
            counts[key] += 1
    return [(names[k], n) for k, n in
            sorted(counts.items(), key=lambda kv: (-kv[1], names[kv[0]].lower()))[:limit]]


def decades(years: list) -> list[tuple[int, int]]:
    """(decade, albums) oldest first; albums without a year are left out."""
    c = Counter((int(y) // 10) * 10 for y in years if y and int(y) > 0)
    return sorted(c.items())


def color_index(name: str) -> int:
    """A stable colour for a genre (same palette as the radio / decade tiles)."""
    return stable_index(name)


def spread_colors(names: list[str]) -> list[int]:
    """Stable colours, no colour twice on the page."""
    return distinct_colors([stable_index(n) for n in names])
