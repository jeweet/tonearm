"""Album orders that are sorted in Python (no GTK, so unit-testable)."""
from __future__ import annotations

# An artist's albums: their own order (newest first by default), saved apart from the
# Albums page's, with the same menu layout.
ARTIST_ALBUM_SORTS = [("year", "Year"), ("title", "Title"), ("recent", "Date Added")]
ARTIST_ALBUM_DESCENDING = {"year", "recent"}     # natural order: newest first
ARTIST_ALBUM_KEYS = {
    "year": lambda a: (a.year or 0, (a.title or "").lower()),
    "title": lambda a: (a.title or "").lower(),
    "recent": lambda a: a.dateadded or "",
}


def sort_artist_albums(albums, key: str, reverse: bool = False) -> list:
    """``albums`` in the order ``key`` (its natural direction, flipped by ``reverse``)."""
    if key not in ARTIST_ALBUM_KEYS:
        key = "year"
    fn = ARTIST_ALBUM_KEYS[key]
    desc = (key in ARTIST_ALBUM_DESCENDING) != bool(reverse)
    if key == "year":   # newest year first, but A→Z within a year either way
        out = sorted(albums, key=lambda a: (a.title or "").lower())
        return sorted(out, key=lambda a: a.year or 0, reverse=desc)
    return sorted(albums, key=fn, reverse=desc)
