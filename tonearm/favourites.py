"""Kodi Favourites: normalising Kodi's list and building toggle requests (pure data)."""
from __future__ import annotations

import re

PROPS = ["window", "windowparameter", "path", "thumbnail"]
_ALBUM = re.compile(r"musicdb://(?:[^/]+/)*albums/(\d+)/?", re.I)
_ARTIST = re.compile(r"musicdb://(?:[^/]+/)*artists/(\d+)/?$", re.I)
_SONG = re.compile(r"musicdb://(?:[^/]+/)*(\d+)\.[a-z0-9]+$", re.I)   # …/songs/123.flac
PLAYLIST_EXT = (".xsp", ".m3u", ".m3u8", ".pls")
AUDIO = (".flac", ".mp3", ".ogg", ".opus", ".m4a", ".aac", ".wav", ".wv", ".ape", ".alac",
         ".dsf", ".dff", ".wma", ".aiff", ".aif", ".mpc")


def is_playlist(path: str) -> bool:
    """A playlist file (smart .xsp, .m3u/.m3u8, .pls), as Kodi's favourites hold them."""
    return (path or "").lower().rstrip("/").endswith(PLAYLIST_EXT)


def is_music_playlist(path: str, window: str = "") -> bool:
    """A playlist file that isn't a video one (Tonearm plays music only)."""
    p, w = (path or "").lower(), (window or "").lower()
    return is_playlist(p) and "video" not in p and not w.startswith("video")


def normalise(items) -> list[dict]:
    """Kodi's favourites → dicts with a ``kind``: "album" / "artist" (music library
    folders), "song", "playlist" (a playlist file: Kodi makes them as media or as a
    music-window favourite), "media" (another file to play), "window" (a Kodi screen),
    "script", "other"."""
    out = []
    for f in items or []:
        typ = (f.get("type") or "unknown").lower()
        target = f.get("windowparameter") or f.get("path") or ""
        fav = {"title": f.get("title") or "", "type": typ, "path": f.get("path") or "",
               "window": f.get("window") or "", "windowparameter": f.get("windowparameter") or "",
               "thumbnail": f.get("thumbnail") or "", "albumid": 0, "artistid": 0,
               "songid": 0}
        m_album, m_artist = _ALBUM.search(target), _ARTIST.search(target)
        m_song = _SONG.search(fav["path"]) if typ == "media" else None
        if m_song:
            fav["kind"], fav["songid"] = "song", int(m_song.group(1))
        elif typ == "media" and fav["path"].lower().endswith(AUDIO):
            fav["kind"] = "song"            # a song favourite by its file (matched by path)
        elif typ in ("media", "window") and is_music_playlist(target, fav["window"]):
            fav["kind"], fav["playlist"] = "playlist", target
        elif m_album:
            fav["kind"], fav["albumid"] = "album", int(m_album.group(1))
        elif m_artist:
            fav["kind"], fav["artistid"] = "artist", int(m_artist.group(1))
        elif typ in ("media", "window", "script"):
            fav["kind"] = typ
        else:
            fav["kind"] = "other"
        out.append(fav)
    return out


def describe(fav: dict) -> str:
    return {"album": "Album", "artist": "Artist", "song": "Song", "playlist": "Playlist",
            "media": "Media", "window": "Kodi screen", "script": "Add-on",
            "other": "Favorite"}.get(fav.get("kind"), "Favorite")


# music only — Tonearm is a music player: Kodi's other favourites (videos, Kodi
# screens, add-ons) stay in Kodi
GROUPS = [("artist", "Artists"), ("album", "Albums"), ("song", "Songs"),
          ("playlist", "Playlists")]


def grouped(favs: list[dict]) -> list[tuple[str, str, list[dict]]]:
    """Music favorites by kind for the Favorites page: (key, heading, items), in GROUPS
    order, empty groups left out."""
    out = []
    for key, heading in GROUPS:
        items = [f for f in favs if f.get("kind") == key]
        if items:
            out.append((key, heading, items))
    return out


def album_request(albumid: int, title: str, thumbnail: str = "") -> dict:
    """Favourites.AddFavourite params for an album (Kodi toggles: the same call removes
    an existing favourite)."""
    p = {"title": title or "Album", "type": "window", "window": "music",
         "windowparameter": f"musicdb://albums/{albumid}/"}
    if thumbnail:
        p["thumbnail"] = thumbnail
    return p


def artist_request(artistid: int, name: str, thumbnail: str = "") -> dict:
    p = {"title": name or "Artist", "type": "window", "window": "music",
         "windowparameter": f"musicdb://artists/{artistid}/"}
    if thumbnail:
        p["thumbnail"] = thumbnail
    return p


def song_request(path: str, title: str, thumbnail: str = "") -> dict:
    """A song favourite is a "media" favourite of its file (as Kodi makes them)."""
    p = {"title": title or "Song", "type": "media", "path": path}
    if thumbnail:
        p["thumbnail"] = thumbnail
    return p


def playlist_request(path: str, title: str) -> dict:
    """A Kodi playlist file as a favourite ("media": Kodi plays it, as its own file
    browser's "Add to favourites" does)."""
    return {"title": title or "Playlist", "type": "media", "path": path}


def remove_request(fav: dict) -> dict:
    """AddFavourite params that match an existing favourite (so Kodi removes it)."""
    p = {"title": fav.get("title") or "Favorite", "type": fav.get("type") or "window"}
    for key in ("path", "window", "windowparameter"):
        if fav.get(key):
            p[key] = fav[key]
    return p
