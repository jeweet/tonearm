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
        new = [{"id": int(s["id"]), "title": s.get("title", ""), "artist": s.get("artist", "")}
               for s in songs if int(s["id"]) not in have]
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
