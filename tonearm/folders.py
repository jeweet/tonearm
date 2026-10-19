"""Folder browsing over Kodi's music sources (pure logic, unit-testable).

Kodi's ``Files.GetDirectory`` lists a folder's subfolders and music files — also music
that isn't in the library (new downloads, files without tags).
"""
from __future__ import annotations

import urllib.parse

PLAYLIST_EXT = (".m3u", ".m3u8", ".pls", ".xsp", ".cue")


def name_of(path: str) -> str:
    """The last part of a path or URL, readable ("smb://nas/Music/" → "Music")."""
    p = (path or "").rstrip("/\\")
    tail = p.replace("\\", "/").rsplit("/", 1)[-1] if p else ""
    return urllib.parse.unquote(tail) or path or ""


def kind_of(entry: dict) -> str:
    """"folder", "playlist" or "song"."""
    if entry.get("filetype") == "directory":
        return "folder"
    if (entry.get("file") or "").lower().endswith(PLAYLIST_EXT):
        return "playlist"
    return "song"


def normalise(entries: list[dict]) -> list[dict]:
    """Kodi's entries → {path, name, kind, title, artist, album, duration, size,
    modified, songid} — songid when the file is in the library."""
    out = []
    for e in entries or []:
        path = e.get("file") or ""
        kind = kind_of(e)
        artist = e.get("artist")
        out.append({
            "path": path,
            "name": (name_of(path) if kind != "folder" else
                     (e.get("label") or name_of(path))),
            "kind": kind,
            "title": e.get("title") or "",
            "artist": ", ".join(a for a in artist if a) if isinstance(artist, list)
            else (artist or ""),
            "album": e.get("album") or "",
            "duration": int(e.get("duration") or 0),
            "size": int(e.get("size") or 0),
            "modified": (e.get("lastmodified") or "")[:16],
            "songid": int(e.get("id") or 0) if e.get("type") == "song" else 0,
        })
    return out


SORT_KEYS = {
    "name": lambda e: e["name"].lower(),
    "artist": lambda e: (e["artist"].lower(), e["name"].lower()),
    "album": lambda e: (e["album"].lower(), e["name"].lower()),
    "duration": lambda e: e["duration"],
    "size": lambda e: e["size"],
    "modified": lambda e: e["modified"],
}


def sort_entries(entries: list[dict], key: str = "name", descending: bool = False) -> list[dict]:
    """Folders first (A→Z, or Z→A when sorting by name descending), then the files."""
    fn = SORT_KEYS.get(key, SORT_KEYS["name"])
    folders = sorted((e for e in entries if e["kind"] == "folder"),
                     key=SORT_KEYS["name"], reverse=descending and key == "name")
    files = sorted((e for e in entries if e["kind"] != "folder"), key=fn, reverse=descending)
    return folders + files


def human_size(n: int) -> str:
    if n <= 0:
        return ""
    for unit in ("B", "kB", "MB", "GB"):
        if n < 1000 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1000
    return ""


__all__ = ["normalise", "sort_entries", "kind_of", "name_of", "human_size"]
