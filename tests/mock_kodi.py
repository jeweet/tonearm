"""A tiny fake Kodi JSON-RPC server for tests and UI development.

Run standalone:  python3 -m tests.mock_kodi --port 8080 --albums 300
"""
from __future__ import annotations

import argparse
import json
import random
import time
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class FakeLibrary:
    def __init__(self, n_albums=20, songs_per_album=10, seed=1, api_major=13):
        rnd = random.Random(seed)
        self.api_major = api_major
        self.calls: list[str] = []
        self.player = {"active": False, "playing": False, "time": 0, "position": 0,
                       "shuffled": False, "repeat": "off"}
        self.queue: list[int] = []
        self.favourites: list[dict] = [
            {"title": "Radio Paradise", "type": "media", "path": "http://stream.example/rp.flac"},
            {"title": "Settings", "type": "window", "window": "settings", "windowparameter": ""},
        ]
        self.windows: list = []
        self.volume = 80
        self.settings = {   # a few of Kodi's (Settings.GetSettings shape)
            "audiooutput.audiodevice": {"id": "audiooutput.audiodevice", "type": "string",
                                        "value": "PULSE:Default", "options": [
                                            {"label": "Speakers", "value": "PULSE:Default"},
                                            {"label": "Amplifier (HDMI)", "value": "ALSA:hdmi"}]},
            "musicplayer.crossfade": {"id": "musicplayer.crossfade", "type": "integer",
                                      "value": 0, "minimum": 0, "maximum": 15, "step": 1},
            "musicplayer.replaygaintype": {"id": "musicplayer.replaygaintype", "type": "integer",
                                           "value": 1, "options": [
                                               {"label": "None", "value": 0},
                                               {"label": "Album", "value": 1},
                                               {"label": "Track", "value": 2}]},
            "lookandfeel.skin": {"id": "lookandfeel.skin", "type": "string", "value": "x"},
        }
        self.slow_over = 0       # pages bigger than this sleep slow_delay seconds
        self.slow_delay = 0.0
        self.fail_after = None   # fail data pages after this many succeed
        self.artists, self.albums, self.songs = {}, {}, {}
        self.props = {k: "2026-01-01 10:00:00" for k in (
            "librarylastupdated", "librarylastcleaned", "artistlinksupdated",
            "songslastadded", "albumslastadded", "artistslastadded",
            "songsmodified", "albumsmodified", "artistsmodified")}
        self.props["missingartistid"] = 1
        words = ["Night", "Blue", "Echo", "Glass", "River", "Static", "Velvet", "Moon", "Paper", "Saint"]
        n_artists = max(1, n_albums // 3)
        for a in range(1, n_artists + 1):
            name = f"{rnd.choice(words)} {rnd.choice(words)}s {a}"
            self.artists[a] = {"artistid": a, "artist": name, "label": name, "genre": [],
                               "thumbnail": "", "fanart": "", "sortname": "", "dateadded": "2025-01-01 00:00:00",
                               "art": {"fanart": f"image://fanart-{a}.jpg/", "thumb": f"image://thumb-{a}.jpg/"}}
        sid = 1
        for i in range(1, n_albums + 1):
            self.add_album(i, rnd.randint(1, n_artists), f"{rnd.choice(words)} {rnd.choice(words)} {i}",
                           "2025-%02d-%02d 12:00:00" % (rnd.randint(1, 12), rnd.randint(1, 28)),
                           songs_per_album, sid)
            sid += songs_per_album

    def add_album(self, albumid, artistid, title, dateadded, n_songs, first_songid):
        ar = self.artists[artistid]["artist"]
        self.albums[albumid] = {"albumid": albumid, "title": title, "label": title,
                                "artist": [ar], "artistid": [artistid], "displayartist": ar,
                                "year": 2000 + albumid % 25, "genre": ["Rock"], "thumbnail": "",
                                "playcount": 0, "lastplayed": "", "userrating": 0,
                                "art": {"thumb": f"image://cover-{albumid}.jpg/",
                                        "artist.fanart": f"image://fanart-{artistid}.jpg/"},
                                "dateadded": dateadded, "datemodified": dateadded}
        for t in range(n_songs):
            s = first_songid + t
            self.songs[s] = {"songid": s, "title": f"Track {t + 1} of {title}", "label": "x",
                             "artist": [ar], "displayartist": ar, "albumid": albumid,
                             "track": t + 1, "disc": 1, "duration": 200 + t, "userrating": 0,
                             "file": f"/music/{albumid}/{t + 1:02d}.flac",
                             "playcount": 0, "lastplayed": "", "genre": ["Rock"],
                             "year": 2000 + albumid % 25,
                             "dateadded": dateadded, "datemodified": dateadded}

    def _add_to_queue(self, it):
        if "albumid" in it:
            self.queue += sorted(k for k, v in self.songs.items() if v["albumid"] == it["albumid"])
        elif "songid" in it:
            self.queue.append(it["songid"])
        elif "genreid" in it:
            self.queue += sorted(self.songs)[:10]
        elif "file" in it:
            self.queue += sorted(self.songs)[:5]
        elif "directory" in it:
            self.added_dirs = getattr(self, "added_dirs", []) + [it["directory"]]
            self.queue += sorted(self.songs)[:7]

    def touch(self, *keys, ts="2026-06-01 10:00:00"):
        for k in keys:
            self.props[k] = ts

    # -- JSON-RPC --------------------------------------------------------
    def handle(self, req):
        method, params = req.get("method"), req.get("params") or {}
        self.calls.append(method)
        try:
            result = self.dispatch(method, params)
            return {"jsonrpc": "2.0", "id": req.get("id"), "result": result}
        except KeyError as e:
            return {"jsonrpc": "2.0", "id": req.get("id"),
                    "error": {"code": -32602, "message": f"Invalid params: {e}"}}

    def _project(self, item, props, idkey):
        out = {idkey: item[idkey], "label": item.get("label") or item.get("title") or item.get("artist")}
        for p in props:
            if p not in item:
                raise KeyError(p)
            out[p] = item[p]
        return out

    def _filter(self, items, flt):
        if not flt:
            return items

        def ok(it, f):
            if "albumid" in f:
                return it.get("albumid") == f["albumid"]
            if "or" in f:
                return any(ok(it, g) for g in f["or"])
            if "and" in f:
                return all(ok(it, g) for g in f["and"])
            if f["field"] in ("playcount", "userrating"):
                return it.get(f["field"], 0) > int(f["value"])
            if f["field"] not in ("dateadded", "datemodified"):
                raise KeyError(f["field"])
            return it.get(f["field"], "")[:10] > f["value"][:10]
        return [i for i in items if ok(i, flt)]

    def _list(self, store, key, idkey, params):
        props = params.get("properties", [])
        lim = params.get("limits") or {}
        if self.slow_over and props and lim.get("end", 0) - lim.get("start", 0) > self.slow_over:
            time.sleep(self.slow_delay)  # big pages are slow on this "Kodi"
        if self.fail_after is not None and props:
            self.fail_after -= 1
            if self.fail_after < 0:
                raise KeyError("simulated outage")
        items = self._filter(sorted(store.values(), key=lambda x: x[idkey]), params.get("filter"))
        sort = params.get("sort") or {}
        if sort.get("method") in ("lastplayed", "playcount", "userrating"):
            items.sort(key=lambda x: x.get(sort["method"]) or 0,
                       reverse=sort.get("order") == "descending")
        total = len(items)
        lim = params.get("limits") or {}
        start, end = lim.get("start", 0), lim.get("end", total)
        items = items[start:end]
        return {key: [self._project(i, props, idkey) for i in items],
                "limits": {"start": start, "end": start + len(items), "total": total}}

    def dispatch(self, method, p):
        if method == "JSONRPC.Version":
            return {"version": {"major": self.api_major, "minor": 5, "patch": 0}}
        if method == "JSONRPC.Ping":
            return "pong"
        if method == "Application.GetProperties":
            return {"name": "Kodi", "version": {"major": 21, "minor": 1, "tag": "stable"},
                    "volume": self.volume, "muted": False}
        if method == "AudioLibrary.GetProperties":
            if self.api_major < 10:
                raise KeyError("method")
            return {k: self.props[k] for k in p["properties"]}
        if method == "AudioLibrary.GetArtists":
            return self._list(self.artists, "artists", "artistid", p)
        if method == "AudioLibrary.GetAlbums":
            return self._list(self.albums, "albums", "albumid", p)
        if method == "AudioLibrary.GetSongs":
            return self._list(self.songs, "songs", "songid", p)
        if method == "AudioLibrary.GetAlbumDetails" and set(p.get("properties", [])) <= {
                "originaldate", "releasedate"}:
            a = self.albums[p["albumid"]]
            return {"albumdetails": {"albumid": a["albumid"], "label": a["title"],
                                     "originaldate": a.get("originaldate", ""),
                                     "releasedate": a.get("releasedate", "")}}
        if method == "AudioLibrary.GetAlbumDetails":
            return {"albumdetails": self._project(self.albums[p["albumid"]], p["properties"], "albumid")}
        if method == "AudioLibrary.GetSongDetails":
            return {"songdetails": self._project(self.songs[p["songid"]], p["properties"], "songid")}
        if method == "AudioLibrary.SetSongDetails":
            self.songs[p["songid"]]["userrating"] = p["userrating"]
            return "OK"
        if method == "AudioLibrary.SetAlbumDetails":
            self.albums[p["albumid"]]["userrating"] = p["userrating"]
            return "OK"
        if method == "AudioLibrary.GetArtistDetails":
            return {"artistdetails": self._project(self.artists[p["artistid"]], p.get("properties", []), "artistid")}
        if method == "Player.GetActivePlayers":
            return [{"playerid": 0, "type": "audio"}] if self.player["active"] else []
        if method == "Player.GetProperties":
            pl = self.player
            t = int(pl["time"])
            return {"speed": 1 if pl["playing"] else 0, "position": pl["position"],
                    "time": {"hours": 0, "minutes": t // 60, "seconds": t % 60, "milliseconds": 0},
                    "totaltime": {"hours": 0, "minutes": 3, "seconds": 20, "milliseconds": 0},
                    "shuffled": pl["shuffled"], "repeat": pl["repeat"], "playlistid": 0,
                    "partymode": pl.get("partymode", False)}
        if method == "Player.GetItem":
            if not 0 <= self.player["position"] < len(self.queue):
                return {"item": {"type": "unknown", "label": getattr(self, "playing_file", "")}}
            sid = self.queue[self.player["position"]]
            s = self.songs[sid]
            a = self.albums[s["albumid"]]
            return {"item": {"id": sid, "type": "song", "title": s["title"], "artist": s["artist"],
                             "album": a["title"], "albumid": a["albumid"], "artistid": a["artistid"],
                             "duration": s["duration"], "thumbnail": "", "track": s["track"]}}
        if method == "Playlist.GetProperties":
            return {"size": len(self.queue)}
        if method == "AudioLibrary.GetGenres":
            return {"genres": [{"genreid": 7, "label": "Rock"}]}
        if method == "Files.GetSources":
            return {"sources": [{"file": "/music/", "label": "Music"},
                                {"file": "smb://nas/Downloads/", "label": "Downloads"},
                                {"file": "addons://sources/audio/", "label": "Music add-ons"}]}
        if method == "Files.GetDirectory":
            d = p["directory"]
            if d == "/music/":
                return {"files": [
                    {"file": "/music/Zebra/", "label": "Zebra", "filetype": "directory",
                     "type": "unknown"},
                    {"file": "/music/b.flac", "label": "b.flac", "filetype": "file",
                     "type": "song", "id": 2, "title": "Bee", "artist": ["X"], "duration": 90,
                     "size": 2000000, "lastmodified": "2026-01-02 10:00:00"},
                    {"file": "/music/a.mp3", "label": "a.mp3", "filetype": "file",
                     "type": "unknown", "title": "", "artist": [], "duration": 120,
                     "size": 1000000, "lastmodified": "2026-01-01 10:00:00"},
                    {"file": "/music/mix.m3u", "label": "mix", "filetype": "file"}]}
            if d == "special://musicplaylists/":
                return {"files": [
                    {"file": "special://profile/playlists/music/Party.xsp", "label": "Party",
                     "filetype": "directory", "type": "unknown"},
                    {"file": "special://profile/playlists/music/Road trip.m3u", "label": "Road trip",
                     "filetype": "file", "type": "unknown"},
                    {"file": "special://profile/playlists/music/readme.txt", "label": "readme",
                     "filetype": "file"}]}
            songs = sorted(self.songs)[:5]
            return {"files": [{"file": f"/music/{s}.flac", "label": self.songs[s]["title"],
                               "title": self.songs[s]["title"], "artist": self.songs[s]["artist"],
                               "duration": self.songs[s]["duration"], "type": "song", "id": s}
                              for s in songs]}
        if method == "Playlist.Swap":
            a, b = p["position1"], p["position2"]
            self.queue[a], self.queue[b] = self.queue[b], self.queue[a]
            pos = self.player.get("position")
            if pos in (a, b):                      # Kodi keeps playing the moved track
                self.player["position"] = b if pos == a else a
            return "OK"
        if method == "Playlist.Clear":
            self.queue = []
            return "OK"
        if method == "Playlist.Add":
            its = p["item"] if isinstance(p["item"], list) else [p["item"]]
            for it in its:
                self._add_to_queue(it)
            return "OK"
        if method == "Player.Open" and "partymode" in p["item"]:   # Kodi's Party Mode
            self.partymode_source = p["item"]["partymode"]
            self.queue = list(self.songs)[:10]
            self.player.update(active=True, playing=True, time=0, position=0, partymode=True)
            return "OK"
        if method == "Player.SetPartymode":
            self.player["partymode"] = bool(p["partymode"])
            return "OK"
        if method == "Player.Open" and "file" in p["item"]:   # a stream / file favourite
            self.player.update(active=True, playing=True, time=0, position=-1, shuffled=False)
            self.playing_file = p["item"]["file"]
            return "OK"
        if method == "Player.Open":
            self.player.update(active=True, playing=True, time=0, partymode=False,
                               position=p["item"].get("position", 0), shuffled=bool(
                                   (p.get("options") or {}).get("shuffled")))
            return "OK"
        if method == "Player.PlayPause":
            self.player["playing"] = not self.player["playing"]
            return {"speed": 1 if self.player["playing"] else 0}
        if method == "Player.GoTo":
            to = p["to"]
            pos = self.player["position"]
            self.player["position"] = (pos + 1 if to == "next" else pos - 1 if to == "previous" else to) % len(self.queue)
            self.player["time"] = 0
            return "OK"
        if method == "Player.Seek":
            v = p["value"].get("time", p["value"])
            self.player["time"] = v["hours"] * 3600 + v["minutes"] * 60 + v["seconds"]
            return {}
        if method == "Player.Stop":
            self.player.update(active=False, playing=False)
            return "OK"
        if method == "Player.SetShuffle":
            self.player["shuffled"] = p["shuffle"]
            return "OK"
        if method == "Player.SetRepeat":
            self.player["repeat"] = p["repeat"]
            return "OK"
        if method == "Application.SetVolume":
            self.volume = p["volume"]
            return self.volume
        if method == "Favourites.GetFavourites":
            props = (p or {}).get("properties") or []
            return {"favourites": [{k: v for k, v in f.items()
                                    if k in ("title", "type") or k in props} for f in self.favourites]}
        if method == "Favourites.AddFavourite":
            key = (p["type"], p.get("path", ""), p.get("window", ""), p.get("windowparameter", ""))
            same = [f for f in self.favourites if (f["type"], f.get("path", ""), f.get("window", ""),
                                                   f.get("windowparameter", "")) == key]
            if same:   # Kodi toggles
                self.favourites.remove(same[0])
            else:
                self.favourites.append(dict(p))
            return "OK"
        if method == "GUI.ActivateWindow":
            self.windows.append((p["window"], p.get("parameters")))
            return "OK"
        if method == "Settings.GetSettings":
            return {"settings": list(self.settings.values())}
        if method == "Settings.GetSettingValue":
            if p["setting"] == "audiooutput.passthrough":
                return {"value": False}
            raise KeyError(method)
        if method == "Settings.SetSettingValue":
            self.settings[p["setting"]]["value"] = p["value"]
            return True
        if method == "System.GetProperties":
            return {"canshutdown": True, "cansuspend": False, "canhibernate": False,
                    "canreboot": True}
        if method == "XBMC.GetInfoLabels":
            vals = {"Network.MacAddress": "aa:bb:cc:dd:ee:ff", "MusicPlayer.Codec": "flac",
                    "MusicPlayer.SampleRate": "96", "MusicPlayer.BitsPerSample": "24",
                    "MusicPlayer.BitRate": "2800", "MusicPlayer.Channels": "2"}
            return {k: vals.get(k, "") for k in p["labels"]}
        if method == "Playlist.GetItems":
            return {"items": [{"id": s, "type": "song", "title": self.songs[s]["title"],
                               "artist": self.songs[s]["artist"], "duration": self.songs[s]["duration"]}
                              for s in self.queue]}
        raise KeyError(method)


def serve(lib: FakeLibrary, port=0):
    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def do_POST(self):
            raw = self.rfile.read(int(self.headers["Content-Length"]))
            if len(raw) > 20000:          # like Kodi's web server
                self.send_response(413)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            body = json.loads(raw)
            if isinstance(body, list):
                out = [lib.handle(r) for r in body]
            else:
                out = lib.handle(body)
            data = json.dumps(out).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    srv.handle_error = lambda *a: None  # clients that time out on purpose aren't errors
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--albums", type=int, default=200)
    a = ap.parse_args()
    s = serve(FakeLibrary(a.albums), a.port)
    print(f"Fake Kodi on http://127.0.0.1:{a.port}/jsonrpc (Ctrl+C to stop)")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        s.shutdown()
