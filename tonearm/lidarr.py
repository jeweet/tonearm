"""Lidarr v1 API client (stdlib only)."""
from __future__ import annotations

import datetime as _dt
import http.client
import json
import os
import threading
import time
import unicodedata
import urllib.parse
from dataclasses import dataclass

MONITOR_OPTIONS = [
    ("all", "All albums"),
    ("future", "Future albums"),
    ("missing", "Missing albums"),
    ("existing", "Existing albums"),
    ("latest", "Latest album"),
    ("first", "First album"),
    ("none", "None"),
]


class LidarrError(Exception):
    pass


def norm_name(name: str) -> str:
    """Loose artist-name key: case/accents/punctuation/leading 'the' ignored."""
    s = unicodedata.normalize("NFKD", name or "")
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    s = s.replace("&", " and ")
    s = "".join(c if c.isalnum() else " " for c in s)
    words = s.split()
    if words and words[0] == "the":
        words = words[1:]
    return " ".join(words)


@dataclass
class LidarrSettings:
    enabled: bool = False
    url: str = ""
    api_key: str = ""
    root_folder: str = ""
    quality_profile: int = 0
    metadata_profile: int = 0
    monitor: str = "all"
    search_on_add: bool = True

    @classmethod
    def from_dict(cls, d: dict) -> "LidarrSettings":
        s = cls()
        for k in s.__dict__:
            if k in d:
                setattr(s, k, d[k])
        return s

    def to_dict(self, with_key: bool = True) -> dict:
        d = dict(self.__dict__)
        if not with_key:
            d.pop("api_key")
        return d

    @property
    def configured(self) -> bool:
        return self.enabled and bool(self.url) and bool(self.api_key)


class LidarrClient:
    def __init__(self, url: str, api_key: str, timeout: float = 15.0, cache_dir: str | None = None):
        url = url.strip().rstrip("/")
        if "://" not in url:
            url = "http://" + url
        self.base = url
        parts = urllib.parse.urlsplit(url)
        self.scheme = parts.scheme
        self.host = parts.hostname or ""
        self.port = parts.port or (443 if parts.scheme == "https" else 80)
        self.prefix = parts.path.rstrip("/")  # Lidarr behind a reverse proxy at /lidarr
        self.api_key = api_key.strip()
        self.timeout = timeout
        self._local = threading.local()
        self._artists: list[dict] | None = None
        self._artists_at = 0.0
        self._lock = threading.Lock()
        self.cache_dir = cache_dir
        if cache_dir:
            os.makedirs(cache_dir, exist_ok=True)

    # -- disk cache: pages show the last known lists instantly, then refresh ----------
    def _cache_file(self, name: str) -> str | None:
        return os.path.join(self.cache_dir, f"{name}.json") if self.cache_dir else None

    def _store(self, name: str, data):
        path = self._cache_file(name)
        if not path:
            return
        try:
            tmp = path + ".tmp"
            with open(tmp, "w") as f:
                json.dump({"at": time.time(), "base": self.base, "data": data}, f)
            os.replace(tmp, path)
        except OSError:
            pass

    def cached(self, name: str, max_age: float | None = None):
        """Last stored result of artists/queue/upcoming/missing, or None."""
        path = self._cache_file(name)
        if not path:
            return None
        try:
            with open(path) as f:
                d = json.load(f)
        except (OSError, ValueError):
            return None
        if d.get("base") != self.base:
            return None
        if max_age is not None and time.time() - d.get("at", 0) > max_age:
            return None
        return d.get("data")

    # -- transport -------------------------------------------------------
    def _conn(self):
        c = getattr(self._local, "conn", None)
        if c is None:
            cls = http.client.HTTPSConnection if self.scheme == "https" else http.client.HTTPConnection
            c = cls(self.host, self.port, timeout=self.timeout)
            self._local.conn = c
        return c

    def _drop(self):
        c = getattr(self._local, "conn", None)
        if c is not None:
            try:
                c.close()
            except Exception:  # noqa: BLE001
                pass
        self._local.conn = None

    def request(self, method: str, path: str, params: dict | None = None, body=None, raw=False):
        url = self.prefix + path
        if params:
            url += "?" + urllib.parse.urlencode(params, doseq=True)
        headers = {"X-Api-Key": self.api_key, "Accept": "application/json"}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        last = None
        for _ in range(2):
            conn = self._conn()
            try:
                conn.request(method, url, body=data, headers=headers)
                resp = conn.getresponse()
                payload = resp.read()
            except (OSError, http.client.HTTPException) as exc:
                last = exc
                self._drop()
                continue
            if resp.status == 401:
                raise LidarrError("Lidarr rejected the API key")
            if resp.status >= 400:
                msg = payload[:300].decode("utf-8", "replace")
                try:
                    j = json.loads(payload)
                    if isinstance(j, list) and j and isinstance(j[0], dict):
                        msg = "; ".join(e.get("errorMessage", "") for e in j if e.get("errorMessage"))
                    elif isinstance(j, dict):
                        msg = j.get("message") or j.get("error") or msg
                except ValueError:
                    pass
                raise LidarrError(f"Lidarr HTTP {resp.status}: {msg}")
            if raw:
                return payload
            return json.loads(payload) if payload else None
        raise LidarrError(f"Cannot reach Lidarr at {self.base}: {last}")

    def get(self, path, **params):
        return self.request("GET", "/api/v1" + path, params or None)

    def post(self, path, body):
        return self.request("POST", "/api/v1" + path, body=body)

    def put(self, path, body):
        return self.request("PUT", "/api/v1" + path, body=body)

    # -- API -------------------------------------------------------------
    def status(self) -> dict:
        return self.get("/system/status")

    def artists(self, max_age: float = 300) -> list[dict]:
        with self._lock:
            if self._artists is not None and time.monotonic() - self._artists_at < max_age:
                return self._artists
        if self._artists is None and max_age > 0:
            disk = self.cached("artists", max_age=max_age)
            if disk is not None:
                with self._lock:
                    self._artists, self._artists_at = disk, time.monotonic()
                return disk
        arts = self.get("/artist") or []
        with self._lock:
            self._artists, self._artists_at = arts, time.monotonic()
        self._store("artists", arts)
        return arts

    def invalidate(self):
        with self._lock:
            self._artists = None

    def artist_by_mbid(self, mbid: str) -> dict | None:
        return next((a for a in self.artists() if a.get("foreignArtistId") == mbid), None)

    def lookup(self, term: str) -> list[dict]:
        """Search MusicBrainz through Lidarr. Items already in Lidarr carry an id."""
        res = self.get("/artist/lookup", term=term) or []
        known = {a.get("foreignArtistId"): a for a in self.artists()}
        for r in res:
            mine = known.get(r.get("foreignArtistId"))
            if mine is not None:
                r["id"] = mine["id"]
                r["monitored"] = mine.get("monitored")
                r["statistics"] = mine.get("statistics")
            elif not r.get("id"):
                r["id"] = 0
        return res

    def lookup_mbid(self, mbid: str) -> dict | None:
        res = self.lookup(f"lidarr:{mbid}")
        return res[0] if res else None

    def artist(self, artist_id: int) -> dict:
        return self.get(f"/artist/{artist_id}")

    def albums(self, artist_id: int) -> list[dict]:
        return self.get("/album", artistId=artist_id) or []

    def root_folders(self) -> list[dict]:
        return self.get("/rootfolder") or []

    def quality_profiles(self) -> list[dict]:
        return self.get("/qualityprofile") or []

    def metadata_profiles(self) -> list[dict]:
        return self.get("/metadataprofile") or []

    def add_artist(self, lookup_item: dict, settings: LidarrSettings,
                   monitor: str | None = None, search: bool | None = None) -> dict:
        if not settings.root_folder or not settings.quality_profile or not settings.metadata_profile:
            raise LidarrError("Pick a root folder, quality and metadata profile in Preferences › Lidarr")
        body = {k: v for k, v in lookup_item.items() if k not in ("id", "statistics")}
        monitor = monitor or settings.monitor
        body.update({
            "qualityProfileId": int(settings.quality_profile),
            "metadataProfileId": int(settings.metadata_profile),
            "rootFolderPath": settings.root_folder,
            "monitored": monitor != "none",
            "monitorNewItems": "all" if monitor in ("all", "future") else "none",
            "addOptions": {
                "monitor": monitor,
                "searchForMissingAlbums": settings.search_on_add if search is None else search,
            },
        })
        res = self.post("/artist", body)
        self.invalidate()
        return res

    def command(self, name: str, **kw) -> dict:
        return self.post("/command", {"name": name, **kw})

    def search_artist(self, artist_id: int):
        return self.command("ArtistSearch", artistId=artist_id)

    def set_monitored(self, artist: dict, monitored: bool) -> dict:
        body = dict(artist)
        body["monitored"] = monitored
        res = self.put(f"/artist/{artist['id']}", body)
        self.invalidate()
        return res

    def queue(self) -> list[dict]:
        res = self.get("/queue", page=1, pageSize=100, includeArtist="true", includeAlbum="true")
        out = (res or {}).get("records", []) if isinstance(res, dict) else (res or [])
        self._store("queue", out)
        return out

    def missing(self, page_size: int = 100) -> list[dict]:
        res = self.get("/wanted/missing", page=1, pageSize=page_size, includeArtist="true",
                       sortKey="releaseDate", sortDirection="descending", monitored="true")
        out = (res or {}).get("records", [])
        self._store("missing", out)
        return out

    def upcoming(self, days: int = 90) -> list[dict]:
        start = _dt.date.today() - _dt.timedelta(days=7)
        end = _dt.date.today() + _dt.timedelta(days=days)
        res = self.get("/calendar", start=start.isoformat(), end=end.isoformat(),
                       includeArtist="true", unmonitored="false")
        out = sorted(res or [], key=lambda a: a.get("releaseDate") or "")
        self._store("upcoming", out)
        return out

    def image_bytes(self, url: str) -> bytes:
        """Fetch a Lidarr-hosted image (MediaCover path)."""
        if url.startswith(self.prefix + "/") and self.prefix:
            url = url[len(self.prefix):]
        return self.request("GET", url, raw=True)


def _local_cover(url: str) -> str:
    """Lidarr's own cached copy. Some setups report the on-disk path
    (/config/MediaCover/…); the web server serves it at /MediaCover/…."""
    if not url:
        return ""
    i = url.find("/MediaCover/")
    return "lidarr:" + (url[i:] if i >= 0 else url)


def best_image(item: dict, kinds=("poster", "cover", "fanart", "banner", "logo")) -> str:
    """Artwork-cache key for a Lidarr item: its local copy first (Lidarr serves it even
    when the remote image cache has gone), then the remote URL, joined by "|"."""
    imgs = item.get("images") or []
    ordered = [im for kind in kinds for im in imgs if (im.get("coverType") or "").lower() == kind]
    if not ordered and "fanart" not in kinds:
        ordered = list(imgs)
    for im in ordered:
        alts = [a for a in (_local_cover(im.get("url") or ""), im.get("remoteUrl") or "") if a]
        if alts:
            return "|".join(alts)
    return ""


def album_progress(album: dict) -> tuple[int, int]:
    st = album.get("statistics") or {}
    return int(st.get("trackFileCount") or 0), int(st.get("totalTrackCount") or st.get("trackCount") or 0)
