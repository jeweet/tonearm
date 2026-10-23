"""Tonearm's own playlists (pure Python, testable headless).

Kodi's JSON-RPC API can read playlist files but not write or delete them, so
playlists you make in Tonearm live here: one JSON file per Kodi instance, holding
Kodi song ids (with title / artist kept alongside, to show something sensible if a
rescan ever drops a song). They play on Kodi through its play queue.
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import uuid


class PlaylistStore:
    def __init__(self, path: str):
        self.path = path
        self._lists: list[dict] = []
        try:
            with open(path) as f:
                data = json.load(f)
            self._lists = [p for p in data.get("playlists", []) if p.get("id")]
        except (OSError, ValueError):
            self._lists = []

    # -- reading -----------------------------------------------------------
    def all(self) -> list[dict]:
        return sorted(self._lists, key=lambda p: p["name"].lower())

    def get(self, pid: str) -> dict | None:
        return next((p for p in self._lists if p["id"] == pid), None)

    # -- changing ------------------------------------------------------------
    def create(self, name: str) -> dict:
        pl = {"id": uuid.uuid4().hex[:10], "name": name.strip() or "Playlist",
              "songs": [], "updated": _now()}
        self._lists.append(pl)
        self.save()
        return pl

    def rename(self, pid: str, name: str):
        pl = self.get(pid)
        if pl is not None and name.strip():
            pl["name"], pl["updated"] = name.strip(), _now()
            self.save()

    def delete(self, pid: str):
        self._lists = [p for p in self._lists if p["id"] != pid]
        self.save()

    def add(self, pid: str, songs: list[dict]) -> int:
        """Append songs ({"id", "title", "artist"}), skipping ones already in the
        playlist. Returns how many were added."""
        pl = self.get(pid)
        if pl is None:
            return 0
        have = {s["id"] for s in pl["songs"]}
        new = []
        for s in songs:
            sid = int(s["id"])
            if sid not in have:          # also only once from this batch
                have.add(sid)
                new.append({"id": sid, "title": s.get("title", ""),
                            "artist": s.get("artist", "")})
        if new:
            pl["songs"].extend(new)
            pl["updated"] = _now()
            self.save()
        return len(new)

    def remove(self, pid: str, index: int):
        """Remove the song at ``index`` (a playlist may hold a song only once, but the
        position is what the list shows)."""
        pl = self.get(pid)
        if pl is not None and 0 <= index < len(pl["songs"]):
            del pl["songs"][index]
            pl["updated"] = _now()
            self.save()

    def move(self, pid: str, src: int, dst: int):
        """Move the song at ``src`` to position ``dst`` (drag to reorder, to top…)."""
        pl = self.get(pid)
        if pl is None:
            return
        songs = pl["songs"]
        if not (0 <= src < len(songs)):
            return
        dst = max(0, min(len(songs) - 1, dst))
        if src == dst:
            return
        songs.insert(dst, songs.pop(src))
        pl["updated"] = _now()
        self.save()

    def reorder(self, pid: str, ids: list[int]):
        """Put the songs in the order of ``ids`` (a sort); unknown ids are ignored and
        songs not named keep their place at the end."""
        pl = self.get(pid)
        if pl is None:
            return
        by_id = {s["id"]: s for s in pl["songs"]}
        out = [by_id.pop(i) for i in ids if i in by_id]
        out += [s for s in pl["songs"] if s["id"] in by_id]
        pl["songs"], pl["updated"] = out, _now()
        self.save()

    def duplicate(self, pid: str, name: str | None = None) -> dict | None:
        pl = self.get(pid)
        if pl is None:
            return None
        copy = self.create(name or f"{pl['name']} (copy)")
        copy["songs"] = [dict(s) for s in pl["songs"]]
        self.save()
        return copy

    def create_with(self, name: str, songs: list[dict]) -> dict:
        """A new playlist holding ``songs`` (the queue, a copy of a Kodi playlist)."""
        pl = self.create(name)
        self.add(pl["id"], songs)
        return pl

    def unique_name(self, name: str) -> str:
        """``name``, or "name 2", "name 3"… when taken."""
        taken = {p["name"].lower() for p in self._lists}
        if name.lower() not in taken:
            return name
        n = 2
        while f"{name} {n}".lower() in taken:
            n += 1
        return f"{name} {n}"

    def save(self):
        tmp = self.path + ".tmp"
        try:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            with open(tmp, "w") as f:
                json.dump({"playlists": self._lists}, f, indent=1)
            os.replace(tmp, self.path)
        except OSError:
            pass


def _now() -> str:
    return _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
