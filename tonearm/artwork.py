"""On-disk + in-memory artwork cache.

Originals are downloaded once from Kodi's /image/ endpoint, then scaled copies
are kept: ``small`` (grids, lists), ``large`` (album header, player, MPRIS/lock
screen) and ``wide`` (fanart backdrops). Everything after the first download is
read from disk.

Loading from disk and downloading use separate worker queues, so covers that are
already cached never wait behind slow downloads. Downloads are served newest
first, and requests for tiles that scrolled away are cancelled.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import urllib.request
from collections import OrderedDict

import gi

gi.require_version("Gdk", "4.0")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Gdk, GdkPixbuf, GLib  # noqa: E402

from .workers import DaemonPool  # noqa: E402
from .logger import log  # noqa: E402

SIZES = {"tiny": 40, "small": 256, "large": 800, "wide": 1600}
MISSING_TTL = 3 * 24 * 3600  # retry images that 404'd after three days


class ArtworkCache:
    def __init__(self, root: str, client=None, memory_items: int = 400):
        self.root = root
        self.client = client
        self.lidarr = None  # LidarrClient, for lidarr:/MediaCover/... keys
        self._disk = DaemonPool(3, "tonearm-art-disk", lifo=True)
        self._net = DaemonPool(6, "tonearm-art-net", lifo=True)
        self._mem: OrderedDict = OrderedDict()
        self._mem_max = memory_items
        self._pending: dict = {}
        self._lock = threading.Lock()
        self._failed: dict = {}  # key -> time of a transient failure (retried after a minute)
        for d in ("orig", *SIZES):
            os.makedirs(os.path.join(root, d), exist_ok=True)
        # Images that don't exist (HTTP 404) are remembered on disk for a while, so
        # they aren't requested again on every start.
        self._missing_path = os.path.join(root, "missing.json")
        self._missing: dict = {}
        try:
            with open(self._missing_path) as f:
                now = time.time()
                self._missing = {k: t for k, t in json.load(f).items() if now - t < MISSING_TTL}
        except (OSError, ValueError):
            pass
        self._missing_dirty = False

    @staticmethod
    def _key(kodi_path: str) -> str:
        return hashlib.sha1(kodi_path.encode()).hexdigest()

    def file_for(self, kodi_path: str, size: str = "large") -> str:
        """Path of the scaled copy (JPEG normally, PNG for images with transparency)."""
        base = os.path.join(self.root, size, self._key(kodi_path))
        for ext in (".jpg", ".png"):
            if os.path.exists(base + ext):
                return base + ext
        return base + ".jpg"

    def cached_file(self, kodi_path: str, size: str = "large") -> str | None:
        if not kodi_path:
            return None
        p = self.file_for(kodi_path, size)
        return p if os.path.exists(p) else None

    def load(self, kodi_path: str, size: str, callback):
        """Call ``callback(texture or None)`` on the main loop.

        Returns a ticket for :meth:`cancel` (or None when answered immediately)."""
        if not kodi_path:
            callback(None)
            return None
        key = (kodi_path, size)
        tex = self._mem.get(key)
        if tex is not None:
            self._mem.move_to_end(key)
            callback(tex)
            return None
        failed_at = self._failed.get(key)
        if kodi_path in self._missing or (failed_at and time.monotonic() - failed_at < 60):
            callback(None)
            return None
        with self._lock:
            if key in self._pending:
                self._pending[key].append(callback)
                return key, callback
            self._pending[key] = [callback]
        on_disk = os.path.exists(self.file_for(kodi_path, size))
        (self._disk if on_disk else self._net).submit(self._work, kodi_path, size, key)
        return key, callback

    def cancel(self, ticket):
        """The widget no longer needs this image (e.g. a recycled grid tile)."""
        if not ticket:
            return
        key, cb = ticket
        with self._lock:
            cbs = self._pending.get(key)
            if cbs and cb in cbs:
                cbs.remove(cb)
                if not cbs:
                    del self._pending[key]  # the worker sees this and skips the work

    def ensure_file(self, kodi_path: str, size: str = "large") -> str | None:
        """Blocking: make sure the scaled file exists; returns its path."""
        if not kodi_path:
            return None
        out = self.file_for(kodi_path, size)
        if os.path.exists(out):
            return out
        orig = os.path.join(self.root, "orig", self._key(kodi_path))
        if not os.path.exists(orig):
            os.makedirs(os.path.dirname(orig), exist_ok=True)
            data = self._fetch(kodi_path)
            if data is None:
                return None
            tmp = orig + ".part"
            with open(tmp, "wb") as f:
                f.write(data)
            os.replace(tmp, orig)
        px = SIZES[size]
        os.makedirs(os.path.dirname(out), exist_ok=True)
        pix = GdkPixbuf.Pixbuf.new_from_file_at_scale(orig, px, px, True)
        if pix.get_has_alpha():
            out = out[:-4] + ".png"
            fmt, keys, vals = "png", [], []
        else:
            out = out[:-4] + ".jpg"
            fmt, keys, vals = "jpeg", ["quality"], ["90"]
        tmp = out + ".part"
        pix.savev(tmp, fmt, keys, vals)
        os.replace(tmp, out)
        return out

    def _fetch(self, key: str) -> bytes | None:
        if "|" in key:  # alternatives, best first (e.g. Lidarr's local copy, then remote)
            last = None
            for alt in key.split("|"):
                try:
                    data = self._fetch(alt)
                    if data:
                        return data
                except Exception as exc:  # noqa: BLE001
                    last = exc
            if last is not None:
                raise last
            return None
        if key.startswith(("http://", "https://")):
            req = urllib.request.Request(key, headers={"User-Agent": "Tonearm/0.3"})
            with urllib.request.urlopen(req, timeout=20) as resp:
                return resp.read()
        if key.startswith("lidarr:"):
            if self.lidarr is None:
                return None
            return self.lidarr.image_bytes(key[len("lidarr:"):])
        if self.client is None:
            return None
        return self.client.fetch_bytes(self.client.image_url(key))

    def _work(self, kodi_path, size, key):
        with self._lock:
            if key not in self._pending:
                return  # nobody wants it any more (scrolled away)
        tex = None
        try:
            path = self.ensure_file(kodi_path, size)
            if path:
                tex = Gdk.Texture.new_from_filename(path)
        except Exception as exc:  # noqa: BLE001 — missing art is normal
            log.debug("Artwork %s %r: %s", size, kodi_path, exc)
            if "404" in str(exc):
                with self._lock:
                    self._missing[kodi_path] = time.time()
                    self._missing_dirty = True
                GLib.timeout_add_seconds(5, self._save_missing)
            tex = None
        if tex is not None:
            log.debug("Artwork %s ready: %r", size, kodi_path[:60])
        GLib.idle_add(self._deliver, key, tex)

    def _deliver(self, key, tex):
        with self._lock:
            cbs = self._pending.pop(key, [])
        if tex is None:
            self._failed[key] = time.monotonic()  # e.g. Kodi busy: try again in a minute
        else:
            self._mem[key] = tex
            while len(self._mem) > self._mem_max:
                self._mem.popitem(last=False)
        for cb in cbs:
            try:
                cb(tex)
            except Exception:  # noqa: BLE001
                import traceback
                traceback.print_exc()
        return GLib.SOURCE_REMOVE

    def _save_missing(self):
        with self._lock:
            if not self._missing_dirty:
                return GLib.SOURCE_REMOVE
            self._missing_dirty = False
            data = dict(self._missing)
        try:
            tmp = self._missing_path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(data, f)
            os.replace(tmp, self._missing_path)
        except OSError:
            pass
        return GLib.SOURCE_REMOVE

    def shutdown(self):
        self._disk.shutdown()
        self._net.shutdown()

    def size_bytes(self) -> int:
        total = 0
        for dirpath, _, files in os.walk(self.root):
            for f in files:
                try:
                    total += os.path.getsize(os.path.join(dirpath, f))
                except OSError:
                    pass
        return total

    def prune(self, limit_bytes: int):
        """Drop least-recently-modified originals until under the limit."""
        entries = []
        for dirpath, _, files in os.walk(self.root):
            for f in files:
                p = os.path.join(dirpath, f)
                try:
                    st = os.stat(p)
                except OSError:
                    continue
                entries.append((st.st_mtime, st.st_size, p))
        total = sum(e[1] for e in entries)
        for _, size, p in sorted(entries):
            if total <= limit_bytes:
                break
            try:
                os.remove(p)
                total -= size
            except OSError:
                pass

    def clear(self):
        self._mem.clear()
        self._failed.clear()
        self.prune(0)
