"""Connection to one Kodi instance: library cache, sync, player state, commands.

Everything network-bound runs on worker threads; signals are emitted on the
GTK main loop.
"""
from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, field, replace

from gi.repository import GLib, GObject

from .artwork import ArtworkCache
from .cache import LibraryCache
from .config import Config, cache_dir
from .fuzzy import FuzzyIndex
from .kodi import (INVALID_PARAMS, Instance, KodiClient, KodiConnectionError, KodiError,
                   KodiTimeout, NotificationListener)
from .lidarr import LidarrClient, norm_name
from .sync import LibrarySync, SyncCancelled, SyncResult
from .util import idle, run_async
from . import audioinfo, favourites, volumebridge, wol
from .playlists import PlaylistStore
from .sleeptimer import SleepTimer
from .volumebridge import VolumeBridge
from .workers import DaemonPool
from .logger import log

AUDIO_PLAYLIST = 0
ITEM_PROPS = ["title", "artist", "album", "albumid", "artistid", "duration", "thumbnail", "track"]
PLAYER_PROPS = ["speed", "time", "totaltime", "position", "shuffled", "repeat", "playlistid",
                "partymode"]

# Module-level scope predicates so the fuzzy index can reuse narrowing results.
SCOPES = {
    "all": None,
    "all_short": lambda e: e[0] != "song",  # 1–2 letters: songs would just be noise (and slow)
    "artist": lambda e: e[0] == "artist",
    "album": lambda e: e[0] == "album",
    "song": lambda e: e[0] == "song",
}
KIND_BONUS = {"artist": 10, "album": 5, "song": 0}


@dataclass
class PlayerState:
    active: bool = False
    playing: bool = False
    playerid: int = -1
    songid: int = 0
    title: str = ""
    artist: str = ""
    album: str = ""
    albumid: int = 0
    artistid: list = field(default_factory=list)
    thumbnail: str = ""
    duration: float = 0.0
    position: float = 0.0          # seconds at `stamp`
    stamp: float = 0.0             # time.monotonic() when position was read
    queue_position: int = -1
    queue_size: int = 0
    shuffle: bool = False
    repeat: str = "off"            # off | one | all
    volume: int = 100
    muted: bool = False
    partymode: bool = False        # Kodi's Party Mode keeps adding random songs

    def now(self) -> float:
        if not self.playing:
            return self.position
        pos = self.position + (time.monotonic() - self.stamp)
        return min(pos, self.duration) if self.duration else pos

    def track_key(self):
        return (self.songid, self.title, self.album, self.thumbnail)


def _secs(t: dict | None) -> float:
    if not t:
        return 0.0
    return t.get("hours", 0) * 3600 + t.get("minutes", 0) * 60 + t.get("seconds", 0) + t.get("milliseconds", 0) / 1000


def _time_obj(seconds: float) -> dict:
    seconds = max(0, int(seconds))
    return {"hours": seconds // 3600, "minutes": seconds % 3600 // 60, "seconds": seconds % 60, "milliseconds": 0}


class Session(GObject.Object):
    __gtype_name__ = "TonearmSession"

    __gsignals__ = {
        "instance-changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "library-changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "player-changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "track-changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "seeked": (GObject.SignalFlags.RUN_FIRST, None, (float,)),
        "queue-changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "sync-progress": (GObject.SignalFlags.RUN_FIRST, None, (str, float)),
        "sync-finished": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
        "toast": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        "lidarr-changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "favourites-changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "playlists-changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "sleep-changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "ratings-changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
        # (short message, full details) — shown as a toast with a Details button
        "problem": (GObject.SignalFlags.RUN_FIRST, None, (str, str)),
    }

    connected = GObject.Property(type=bool, default=False)
    live = GObject.Property(type=bool, default=False)       # TCP notifications up
    syncing = GObject.Property(type=bool, default=False)
    # Kodi didn't answer (or went away) — not just "not connected yet"; the window
    # then offers Wake-on-LAN
    offline = GObject.Property(type=bool, default=False)

    def __init__(self, config: Config):
        super().__init__()
        self.config = config
        self.instance: Instance | None = None
        self.client: KodiClient | None = None
        self.cache: LibraryCache | None = None
        self.artwork: ArtworkCache | None = None
        self.sync_engine: LibrarySync | None = None
        self.listener: NotificationListener | None = None
        self.state = PlayerState()
        self.kodi_info: dict = {}
        self._index: FuzzyIndex | None = None
        self._index_lock = threading.Lock()
        self._search_gen = 0
        self._poll_id = 0
        self._refresh_pending = 0
        self._sync_again = False
        self._lib_updates: dict = {}
        self._lib_update_id = 0
        self._generation = 0              # bumps on instance switch; drops stale results
        self.foreground = True
        self._stopped_at = 0.0
        self._confirming = False      # a "is Kodi still there?" check is running
        self._busy_until = 0.0        # our own heavy command is running: Kodi may lag
        self._radio_guard_until = 0.0  # a radio just started: its own stop event is no "stop"
        self._reconnect_id = 0
        self._sync_retry_id = 0
        self._wake_until = 0.0        # after a Wake-on-LAN: retry often until then
        self.power_options: set = set()   # what Kodi's system allows (POWER keys)
        # Kodi's user ratings (1–10) of songs and albums: {kind: {id: rating}}
        self.ratings: dict = {"song": {}, "album": {}}
        self._rating_checks: set = set()
        self._rating_check_id = 0
        self.connect("sync-finished", lambda *_: self.refresh_ratings())
        self._synced_once = False
        self._last_pos = None
        self._watch_key = None
        GLib.timeout_add_seconds(1, self._watchdog)
        # Phone volume keys → Kodi volume (opt-in, see volumebridge.py). Bridge calls
        # run in order on their own worker so start/push/stop never overtake each other.
        self._vb = VolumeBridge(lambda v, m: idle(self._on_bridge_change, v, m))
        self._vb_pool = DaemonPool(1, "tonearm-volume")
        self._vb_on = False
        self._vb_last = None
        self._radio = None
        # sleep timer: Tonearm stops Kodi itself (Kodi's API can't schedule it)
        self.sleep = SleepTimer()
        self.sleep_choice = "off"       # the menu choice ("30", "album", …)
        self._sleep_tick = 0
        self._sleep_volume = None       # the volume before a fade-out (to restore)
        self._sleep_next_album = None   # album of the next queue item ("album" mode)
        self.connect("track-changed", lambda *_: self._sleep_track_changed())
        self.favourites: list[dict] = []   # Kodi's Favourites, normalised (favourites.py)
        self.favourites_stale = False      # True: the cached copy (Kodi was unreachable)
        self.playlists: PlaylistStore | None = None   # Tonearm's own playlists
        if volumebridge.available():
            self._vb_pool.submit(self._vb.cleanup_leftovers)
        self.lidarr: LidarrClient | None = None
        self.lidarr_info: dict = {}
        self._kodi_names: dict | None = None
        self._lidarr_gen = 0
        self.configure_lidarr()

    # ------------------------------------------------------------------
    # instance lifecycle
    # ------------------------------------------------------------------
    def activate(self, inst: Instance | None):
        self._teardown()
        self._generation += 1
        self.instance = inst
        self.state = PlayerState()
        self.kodi_info = {}
        self._synced_once = False
        self.favourites = []
        self.playlists = None
        if inst is None:
            self.client = self.cache = self.artwork = self.sync_engine = None
            self.emit("playlists-changed")
            self.emit("instance-changed")
            self.emit("library-changed")
            self.emit("player-changed")
            return
        root = self.config.instance_cache_dir(inst.id)
        self.client = KodiClient(inst)
        self.cache = LibraryCache(os.path.join(root, "library.sqlite"))
        self.artwork = ArtworkCache(os.path.join(root, "art"), self.client)
        self.artwork.lidarr = self.lidarr
        self.sync_engine = LibrarySync(self.client, self.cache)
        self.kodi_info = self.cache.get_meta_json("kodi_info", {}) or {}
        self.playlists = PlaylistStore(os.path.join(root, "playlists.json"))
        self.emit("playlists-changed")
        self.ratings = {k: self.cache.ratings(k) for k in ("song", "album")}
        self.emit("ratings-changed")
        self._index = None
        # The cached library is usable immediately, even offline.
        self.emit("instance-changed")
        self.emit("library-changed")
        self.emit("player-changed")
        self._connect()
        limit = int(self.config.prefs.get("artwork_cache_mb", 400)) * 1024 * 1024
        art = self.artwork
        run_async(lambda: art.prune(limit))

    def shutdown(self):
        """Stop listeners/timers and close the cache; used when the app quits."""
        self._generation += 1
        self._vb_pool.shutdown()
        if self._vb.active:
            try:
                self._vb.stop()  # synchronously: give the phone its speaker back before exiting
            except Exception:  # noqa: BLE001
                pass
        self._teardown()
        if self.artwork is not None:
            self.artwork.shutdown()
        if self.cache is not None:
            try:
                self.cache.close()
            except Exception:  # noqa: BLE001
                pass
            self.cache = None

    def _teardown(self):
        if self._vb_on:  # leaving this Kodi: hand the volume keys back to the phone
            self._vb_on = False
            self._vb_pool.submit(self._vb_run, self._vb.stop)
        # A sync still running for the previous instance must not block the new one:
        # stop it, and clear the "syncing" state it would otherwise leave behind.
        if self.sync_engine is not None:
            self.sync_engine.cancel()
        self.props.syncing = False
        self._sync_again = False
        if self._sync_retry_id:
            GLib.source_remove(self._sync_retry_id)
            self._sync_retry_id = 0
        if self._reconnect_id:
            GLib.source_remove(self._reconnect_id)
            self._reconnect_id = 0
        for attr in ("_lib_update_id", "_rating_check_id"):
            if getattr(self, attr, 0):
                GLib.source_remove(getattr(self, attr))
                setattr(self, attr, 0)
        self._lib_updates = {}
        self._rating_checks = set()
        if self.listener:
            self.listener.stop()
            self.listener = None
        if self._poll_id:
            GLib.source_remove(self._poll_id)
            self._poll_id = 0
        self.props.connected = False
        self.props.offline = False
        self.props.live = False
        self.power_options = set()
        if self.cache:
            old = self.cache
            GLib.timeout_add_seconds(5, lambda: (old.close(), False)[1])

    def _connect(self):
        gen, client = self._generation, self.client

        def done(info):
            if gen != self._generation:
                return
            self.kodi_info = info
            if self.cache is not None:
                self.cache.set_meta("kodi_info", info)  # shown in Preferences when offline
            self.props.connected = True
            self.props.offline = False
            self._wake_until = 0.0
            self._offline_notified = False
            self._learn_mac(gen, client)
            self.refresh_ratings()
            self._fetch_power(gen, client)
            self.emit("instance-changed")
            if self.config.prefs.get("notifications", True) and self.listener is None:
                self._start_listener()
            self.refresh_player()
            self._schedule_poll()
            self.fetch_favourites()
            # Sync once per session (or if the last check is old) — not on every reconnect,
            # otherwise a flaky link turns into an endless series of syncs.
            last = float(self.cache.get_meta("last_check") or 0) if self.cache else 0
            if self.config.prefs.get("sync_on_connect", True) and \
                    (not self._synced_once or time.time() - last > 600):
                self._synced_once = True
                self.sync()

        def failed(exc):
            if gen != self._generation:
                return
            if isinstance(exc, KodiTimeout):
                # It's there but busy (e.g. scanning); try again soon, don't call it offline.
                self._schedule_reconnect(5)
                return
            self.props.connected = False
            self.props.offline = True
            if not getattr(self, "_offline_notified", False):
                self._offline_notified = True
                self._toast_problem(f"Offline — showing cached library ({exc})")
            # just woken: look every few seconds while it boots
            self._schedule_reconnect(5 if time.time() < self._wake_until else 15)

        run_async(client.info, done=done, error=failed)

    def _schedule_reconnect(self, seconds: int):
        """At most one pending reconnect attempt."""
        if self._reconnect_id:
            return
        gen = self._generation

        def fire():
            self._reconnect_id = 0
            if gen == self._generation and not self.props.connected:
                self._connect()
            return GLib.SOURCE_REMOVE

        self._reconnect_id = GLib.timeout_add_seconds(seconds, fire)

    def _toast_problem(self, message: str):
        """A toast about something that went wrong — also in the log, as a warning
        (other toasts — "Party Mode: Whole library" — are news, not problems)."""
        log.warning("%s", message)
        self.emit("toast", message)

    def reconnect(self):
        self.activate(self.instance)

    # -- ratings (Kodi's userrating, 1–10; 0 = none) ---------------------------
    RATING_METHODS = {"song": ("AudioLibrary.GetSongs", "songs", "songid",
                               "AudioLibrary.SetSongDetails", "AudioLibrary.GetSongDetails",
                               "songdetails"),
                      "album": ("AudioLibrary.GetAlbums", "albums", "albumid",
                                "AudioLibrary.SetAlbumDetails", "AudioLibrary.GetAlbumDetails",
                                "albumdetails")}

    def rating(self, kind: str, itemid: int) -> int:
        return self.ratings.get(kind, {}).get(itemid, 0)

    def refresh_ratings(self):
        """All rated songs and albums from Kodi (rated ones only: a few hundred at most,
        however big the library), into the cache."""
        client, cache, gen = self.client, self.cache, self._generation
        if client is None or cache is None or not self.props.connected:
            return

        def fetch(kind):
            method, key, idkey = self.RATING_METHODS[kind][:3]
            base = {"properties": ["userrating"],
                    "sort": {"method": "userrating", "order": "descending"}}
            try:
                res = client.call(method, {**base, "limits": {"start": 0, "end": 20000},
                                           "filter": {"field": "userrating",
                                                      "operator": "greaterthan", "value": "0"}})
                items = (res or {}).get(key) or []
            except KodiError:
                # no userrating filter: best rated first, page until the unrated start
                items, start = [], 0
                while True:
                    res = client.call(method, {**base, "limits": {"start": start,
                                                                  "end": start + 500}})
                    page = (res or {}).get(key) or []
                    items += page
                    if len(page) < 500 or not page or not page[-1].get("userrating"):
                        break
                    start += 500
            return {int(i[idkey]): int(i.get("userrating") or 0) for i in items
                    if int(i.get("userrating") or 0) > 0}

        def work():
            out = {kind: fetch(kind) for kind in ("song", "album")}
            for kind, got in out.items():
                cache.replace_ratings(kind, got)
            return out

        def done(out):
            if gen != self._generation:
                return
            if out != self.ratings:
                self.ratings = out
                self.emit("ratings-changed")

        run_async(work, done=done, error=lambda e: log.info("Ratings not fetched: %s", e))

    def rate(self, kind: str, itemid: int, rating: int):
        """Rate a song or album 1–10 (0 removes the rating) — here at once, on Kodi."""
        if kind not in self.RATING_METHODS or not itemid:
            return
        rating = max(0, min(10, int(rating)))
        client, cache = self.client, self.cache
        if client is None or not self.props.connected:
            self._toast_problem("Kodi is offline — ratings can't be changed now")
            self.emit("ratings-changed")       # put the stars back
            return
        before = self.rating(kind, itemid)
        self._store_rating(kind, itemid, rating)
        _m, _k, idkey, setter = self.RATING_METHODS[kind][:4]

        def failed(exc):
            self._store_rating(kind, itemid, before)
            self.emit("problem", "Kodi didn't take the rating", str(exc))

        run_async(lambda: client.call(setter, {idkey: itemid, "userrating": rating}),
                  error=failed)

    def _store_rating(self, kind, itemid, rating):
        table = self.ratings.setdefault(kind, {})
        if rating > 0:
            table[itemid] = rating
        else:
            table.pop(itemid, None)
        if self.cache is not None:
            self.cache.set_rating(kind, itemid, rating)
        self.emit("ratings-changed")

    def _check_rating(self, kind, itemid):
        """A song / album changed on Kodi: look at its rating (gathered for a moment)."""
        self._rating_checks.add((kind, itemid))
        if self._rating_check_id:
            return
        gen = self._generation

        def go():
            self._rating_check_id = 0
            checks, self._rating_checks = self._rating_checks, set()
            client = self.client
            if client is None or gen != self._generation:
                return GLib.SOURCE_REMOVE
            calls = []
            for kind, iid in checks:
                getter, idkey = self.RATING_METHODS[kind][4], self.RATING_METHODS[kind][2]
                calls.append((kind, iid, getter, idkey))

            def work():
                res = client.batch([(g, {k: i, "properties": ["userrating"]})
                                    for _kind, i, g, k in calls])
                out = []
                for (kind, iid, _g, _k), r in zip(calls, res):
                    if isinstance(r, dict) and not r.get("error"):
                        det = r.get(self.RATING_METHODS[kind][5]) or {}
                        out.append((kind, iid, int(det.get("userrating") or 0)))
                return out

            def done(out):
                if gen != self._generation:
                    return
                for kind, iid, rating in out:
                    if self.rating(kind, iid) != rating:
                        self._store_rating(kind, iid, rating)

            run_async(work, done=done, error=lambda e: log.debug("Rating check: %s", e))
            return GLib.SOURCE_REMOVE

        self._rating_check_id = GLib.timeout_add(1200, go)

    # -- power (Kodi's System.* calls) ---------------------------------------
    # key: (Kodi's "can…" property or None = always, JSON-RPC method)
    POWER = {"quit": (None, "Application.Quit"),
             "suspend": ("cansuspend", "System.Suspend"),
             "hibernate": ("canhibernate", "System.Hibernate"),
             "reboot": ("canreboot", "System.Reboot"),
             "shutdown": ("canshutdown", "System.Shutdown")}

    def _fetch_power(self, gen: int, client: KodiClient):
        props = [p for p, _m in self.POWER.values() if p]

        def done(res):
            if gen != self._generation:
                return
            self.power_options = {k for k, (p, _m) in self.POWER.items()
                                  if p is None or (res or {}).get(p)}
            self.emit("instance-changed")    # menus follow

        def failed(exc):
            log.debug("No power options from Kodi: %s", exc)
            if gen == self._generation:
                self.power_options = {"quit"}
                self.emit("instance-changed")

        run_async(lambda: client.call("System.GetProperties", {"properties": props}),
                  done=done, error=failed)

    def power(self, key: str):
        """Quit Kodi, or suspend / hibernate / reboot / shut down its machine."""
        if key not in self.POWER or self.client is None:
            return
        method = self.POWER[key][1]
        client, name = self.client, self.instance.name if self.instance else "Kodi"
        log.info("Power: %s on %s", method, name)

        def done(_r):
            msg = {"quit": f"Quitting Kodi on {name}", "suspend": f"Suspending {name}",
                   "hibernate": f"Hibernating {name}", "reboot": f"Restarting {name}",
                   "shutdown": f"Shutting down {name}"}[key]
            self.emit("toast", msg)

        def failed(exc):
            # the machine going away mid-call is success, not an error
            if isinstance(exc, (KodiConnectionError, KodiTimeout)):
                done(None)
                return
            self.emit("problem", "Kodi refused", str(exc))

        run_async(lambda: client.call(method, timeout=5), done=done, error=failed)

    # -- audio: the playing stream's format, Kodi's audio settings -------------
    def fetch_stream_info(self, callback):
        """``callback(text)``: "FLAC · 24-bit / 96 kHz" for what's playing ("" if unknown)."""
        client, gen = self.client, self._generation
        if client is None or not self.props.connected:
            callback("")
            return
        run_async(lambda: client.call("XBMC.GetInfoLabels", {"labels": audioinfo.LABELS}),
                  done=lambda labels: gen == self._generation and
                  callback(audioinfo.describe(labels)),
                  error=lambda _e: callback(""))

    # Kodi's own settings Tonearm offers (Preferences → Kodi Audio)
    AUDIO_SETTINGS = ["audiooutput.audiodevice", "audiooutput.passthrough",
                      "musicplayer.crossfade", "musicplayer.crossfadealbumtracks",
                      "musicplayer.replaygaintype"]

    def fetch_audio_settings(self, callback):
        """``callback(settings, error)``: {setting id: Kodi's setting dict} — with "value",
        "type", and "options" ([{label, value}]) or "minimum" / "maximum" / "step" —
        and an error text ("" when fine)."""
        client, gen = self.client, self._generation
        if client is None or not self.props.connected:
            callback({}, "offline")
            return

        def work():
            # all settings, picked out here: asking by section / category depends on
            # Kodi's internal names, which change between versions
            res = client.call("Settings.GetSettings", {"level": "expert"}, timeout=20)
            out = {st["id"]: st for st in (res or {}).get("settings", [])
                   if st.get("id") in self.AUDIO_SETTINGS}
            # anything not listed (older Kodi): at least its value
            for sid in self.AUDIO_SETTINGS:
                if sid not in out:
                    try:
                        v = client.call("Settings.GetSettingValue", {"setting": sid})
                    except KodiError:
                        continue
                    value = (v or {}).get("value")
                    if value is not None:
                        out[sid] = {"id": sid, "value": value, "type": (
                            "boolean" if isinstance(value, bool) else
                            "integer" if isinstance(value, int) else "string")}
            log.info("Kodi audio settings: %s", ", ".join(sorted(out)) or "none")
            return out

        def failed(exc):
            log.warning("Kodi's audio settings unavailable: %s", exc)
            if gen == self._generation:
                callback({}, str(exc))

        run_async(work, done=lambda res: gen == self._generation and callback(res, ""),
                  error=failed)

    def set_kodi_setting(self, setting: str, value, done=None):
        client = self.client
        if client is None:
            return

        def failed(exc):
            self.emit("problem", "Kodi didn't take the setting", str(exc))
            if done:
                done(False)

        run_async(lambda: client.call("Settings.SetSettingValue",
                                      {"setting": setting, "value": value}),
                  done=lambda _r: done and done(True), error=failed)

    # -- Wake-on-LAN -------------------------------------------------------
    def can_wake(self) -> bool:
        return bool(self.instance and wol.normalize_mac(self.instance.mac))

    def wake(self):
        """Send the Kodi machine a Wake-on-LAN packet, then look for it every few
        seconds for three minutes (while it boots and Kodi starts)."""
        inst = self.instance
        if not self.can_wake():
            return
        gen = self._generation

        def done(_sent):
            if gen != self._generation:
                return
            log.info("Wake-on-LAN sent to %s (%s)", inst.name, inst.mac)
            self.emit("toast", f"Waking {inst.name}… connecting as soon as it's up")
            self._wake_until = time.time() + 180
            if self._reconnect_id:          # look sooner than the 15 s retry
                GLib.source_remove(self._reconnect_id)
                self._reconnect_id = 0
            self._schedule_reconnect(5)

        def failed(exc):
            log.warning("Wake-on-LAN failed: %s", exc)
            self.emit("problem", "Couldn't send the wake-up", str(exc))

        run_async(lambda: wol.wake(inst.mac, inst.host), done=done, error=failed)

    def _learn_mac(self, gen: int, client: KodiClient):
        """Remember the Kodi machine's MAC address (for waking it later)."""
        inst = self.instance

        def done(mac):
            if gen != self._generation or not mac or inst is None or mac == inst.mac:
                return
            inst.mac = mac
            self.config.save()
            log.info("Learnt %s's MAC address for Wake-on-LAN", inst.name)

        run_async(client.mac_address, done=done, error=lambda exc: log.debug(
            "No MAC address from Kodi: %s", exc))

    def _start_listener(self):
        gen = self._generation

        def on_event(method, params):
            idle(self._on_event, gen, method, params)

        def on_state(up):
            idle(self._on_listener_state, gen, up)

        self.listener = NotificationListener(self.instance, on_event, on_state)
        self.listener.start()

    def _on_listener_state(self, gen, up):
        if gen != self._generation:
            return
        self.props.live = up
        self._schedule_poll()

    def set_foreground(self, fg: bool):
        self.foreground = fg
        self._schedule_poll()
        if fg:
            self.refresh_player()

    def _schedule_poll(self):
        if self._poll_id:
            GLib.source_remove(self._poll_id)
            self._poll_id = 0
        if not self.client:
            return
        # With live notifications we only need an occasional drift check.
        if self.props.live:
            interval = 10 if self.foreground else 30
        else:
            interval = 2 if self.foreground else 5
        self._poll_id = GLib.timeout_add_seconds(interval, self._poll)

    def _poll(self):
        self.refresh_player()
        return GLib.SOURCE_CONTINUE

    # ------------------------------------------------------------------
    # notifications
    # ------------------------------------------------------------------
    def _on_event(self, gen, method, params):
        if gen != self._generation:
            return
        data = (params or {}).get("data") or {}
        if method.startswith("Player."):
            if method == "Player.OnPause":
                self._update_state(playing=False, position=self.state.now(), stamp=time.monotonic())
            elif method in ("Player.OnResume",):
                self._update_state(playing=True, stamp=time.monotonic())
            elif method == "Player.OnStop":
                self._stopped_at = time.monotonic()
                # Starting a radio while something plays stops the old track; that stop
                # event can arrive after the radio has started and must not end it.
                if time.monotonic() > self._radio_guard_until:
                    self._radio = None
                self._apply_state(PlayerState(volume=self.state.volume, muted=self.state.muted))
            self._refresh_soon()
        elif method == "Application.OnVolumeChanged":
            self._update_state(volume=int(data.get("volume", self.state.volume)),
                               muted=bool(data.get("muted", self.state.muted)))
        elif method.startswith("Playlist."):
            if data.get("playlistid", AUDIO_PLAYLIST) == AUDIO_PLAYLIST:
                self.emit("queue-changed")
        elif method in ("AudioLibrary.OnUpdate", "AudioLibrary.OnRemove"):
            item = data.get("item") or data
            kind, iid = item.get("type"), item.get("id")
            if kind in ("song", "album", "artist") and iid:
                if kind in ("song", "album") and not method.endswith("Remove"):
                    self._check_rating(kind, iid)    # rated on Kodi (or by us)
                self._lib_updates[(kind, iid)] = method.endswith("Remove")
                if self._lib_update_id:
                    GLib.source_remove(self._lib_update_id)
                self._lib_update_id = GLib.timeout_add(1500, self._flush_lib_updates)
        elif method in ("AudioLibrary.OnScanFinished", "AudioLibrary.OnCleanFinished"):
            self.sync()
        elif method == "AudioLibrary.OnScanStarted":
            self.emit("toast", "Kodi started a library scan")
        elif method == "System.OnQuit":
            self.props.connected = False
            self.props.offline = True
            self._schedule_reconnect(15)

    def _flush_lib_updates(self):
        self._lib_update_id = 0
        updates, self._lib_updates = self._lib_updates, {}
        if len(updates) > 40 or self.props.syncing:
            self.sync()
            return GLib.SOURCE_REMOVE
        engine, gen = self.sync_engine, self._generation
        if engine is None:            # left this Kodi meanwhile
            return GLib.SOURCE_REMOVE

        def work():
            for (kind, iid), removed in updates.items():
                try:
                    (engine.apply_remove if removed else engine.apply_update)(kind, iid)
                except (KodiError, KodiConnectionError):
                    pass

        def done(_):
            if gen == self._generation:
                self._invalidate_index()
                self.emit("library-changed")

        run_async(work, done=done)
        return GLib.SOURCE_REMOVE

    # ------------------------------------------------------------------
    # library sync
    # ------------------------------------------------------------------
    def sync(self, full: bool = False, force: bool = False):
        """Bring the cache up to date. ``force``: user asked, so always check."""
        if not self.sync_engine:
            return
        if self.props.syncing:
            self._sync_again = True
            return
        self.props.syncing = True
        gen, engine = self._generation, self.sync_engine

        def progress(stage, frac):
            idle(lambda: gen == self._generation and self.emit(
                "sync-progress", stage, -1.0 if frac is None else float(frac)))

        def done(result: SyncResult):
            if gen != self._generation:
                return
            self.props.syncing = False
            if result.changed or result.full:
                self._invalidate_index()
                self.emit("library-changed")
            self.emit("sync-finished", result)
            if self._sync_again:
                self._sync_again = False
                self.sync()

        def failed(exc):
            if gen != self._generation or isinstance(exc, SyncCancelled):
                return
            self.props.syncing = False
            self.emit("sync-finished", exc)
            self._toast_problem(f"Sync paused: {exc}")
            # The sync resumes where it stopped; try again in a minute if Kodi is around.
            if isinstance(exc, KodiConnectionError) and not self._sync_retry_id:
                def retry():
                    self._sync_retry_id = 0
                    if gen == self._generation and self.props.connected:
                        self.sync()
                    return GLib.SOURCE_REMOVE
                self._sync_retry_id = GLib.timeout_add_seconds(60, retry)

        run_async(engine.run, full, progress, force or full, done=done, error=failed)

    def scan_library(self, directory: str = ""):
        """Kodi scans for new music — everywhere, or only in ``directory``."""
        if directory:
            self._cmd("AudioLibrary.Scan", {"directory": directory},
                      toast="Scanning this folder into the library on Kodi")
        else:
            self._cmd("AudioLibrary.Scan", {}, toast="Library scan started on Kodi")

    # -- folders (Kodi's music sources, browsed like files) --------------------
    FILE_PROPS = ["title", "artist", "album", "duration", "size", "lastmodified"]

    def fetch_sources(self, callback):
        """``callback(sources | None, error)``: Kodi's music sources [{file, label}];
        None with "offline" or what went wrong."""
        client = self.client
        if client is None or not self.props.connected:
            callback(None, "offline")
            return

        def keep(src: dict) -> bool:
            """Folders only: not Kodi's "Music add-ons" entry, nor its "Add music…"."""
            path = (src.get("file") or "").lower()
            return bool(path) and not path.startswith(("addons://", "plugin://")) \
                and path not in ("add", "source://add/")

        def work():
            try:
                res = client.call("Files.GetSources", {"media": "music"}, timeout=30)
                return [src for src in (res or {}).get("sources") or [] if keep(src)]
            except KodiError as err:
                # some Kodi versions refuse it: their "sources://" folder lists the same
                log.warning("Files.GetSources failed (%s); trying sources://music/", err)
                res = client.call("Files.GetDirectory", {"directory": "sources://music/",
                                                         "media": "music"}, timeout=30)
                return [{"file": f.get("file"), "label": f.get("label")}
                        for f in (res or {}).get("files") or []
                        if f.get("filetype", "directory") == "directory" and keep(f)]

        def failed(exc):
            log.warning("Music sources unavailable: %s", exc)
            callback(None, str(exc) or exc.__class__.__name__)

        run_async(work, done=lambda src: callback(src, ""), error=failed)

    def fetch_directory(self, path: str, callback):
        """``callback(entries | None, error)``: a folder's subfolders and music files
        (with tags when Kodi reads them); None when it can't be listed."""
        client = self.client
        if client is None or not self.props.connected:
            callback(None, "offline")
            return

        def work():
            params = {"directory": path, "media": "music", "properties": self.FILE_PROPS}
            try:
                res = client.call("Files.GetDirectory", params, timeout=60)
            except KodiError as err:
                if err.code != INVALID_PARAMS:
                    raise
                params.pop("properties")         # older Kodi: plain names
                res = client.call("Files.GetDirectory", params, timeout=60)
            return (res or {}).get("files") or []

        run_async(work, done=lambda files: callback(files, ""),
                  error=lambda e: callback(None, str(e)))

    def play_folder(self, path: str, shuffle: bool = False):
        """A folder and everything under it."""
        self._radio = None
        self._expect_busy(60)
        self._seq([
            ("Playlist.Clear", {"playlistid": AUDIO_PLAYLIST}),
            ("Playlist.Add", {"playlistid": AUDIO_PLAYLIST,
                              "item": {"directory": path, "recursive": True}}, 120),
            ("Player.Open", {"item": {"playlistid": AUDIO_PLAYLIST, "position": 0},
                             "options": {"shuffled": shuffle}}),
        ])

    # Kodi's web server refuses a request body over ~20 kB ("HTTP 413")
    MAX_ADD_BYTES = 12000

    @classmethod
    def file_batches(cls, files: list[str]) -> list[list[str]]:
        """``files`` split into Playlist.Add-sized groups, in order."""
        out, cur, size = [], [], 0
        for f in files:
            n = len(json.dumps({"file": f})) + 2
            if cur and size + n > cls.MAX_ADD_BYTES:
                out.append(cur)
                cur, size = [], 0
            cur.append(f)
            size += n
        if cur:
            out.append(cur)
        return out

    def play_files(self, files: list[str], position: int = 0):
        """These files in this order (a folder's songs), from ``position``."""
        if not files:
            return
        self._radio = None
        self._expect_busy(30 + len(files) // 10)
        adds = [("Playlist.Add", {"playlistid": AUDIO_PLAYLIST,
                                  "item": [{"file": f} for f in batch]}, 60)
                for batch in self.file_batches(files)]
        self._seq([("Playlist.Clear", {"playlistid": AUDIO_PLAYLIST}), *adds,
                   ("Player.Open", {"item": {"playlistid": AUDIO_PLAYLIST,
                                             "position": position}})])

    def clean_library(self):
        self._cmd("AudioLibrary.Clean", {"showdialogs": False}, toast="Library clean started on Kodi")

    # ------------------------------------------------------------------
    # fuzzy search
    # ------------------------------------------------------------------
    def _invalidate_index(self):
        with self._index_lock:
            self._index = None
            self._genre_ids = None
            self._kodi_names = None

    def _ensure_index(self) -> FuzzyIndex:
        with self._index_lock:
            if self._index is None and self.cache is not None:
                entries = self.cache.search_entries()
                self._index = FuzzyIndex(entries, key=lambda e: f"{e[2]}  {e[3]}")
            return self._index

    def warm_index(self):
        run_async(self._ensure_index)

    def search(self, query: str, scope: str, callback):
        """callback(list of (score, entry, positions)); entry=(kind,id,title,subtitle,thumb)."""
        self._search_gen += 1
        gen = self._search_gen
        query = query.strip()
        if not query or not self.cache:
            callback([])
            return

        def work():
            idx = self._ensure_index()
            if idx is None:
                return []
            if scope == "all" and len(query) < 3:
                eff = "all_short"
            else:
                eff = scope
            res = idx.search(query, 150, accept=SCOPES.get(eff),
                             cancelled=lambda: gen != self._search_gen)
            res.sort(key=lambda r: r[0] + KIND_BONUS[r[1][0]], reverse=True)
            return res

        run_async(work, done=lambda res: gen == self._search_gen and callback(res))

    # ------------------------------------------------------------------
    # player state
    # ------------------------------------------------------------------
    def _refresh_soon(self):
        if self._refresh_pending:
            return
        self._refresh_pending = GLib.timeout_add(150, self._refresh_now)

    def _refresh_now(self):
        self._refresh_pending = 0
        self.refresh_player()
        return GLib.SOURCE_REMOVE

    def refresh_player(self):
        if not self.client:
            return
        client, gen = self.client, self._generation

        def work():
            players = client.call("Player.GetActivePlayers") or []
            audio = next((p for p in players if p.get("type") == "audio"), None)
            vol = client.call("Application.GetProperties", {"properties": ["volume", "muted"]})
            if audio is None:
                return None, None, vol, time.monotonic()
            pid = audio["playerid"]
            props, item, pl = client.batch([
                ("Player.GetProperties", {"playerid": pid, "properties": PLAYER_PROPS}),
                ("Player.GetItem", {"playerid": pid, "properties": ITEM_PROPS}),
                ("Playlist.GetProperties", {"playlistid": AUDIO_PLAYLIST, "properties": ["size"]}),
            ])
            if isinstance(props, Exception):
                raise props
            size = pl.get("size") if isinstance(pl, dict) else None
            return pid, (props, item if not isinstance(item, Exception) else {}, size), vol, \
                time.monotonic()

        def done(res):
            if gen != self._generation:
                return
            pid, data, vol, stamp = res
            if not self.props.connected:
                self.props.connected = True
            st = PlayerState(volume=int(vol.get("volume", 100)), muted=bool(vol.get("muted")))
            size = None
            if pid is not None:
                props, item, size = data
                it = (item or {}).get("item") or {}
                st.active = True
                st.playerid = pid
                st.playing = props.get("speed", 0) != 0
                st.position = _secs(props.get("time"))
                st.stamp = stamp
                st.duration = _secs(props.get("totaltime")) or float(it.get("duration") or 0)
                st.queue_position = props.get("position", -1)
                st.queue_size = int(size or 0)
                st.shuffle = bool(props.get("shuffled"))
                st.repeat = props.get("repeat", "off")
                st.partymode = bool(props.get("partymode"))
                st.songid = int(it.get("id") or 0) if it.get("type") == "song" else 0
                st.title = it.get("title") or it.get("label") or ""
                st.artist = ", ".join(it.get("artist") or [])
                st.album = it.get("album") or ""
                st.albumid = int(it.get("albumid") or 0)
                st.artistid = it.get("artistid") or []
                st.thumbnail = it.get("thumbnail") or ""
            if st.active and self._queue_finished(st, size):
                # Kodi sometimes leaves the player parked on the last track after the
                # queue ends; release it so the bar, MPRIS and lock screen show "stopped".
                self._cmd("Player.Stop", {"playerid": st.playerid})
                st = PlayerState(volume=st.volume, muted=st.muted)
            self._apply_state(st)

        def failed(exc):
            if gen != self._generation or not isinstance(exc, KodiConnectionError):
                return
            if isinstance(exc, KodiTimeout) or self.props.syncing:
                return  # Kodi is busy (sync, scan); the next poll will catch up
            if time.monotonic() < self._busy_until:
                return  # we just gave Kodi a lot to do (radio, big queue): it lags, it's there
            if not self.props.connected or self._confirming:
                return  # already offline, or a check is under way: don't pile up pings
            self._confirming = True
            # One failed poll isn't an outage: confirm with pings first.
            run_async(ping_twice, done=confirm, error=lambda e: confirm(False))

        def ping_twice():
            if client.ping():
                return True
            time.sleep(2)
            return client.ping()

        def confirm(alive):
            self._confirming = False
            if gen != self._generation or alive or not self.props.connected:
                return
            if self.props.live:
                # The notification connection is still up: Kodi is there, its web server
                # just stumbled. Look again shortly instead of calling it an outage.
                GLib.timeout_add_seconds(3, lambda: (self.refresh_player(), False)[1])
                return
            self.props.connected = False
            self.props.offline = True
            self.emit("toast", "Lost connection to Kodi")
            self._schedule_reconnect(5)

        run_async(work, done=done, error=failed)

    def _queue_finished(self, st: PlayerState, size) -> bool:
        if st.repeat != "off" or not st.duration:
            return False
        at_end = st.position >= st.duration - 1.5
        if st.playing:
            # "playing" but the clock stands still at the end of the last track: stalled
            prev, self._last_pos = self._last_pos, (st.track_key(), round(st.position, 1))
            last = size is None or st.queue_position >= size - 1
            return at_end and last and prev == self._last_pos
        last = size is None or st.queue_position < 0 or st.queue_position >= size - 1
        just_stopped = time.monotonic() - self._stopped_at < 4
        return (at_end and last) or (just_stopped and at_end)

    def _watchdog(self):
        """While playing, notice when a track should have ended but nothing told us."""
        st = self.state
        if st.active and st.playing and st.duration and \
                st.position + (time.monotonic() - st.stamp) > st.duration + 2.5:
            if self._watch_key != st.track_key():
                self._watch_key = st.track_key()
                self.refresh_player()
        return GLib.SOURCE_CONTINUE

    def _apply_state(self, st: PlayerState):
        old = self.state
        self.state = st
        if old.track_key() != st.track_key():
            self.emit("track-changed")
        elif st.active and abs(old.now() - st.now()) > 2.5:
            self.emit("seeked", st.now())
        self.emit("player-changed")
        self._sync_volume_bridge()
        self._radio_check()

    def _update_state(self, **kw):
        self.state = replace(self.state, **kw)
        self.emit("player-changed")
        self._sync_volume_bridge()

    # -- radio ("what should I play?") ---------------------------------------
    RADIO_START = 40
    RADIO_TOP_UP = 20

    @property
    def radio_label(self) -> str | None:
        if self.state.active and self.state.partymode:
            return f"Party Mode · {self._party_label}" if self._party_label else "Party Mode"
        return self._radio["label"] if self._radio else None

    # -- Kodi's Party Mode (Kodi keeps the music going itself) ---------------------
    _party_label = ""

    def start_partymode(self, playlist: str = "", label: str = ""):
        """Kodi's Party Mode: random songs from the whole library, or from a smart
        playlist (``playlist``: its path). Kodi runs it, so it plays on with Tonearm
        closed."""
        client = self.client
        if client is None or not self.props.connected:
            self.emit("toast", "Kodi is offline")
            return
        self._radio = None                  # Tonearm's own radio steps aside
        self._expect_busy(15)

        def done(_r):
            self._party_label = label if playlist else ""
            self._radio_guard_until = time.monotonic() + 6
            self.emit("toast", f"Party Mode: {label or 'Whole library'}" if playlist
                      else "Party Mode: Whole library")
            self._refresh_soon()
            self.emit("queue-changed")

        run_async(lambda: client.call("Player.Open", {"item": {"partymode": playlist or "music"}},
                                      timeout=30),
                  done=done,
                  error=lambda e: self.emit("problem", "Kodi couldn't start Party Mode", str(e)))

    def stop_partymode(self):
        """Kodi stops adding songs; what plays keeps playing."""
        client, pid = self.client, self.state.playerid
        if client is None or pid < 0:
            return

        def done(_r):
            self._party_label = ""
            self._refresh_soon()
            self.emit("toast", "Party Mode stopped — the queue plays out")

        run_async(lambda: client.call("Player.SetPartymode", {"playerid": pid, "partymode": False}),
                  done=done,
                  error=lambda e: self.emit("problem", "Kodi couldn't stop Party Mode", str(e)))

    def start_radio(self, decade: int | None, genre: str | None):
        """Endless random mix from a decade and/or genre (None = any), from the cache;
        topped up near the end of the queue."""
        if not self.client or not self.cache:
            return
        client, cache = self.client, self.cache
        label = " · ".join(x for x in ((f"{decade}s" if decade else "Unknown year")
                                       if decade is not None else "", genre or "") if x) \
            or "Whole library"

        self._expect_busy(20)

        def work():
            albums = cache.radio_album_ids(decade, genre)
            songs = cache.radio_songs(albums, self.RADIO_START)
            if not songs:
                raise KodiError(0, "nothing in the library matches that")
            client.call("Playlist.Clear", {"playlistid": AUDIO_PLAYLIST})
            for i in range(0, len(songs), 50):
                client.call("Playlist.Add", {"playlistid": AUDIO_PLAYLIST,
                                             "item": [{"songid": s} for s in songs[i:i + 50]]},
                            timeout=60)
            client.call("Player.Open", {"item": {"playlistid": AUDIO_PLAYLIST, "position": 0},
                                        "options": {"shuffled": False}})
            return albums, songs

        def done(res):
            albums, songs = res
            self._radio = {"albums": albums, "seen": set(songs), "label": label, "busy": False}
            self._radio_guard_until = time.monotonic() + 6
            self.emit("toast", f"Radio: {label}")
            self._refresh_soon()
            self.emit("queue-changed")

        run_async(work, done=done, error=lambda e: self._toast_problem(f"Radio: {e}"))

    def pick_surprise(self, callback):
        """``callback((decade, genre))``: a random decade and / or genre with a decent
        number of songs (None = any) — for "Surprise Me"."""
        cache = self.cache
        if cache is None:
            return

        def work():
            import random      # noqa: PLC0415
            decades = [d for d, _n, _c in cache.decades()]
            genres = [g for g, _n, _c in cache.genres()]
            for _ in range(25):
                d = random.choice(decades) if decades and random.random() < 0.8 else None
                g = random.choice(genres) if genres and random.random() < 0.8 else None
                if cache.radio_count(cache.radio_album_ids(d, g)) >= 25:
                    return d, g
            return None, None

        run_async(work, done=callback)

    def surprise_radio(self):
        """Radio from a random decade and genre (the launcher's "Surprise Me")."""
        self.pick_surprise(lambda pick: self.start_radio(*pick))

    def _expect_busy(self, seconds: float):
        """We're about to give Kodi a burst of work (clear + fill the queue while it plays):
        don't mistake its slow answers for a lost connection meanwhile."""
        self._busy_until = max(self._busy_until, time.monotonic() + seconds)

    def stop_radio(self):
        self._radio = None

    def _radio_check(self):
        r = self._radio
        st = self.state
        if r is None or r["busy"] or not st.active or not st.queue_size:
            return
        if st.queue_size - st.queue_position > 3:
            return
        r["busy"] = True
        client, cache = self.client, self.cache

        def work():
            more = cache.radio_songs(r["albums"], self.RADIO_TOP_UP, exclude=r["seen"])
            if not more:  # played everything once: start over
                r["seen"].clear()
                more = cache.radio_songs(r["albums"], self.RADIO_TOP_UP)
            client.call("Playlist.Add", {"playlistid": AUDIO_PLAYLIST,
                                         "item": [{"songid": s} for s in more]}, timeout=60)
            return more

        def done(more):
            r["seen"].update(more)
            r["busy"] = False
            self.emit("queue-changed")
            self._refresh_soon()

        def failed(exc):
            r["busy"] = False

        run_async(work, done=done, error=failed)

    # -- sleep timer -----------------------------------------------------
    def set_sleep(self, choice: str):
        """``choice``: a sleeptimer.CHOICES key ("off", "30", "track", "album", …)."""
        self._sleep_restore_volume()
        self.sleep.set(choice, time.monotonic(), self.state.albumid)
        self.sleep_choice = choice if self.sleep.active else "off"
        self._sleep_next_album = None
        if self.sleep.active:
            if not self._sleep_tick:
                self._sleep_tick = GLib.timeout_add(500, self._sleep_check)
            if self.sleep.mode == "album":
                self._fetch_next_album()
            self.emit("toast", self.sleep.label(time.monotonic()))
        else:
            self.emit("toast", "Sleep timer off")
        self.emit("sleep-changed")

    def _sleep_check(self):
        sl, st = self.sleep, self.state
        if not sl.active:
            self._sleep_tick = 0
            return False
        now = time.monotonic()
        if sl.mode == "minutes" and not st.playing and sl.remaining(now) <= 0:
            self.set_sleep("off")          # time's up while paused: nothing to stop
            self._sleep_tick = 0
            return False
        what = sl.decide(now, playing=st.playing, position=st.now(), duration=st.duration,
                         albumid=st.albumid, next_albumid=self._sleep_next_album,
                         last_in_queue=st.queue_size > 0 and
                         st.queue_position >= st.queue_size - 1 and st.repeat == "off")
        if what == "fade":
            if self._sleep_volume is None:
                self._sleep_volume = st.volume
            vol = sl.fade_volume(self._sleep_volume, now)
            if vol != st.volume:
                self.set_volume(vol)
        elif what == "stop":
            self.stop()
            sl.set("off", now)
            self.sleep_choice = "off"
            self._sleep_tick = 0
            # put the volume back once Kodi has stopped, for next time
            GLib.timeout_add(1500, lambda: (self._sleep_restore_volume(), False)[1])
            self.emit("toast", "Sleep timer: playback stopped")
            self.emit("sleep-changed")
            return False
        return True

    def _sleep_restore_volume(self):
        if self._sleep_volume is not None:
            vol, self._sleep_volume = self._sleep_volume, None
            self.set_volume(vol)

    def _sleep_track_changed(self):
        if self.sleep.mode == "album":
            self._sleep_next_album = None
            self._fetch_next_album()

    def _fetch_next_album(self):
        """The album of the next queue item, for "end of album"."""
        if not self.client:
            return
        client, pos = self.client, self.state.queue_position

        def work():
            res = client.call("Playlist.GetItems", {
                "playlistid": AUDIO_PLAYLIST, "properties": ["albumid"],
                "limits": {"start": pos + 1, "end": pos + 2}}) or {}
            items = res.get("items") or []
            return items[0].get("albumid", 0) if items else 0

        def done(albumid):
            if self.sleep.mode == "album":
                self._sleep_next_album = albumid

        run_async(work, done=done, error=lambda e: None)

    # -- volume keys -----------------------------------------------------
    def refresh_volume_bridge(self):
        """Call after the preference changes."""
        self._sync_volume_bridge()

    def _sync_volume_bridge(self):
        if not volumebridge.available():
            return
        st = self.state
        want = bool(self.config.prefs.get("volume_keys")) and st.active and self.props.connected
        if want and not self._vb_on:
            self._vb_on = True
            self._vb_last = (st.volume, st.muted)
            self._vb_pool.submit(self._vb_run, self._vb.start, st.volume, st.muted)
        elif not want and self._vb_on:
            self._vb_on = False
            self._vb_pool.submit(self._vb_run, self._vb.stop)
        elif want and (st.volume, st.muted) != self._vb_last:
            self._vb_last = (st.volume, st.muted)
            self._vb_pool.submit(self._vb_run, self._vb.push, st.volume, st.muted)

    def _vb_run(self, fn, *args):
        try:
            fn(*args)
        except Exception as exc:  # noqa: BLE001
            if fn == self._vb.start:
                idle(self._vb_failed, exc)

    def _vb_failed(self, exc):
        self._vb_on = False
        import sys
        log.warning("Volume keys: %s", exc)
        self.emit("problem", "Volume keys unavailable", str(exc))

    def _on_bridge_change(self, volume: int, muted: bool):
        """The volume keys / system volume moved: apply it to Kodi."""
        if not self._vb_on:
            return
        self._vb_last = (volume, muted)
        st = self.state
        if muted != st.muted:
            self._update_state(muted=muted)
            self._cmd("Application.SetMute", {"mute": muted})
        if volume != st.volume:
            self.set_volume(volume)

    # ------------------------------------------------------------------
    # commands
    # ------------------------------------------------------------------
    def _cmd(self, method, params, toast=None, then=None):
        if not self.client:
            return
        client = self.client

        def done(_):
            if toast:
                self.emit("toast", toast)
            if then:
                then()
            self._refresh_soon()

        def failed(exc):
            self._toast_problem(f"Kodi: {exc}")

        run_async(client.call, method, params, done=done, error=failed)

    def _seq(self, calls, toast=None):
        """Run calls in order on one worker thread."""
        if not self.client:
            return
        client = self.client

        def work():
            for call in calls:               # (method, params[, timeout])
                client.call(call[0], call[1], *call[2:3])

        def failed(exc):
            self._toast_problem(f"Kodi: {exc}")

        def done(_):
            if toast:
                self.emit("toast", toast)
            self._refresh_soon()
            self.emit("queue-changed")

        run_async(work, done=done, error=failed)

    def _play_items(self, item: dict, position: int = 0, shuffle: bool = False):
        self._radio = None  # playing something specific ends a radio session
        self._expect_busy(10)
        self._seq([
            ("Playlist.Clear", {"playlistid": AUDIO_PLAYLIST}),
            ("Playlist.Add", {"playlistid": AUDIO_PLAYLIST, "item": item}),
            ("Player.Open", {"item": {"playlistid": AUDIO_PLAYLIST, "position": position},
                             "options": {"shuffled": shuffle}}),
        ])

    def play_album(self, albumid: int, position: int = 0, shuffle: bool = False):
        self._play_items({"albumid": albumid}, position, shuffle)

    def play_artist(self, artistid: int, shuffle: bool = False):
        self._play_items({"artistid": artistid}, 0, shuffle)

    def play_song(self, songid: int):
        self._play_items({"songid": songid})

    def enqueue(self, item: dict):
        self._seq([("Playlist.Add", {"playlistid": AUDIO_PLAYLIST, "item": item})],
                  toast="Added to queue")

    def play_next(self, item: dict):
        if not self.state.active:
            self._play_items(item)
            return
        pos = max(0, self.state.queue_position + 1)
        self._seq([("Playlist.Insert", {"playlistid": AUDIO_PLAYLIST, "position": pos, "item": item})],
                  toast="Playing next")

    def play_pause(self):
        if self.state.active:
            self._update_state(playing=not self.state.playing, position=self.state.now(),
                               stamp=time.monotonic())
            self._cmd("Player.PlayPause", {"playerid": self.state.playerid})
        else:
            self._cmd("Player.Open", {"item": {"playlistid": AUDIO_PLAYLIST, "position": 0}})

    def play(self):
        if self.state.active and not self.state.playing:
            self.play_pause()
        elif not self.state.active:
            self.play_pause()

    def pause(self):
        if self.state.active and self.state.playing:
            self.play_pause()

    def stop(self):
        if self.state.active:
            self._cmd("Player.Stop", {"playerid": self.state.playerid})

    def next(self):
        if self.state.active:
            self._cmd("Player.GoTo", {"playerid": self.state.playerid, "to": "next"})

    def previous(self):
        if self.state.active:
            # Like most players: restart the track unless we're at the very start
            if self.state.now() > 4:
                self.seek(0)
            else:
                self._cmd("Player.GoTo", {"playerid": self.state.playerid, "to": "previous"})

    def goto(self, position: int):
        if self.state.active:
            self._cmd("Player.GoTo", {"playerid": self.state.playerid, "to": position})
        else:
            self._cmd("Player.Open", {"item": {"playlistid": AUDIO_PLAYLIST, "position": position}})

    def seek(self, seconds: float):
        if not self.state.active:
            return
        seconds = max(0.0, min(seconds, self.state.duration or seconds))
        api = (self.client.api_version or (0,))[0]
        value = {"time": _time_obj(seconds)} if api >= 10 else _time_obj(seconds)
        self._update_state(position=seconds, stamp=time.monotonic())
        self.emit("seeked", seconds)
        self._cmd("Player.Seek", {"playerid": self.state.playerid, "value": value})

    def set_volume(self, volume: int):
        volume = max(0, min(100, int(volume)))
        self.state.volume = volume
        self._cmd("Application.SetVolume", {"volume": volume})

    def toggle_mute(self):
        self._cmd("Application.SetMute", {"mute": "toggle"})

    def set_shuffle(self, value: bool):
        if self.state.active:
            self._update_state(shuffle=value)
            self._cmd("Player.SetShuffle", {"playerid": self.state.playerid, "shuffle": value})

    def set_repeat(self, value: str):
        if self.state.active:
            self._update_state(repeat=value)
            self._cmd("Player.SetRepeat", {"playerid": self.state.playerid, "repeat": value})

    def cycle_repeat(self):
        nxt = {"off": "all", "all": "one", "one": "off"}[self.state.repeat]
        self.set_repeat(nxt)

    def move_in_queue(self, src: int, dst: int):
        """Move a queue item from ``src`` to ``dst``. Kodi's API swaps two items, so it's
        a walk of neighbour swaps (the playing track may move too: Kodi keeps playing it)."""
        if src == dst or src < 0 or dst < 0:
            return
        step = 1 if dst > src else -1
        self._seq([("Playlist.Swap", {"playlistid": AUDIO_PLAYLIST, "position1": i,
                                      "position2": i + step})
                   for i in range(src, dst, step)])

    def remove_from_queue(self, position: int):
        self._seq([("Playlist.Remove", {"playlistid": AUDIO_PLAYLIST, "position": position})])

    def clear_queue(self):
        self._seq([("Playlist.Clear", {"playlistid": AUDIO_PLAYLIST})])

    def get_queue(self, callback):
        if not self.client:
            callback([])
            return
        client = self.client

        def work():
            res = client.call("Playlist.GetItems", {
                "playlistid": AUDIO_PLAYLIST,
                "properties": ["title", "artist", "album", "duration", "albumid", "thumbnail"]})
            return (res or {}).get("items") or []

        run_async(work, done=callback, error=lambda e: callback([]))

    def fetch_album_songs(self, albumid: int, callback):
        """Fetch songs of one album straight from Kodi (cache miss) and cache them."""
        if not self.client or not self.sync_engine:
            callback([])
            return
        client, cache, engine = self.client, self.cache, self.sync_engine

        def work():
            res = client.call("AudioLibrary.GetSongs", {
                "filter": {"albumid": albumid}, "properties": engine._props_for("song")})
            songs = (res or {}).get("songs") or []
            cache.upsert_songs(songs)
            return cache.songs_for_album(albumid)

        run_async(work, done=callback, error=lambda e: callback([]))

    def fetch_artist_art(self, artistid: int, callback):
        """Ask Kodi for an artist's thumb/fanart (also from its art map); cache and
        return (thumbnail, fanart)."""
        if not self.client:
            return
        client, cache = self.client, self.cache

        def work():
            props = ["thumbnail", "fanart"]
            if (client.api_version or (0,))[0] >= 10:
                props.append("art")
            res = client.call("AudioLibrary.GetArtistDetails", {"artistid": artistid, "properties": props})
            d = (res or {}).get("artistdetails") or {}
            art = d.get("art") or {}
            thumb = d.get("thumbnail") or art.get("thumb") or ""
            fanart = d.get("fanart") or art.get("fanart") or art.get("fanart1") or ""
            if thumb or fanart:
                cache.set_artist_art(artistid, thumb, fanart)
            return thumb, fanart

        run_async(work, done=callback, error=lambda e: None)

    def fetch_album_info(self, albumid: int, callback):
        """Review text, styles, moods and label for an album, straight from Kodi (kept
        in memory for the session). ``callback(dict | None)`` on the main loop."""
        memo = self.__dict__.setdefault("_album_info", {})
        key = (self.instance.id if self.instance else "", albumid)  # ids are per Kodi
        if key in memo:
            GLib.idle_add(lambda: (callback(memo[key]), False)[1])
            return
        if not self.client:
            return
        client = self.client

        def work():
            res = client.call("AudioLibrary.GetAlbumDetails", {
                "albumid": albumid, "properties": ["description", "style", "mood", "albumlabel"]})
            d = (res or {}).get("albumdetails") or {}
            info = {"description": (d.get("description") or "").strip(),
                    "style": [x for x in d.get("style") or [] if x],
                    "mood": [x for x in d.get("mood") or [] if x],
                    "label": (d.get("albumlabel") or "").strip()}
            memo[key] = info
            return info

        run_async(work, done=callback, error=lambda e: callback(None))

    def fetch_artist_description(self, artistid: int, callback):
        if not self.client:
            return
        client, cache = self.client, self.cache

        def work():
            res = client.call("AudioLibrary.GetArtistDetails",
                              {"artistid": artistid, "properties": ["description"]})
            desc = ((res or {}).get("artistdetails") or {}).get("description") or ""
            if desc:
                cache.set_artist_description(artistid, desc)
            return desc

        run_async(work, done=callback, error=lambda e: None)

    # ------------------------------------------------------------------
    # Lidarr
    # ------------------------------------------------------------------
    def configure_lidarr(self):
        """(Re)create the Lidarr client from config; call after settings change."""
        st = self.config.lidarr
        self.lidarr = LidarrClient(st.url, st.api_key, cache_dir=os.path.join(cache_dir(), "lidarr")) \
            if st.configured else None
        self.lidarr_info = {}
        if self.artwork is not None:
            self.artwork.lidarr = self.lidarr
        self.emit("lidarr-changed")
        if self.lidarr is not None:
            client = self.lidarr

            def done(info):
                if client is self.lidarr:
                    self.lidarr_info = info or {}
                    self.emit("lidarr-changed")

            run_async(client.status, done=done, error=lambda e: None)

    def lidarr_run(self, fn, *args, done=None, toast_errors=True):
        """Run a LidarrClient call on a worker; errors become toasts."""
        if self.lidarr is None:
            return

        def failed(exc):
            if toast_errors:
                self._toast_problem(f"Lidarr: {exc}")
            if done is not None and not toast_errors:
                done(None)

        run_async(fn, *args, done=done, error=failed)

    def kodi_artist_id(self, name: str) -> int:
        """Kodi artistid for a name (loose match), or 0."""
        if self.cache is None:
            return 0
        with self._index_lock:
            names = self._kodi_names
        if names is None:
            names = {}
            for aid, name in self.cache.artist_names():  # light query, not the stats one
                names.setdefault(norm_name(name), aid)
            with self._index_lock:
                self._kodi_names = names
        return names.get(norm_name(name), 0)

    def lidarr_search(self, query: str, callback):
        """Lidarr/MusicBrainz artist lookup, minus artists already in Kodi.

        callback(list of lookup dicts) — only the newest query reports back.
        """
        self._lidarr_gen += 1
        gen = self._lidarr_gen
        client = self.lidarr
        query = query.strip()
        if client is None or len(query) < 3 or any(c in query for c in "'^$!|"):
            callback([])
            return

        def work():
            res = client.lookup(query)
            out = []
            for r in res[:25]:
                if not self.kodi_artist_id(r.get("artistName", "")):
                    out.append(r)
            return out[:10]

        def done(res):
            if gen == self._lidarr_gen:
                callback(res)

        run_async(work, done=done, error=lambda e: gen == self._lidarr_gen and callback([]))

    def lidarr_artist_for_kodi(self, name: str, callback):
        """Find the Lidarr library artist matching a Kodi artist name."""
        client = self.lidarr
        if client is None:
            return
        key = norm_name(name)

        def work():
            return next((a for a in client.artists() if norm_name(a.get("artistName", "")) == key), None)

        run_async(work, done=callback, error=lambda e: None)

    # ------------------------------------------------------------------
    # Played, genres, decades, playlists
    # ------------------------------------------------------------------
    PLAYED_PROPS = ["title", "displayartist", "artist", "year", "genre", "thumbnail",
                    "playcount", "lastplayed"]

    def fetch_played(self, kind: str, callback, limit: int = 120):
        """kind: "recent" (last played) or "most" (play count). Asks Kodi live — play
        stats change all the time — and keeps the answer for offline use.

        callback(list of album dicts, from_cache: bool)."""
        cache = self.cache
        meta_key = f"played_{kind}"
        if self.client is None or not self.props.connected:
            callback((cache.get_meta_json(meta_key, []) if cache else []), True)
            return
        client = self.client
        method = "lastplayed" if kind == "recent" else "playcount"

        def work():
            params = {"properties": self.PLAYED_PROPS,
                      "sort": {"method": method, "order": "descending"},
                      "limits": {"start": 0, "end": limit},
                      "filter": {"field": "playcount", "operator": "greaterthan", "value": "0"}}
            try:
                res = client.call("AudioLibrary.GetAlbums", params)
            except KodiError:
                params.pop("filter")  # older Kodi: no playcount filter for albums
                params["properties"] = [p for p in self.PLAYED_PROPS if p != "lastplayed"] \
                    if method == "playcount" else self.PLAYED_PROPS
                res = client.call("AudioLibrary.GetAlbums", params)
            albums = [a for a in (res or {}).get("albums") or [] if a.get("playcount", 1)]
            if cache is not None:
                cache.set_meta(meta_key, albums)
            return albums

        run_async(work, done=lambda albums: callback(albums, False),
                  error=lambda e: callback((cache.get_meta_json(meta_key, []) if cache else []), True))

    LISTEN_PROPS = ["albumid", "lastplayed", "playcount", "genre", "year", "duration"]

    def fetch_listening(self, callback):
        """Recently played and most played tracks (Spotlight: Jump Back In and the radio
        tiles; Genres: most played). callback({"recent": [...], "top": [...]}, from_cache)."""
        cache = self.cache
        cached = lambda: (cache.get_meta_json("listening", {}) if cache else {})  # noqa: E731
        if self.client is None or not self.props.connected:
            callback(cached(), True)
            return
        client = self.client

        def get(method, n):
            res = client.call("AudioLibrary.GetSongs", {
                "properties": self.LISTEN_PROPS,
                "sort": {"method": method, "order": "descending"},
                "limits": {"start": 0, "end": n},
                "filter": {"field": "playcount", "operator": "greaterthan", "value": "0"}})
            return [s for s in (res or {}).get("songs") or [] if s.get("playcount", 1)]

        def work():
            data = {"recent": get("lastplayed", 400), "top": get("playcount", 500)}
            if cache is not None:
                cache.set_meta("listening", data)
            return data

        run_async(work, done=lambda d: callback(d, False),
                  error=lambda e: callback(cached(), True))

    def fetch_playlists(self, callback):
        """Kodi's music playlists (smart .xsp and .m3u/.pls). callback(list, from_cache)."""
        cache = self.cache
        if self.client is None or not self.props.connected:
            callback((cache.get_meta_json("playlists", []) if cache else []), True)
            return
        client = self.client

        def work():
            res = client.call("Files.GetDirectory", {
                "directory": "special://musicplaylists/", "media": "music",
                "sort": {"method": "label", "ignorearticle": True}})
            out = []
            for f in (res or {}).get("files") or []:
                path = f.get("file") or ""
                low = path.lower().rstrip("/")
                if low.endswith((".xsp", ".m3u", ".m3u8", ".pls")):
                    out.append({"file": path, "label": f.get("label") or path.rsplit("/", 1)[-1],
                                "smart": low.endswith(".xsp")})
            if cache is not None:
                cache.set_meta("playlists", out)
            return out

        run_async(work, done=lambda pls: callback(pls, False),
                  error=lambda e: callback((cache.get_meta_json("playlists", []) if cache else []), True))

    def fetch_playlist_items(self, path: str, callback):
        """Tracks of one playlist (Kodi resolves smart playlists). callback(list or None)."""
        if self.client is None:
            callback(None)
            return
        client = self.client

        def work():
            res = client.call("Files.GetDirectory", {
                "directory": path, "media": "music",
                "properties": ["title", "artist", "album", "albumid", "duration", "thumbnail"]},
                timeout=60)
            return (res or {}).get("files") or []

        run_async(work, done=callback, error=lambda e: callback(None))

    # -- Kodi Favourites ------------------------------------------------------

    def fav_album_ids(self) -> set:
        return {f["albumid"] for f in self.favourites if f.get("albumid")}

    def fav_artist_ids(self) -> set:
        return {f["artistid"] for f in self.favourites if f.get("artistid")}

    def fav_playlist_paths(self) -> set:
        return {f["playlist"] for f in self.favourites if f.get("kind") == "playlist"}

    def toggle_playlist_favourite(self, path: str, title: str):
        """A Kodi playlist (smart or .m3u) in or out of Kodi's Favorites."""
        existing = next((f for f in self.favourites
                         if f.get("kind") == "playlist" and f.get("playlist") == path), None)
        params = favourites.remove_request(existing) if existing else \
            favourites.playlist_request(path, title)
        self._toggle_favourite(params, title, existing is None)

    def fav_song_ids(self) -> set:
        """Songs that are Favorites: by Kodi's musicdb id, or by file for the songs whose
        files we know (fetch_song_files)."""
        files = self.__dict__.get("_song_files", {})
        by_path = {f["path"] for f in self.favourites if f.get("kind") == "song" and f.get("path")}
        ids = {f["songid"] for f in self.favourites if f.get("songid")}
        ids |= {sid for sid, path in files.items() if path in by_path}
        return ids

    def fetch_song_files(self, albumid: int, callback=None):
        """File paths of an album's songs (to match file-based song favourites);
        remembered for the session."""
        files = self.__dict__.setdefault("_song_files", {})
        known = self.__dict__.setdefault("_song_file_albums", set())
        if not self.client:
            return
        if albumid in known:          # asked before this session: answer at once
            if callback is not None:
                GLib.idle_add(lambda: (callback({}), False)[1])
            return
        known.add(albumid)
        client = self.client

        def work():
            res = client.call("AudioLibrary.GetSongs", {"filter": {"albumid": albumid},
                                                        "properties": ["file"]})
            return {s["songid"]: s.get("file") or "" for s in (res or {}).get("songs") or []}

        def done(found):
            files.update(found)
            if callback is not None:
                callback(found)

        run_async(work, done=done, error=lambda e: None)

    def toggle_song_favourite(self, songid: int):
        if not self.client:
            return
        client, cache = self.client, self.cache
        files = self.__dict__.setdefault("_song_files", {})

        def work():
            path = files.get(songid)
            if not path:
                d = (client.call("AudioLibrary.GetSongDetails", {
                    "songid": songid, "properties": ["file"]}) or {}).get("songdetails") or {}
                path = files[songid] = d.get("file") or ""
            return path

        def done(path):
            existing = next((f for f in self.favourites if f.get("kind") == "song" and (
                f.get("songid") == songid or (path and f.get("path") == path))), None)
            song = cache.song(songid) if cache else None
            title = song.title if song else "Song"
            if existing is not None:
                self._toggle_favourite(favourites.remove_request(existing), title, False)
            elif path:
                al = cache.album(song.albumid) if cache and song else None
                self._toggle_favourite(favourites.song_request(path, title,
                                                               al.thumbnail if al else ""),
                                       title, True)

        run_async(work, done=done, error=lambda e: self._toast_problem(f"Kodi: {e}"))

    def fetch_favourites(self, callback=None):
        """Kodi's Favourites (also cached for offline). Emits favourites-changed;
        ``callback(list, from_cache)`` if given."""
        cache = self.cache

        def deliver(favs, from_cache):
            # set before the signal: pages show the "offline" banner from this flag
            self.favourites = favs
            self.favourites_stale = from_cache
            self.emit("favourites-changed")
            if callback is not None:
                callback(favs, from_cache)

        cached = lambda: (cache.get_meta_json("favourites", []) if cache else [])  # noqa: E731
        if self.client is None or not self.props.connected:
            deliver(cached(), True)
            return
        client = self.client

        def work():
            res = client.call("Favourites.GetFavourites", {"properties": favourites.PROPS})
            favs = favourites.normalise((res or {}).get("favourites"))
            if cache is not None:
                cache.set_meta("favourites", favs)
            return favs

        run_async(work, done=lambda favs: deliver(favs, False),
                  error=lambda e: deliver(cached(), True))

    def toggle_album_favourite(self, albumid: int):
        al = self.cache.album(albumid) if self.cache else None
        if al is None:
            return
        existing = next((f for f in self.favourites if f.get("albumid") == albumid), None)
        params = favourites.remove_request(existing) if existing else \
            favourites.album_request(albumid, al.title, al.thumbnail)
        self._toggle_favourite(params, al.title, existing is None)

    def toggle_artist_favourite(self, artistid: int):
        ar = self.cache.artist(artistid) if self.cache else None
        if ar is None:
            return
        existing = next((f for f in self.favourites if f.get("artistid") == artistid), None)
        params = favourites.remove_request(existing) if existing else \
            favourites.artist_request(artistid, ar.name, ar.thumbnail)
        self._toggle_favourite(params, ar.name, existing is None)

    def remove_favourite(self, fav: dict):
        self._toggle_favourite(favourites.remove_request(fav), fav.get("title", ""), False)

    def _toggle_favourite(self, params, title, adding):
        if not self.client:
            return
        if (self.client.api_version or (99,))[0] < 8:
            self.emit("toast", "This Kodi is too old to change favorites remotely")
            return
        client = self.client

        def done(_):
            self.emit("toast", f"Added “{title}” to Favorites" if adding
                      else f"Removed “{title}” from Favorites")
            self.fetch_favourites()

        # Kodi's AddFavourite adds — or removes the same favourite if it's there already
        run_async(client.call, "Favourites.AddFavourite", params, done=done,
                  error=lambda e: self._toast_problem(f"Kodi: {e}"))

    def open_favourite(self, fav: dict) -> bool:
        """Play / open a favourite on Kodi. Albums and artists are left to the caller
        (they open as pages); returns False for those."""
        kind = fav.get("kind")
        if kind in ("album", "artist"):
            return False
        if kind == "song" and fav.get("songid"):
            self.play_song(fav["songid"])
            return True
        if kind == "playlist" and fav.get("playlist"):
            self.play_playlist(fav["playlist"])
            self.emit("toast", "Playing on Kodi")
            return True
        if kind == "song" and fav.get("path"):
            kind = "media"
        if kind == "media" and fav.get("path"):
            self._radio = None
            self._cmd("Player.Open", {"item": {"file": fav["path"]}}, toast="Playing on Kodi")
        elif kind == "window" and fav.get("window"):
            params = {"window": fav["window"]}
            if fav.get("windowparameter"):
                params["parameters"] = [fav["windowparameter"]]
            self._cmd("GUI.ActivateWindow", params, toast="Opened on Kodi")
        elif kind == "script" and fav.get("path"):
            path = fav["path"]
            if "://" in path:
                self._cmd("Player.Open", {"item": {"file": path}}, toast="Opened on Kodi")
            else:
                self._cmd("Addons.ExecuteAddon", {"addonid": path}, toast="Started on Kodi")
        else:
            self._toast_problem("Tonearm can't open this favourite")
        return True

    # -- Tonearm's own playlists (Kodi's API can't write playlist files) -----
    def my_playlists(self) -> list[dict]:
        return self.playlists.all() if self.playlists is not None else []

    def create_playlist(self, name: str) -> dict | None:
        if self.playlists is None:
            return None
        pl = self.playlists.create(name)
        self.emit("playlists-changed")
        return pl

    def rename_playlist(self, pid: str, name: str):
        if self.playlists is not None:
            self.playlists.rename(pid, name)
            self.emit("playlists-changed")

    def delete_playlist(self, pid: str):
        if self.playlists is not None:
            pl = self.playlists.get(pid)
            self.playlists.delete(pid)
            self.emit("playlists-changed")
            if pl:
                self.emit("toast", f"Deleted “{pl['name']}”")

    def remove_from_playlist(self, pid: str, index: int):
        if self.playlists is not None:
            self.playlists.remove(pid, index)
            self.emit("playlists-changed")

    def add_to_playlist(self, pid: str, kind: str, item_id: int):
        """Add a song, an album's songs or an artist's songs to a playlist."""
        store, cache = self.playlists, self.cache
        if store is None or cache is None or store.get(pid) is None:
            return

        def songs_of_album(albumid):
            songs = cache.songs_for_album(albumid)
            if not songs and self.client is not None:     # not cached yet: ask Kodi
                res = self.client.call("AudioLibrary.GetSongs", {
                    "filter": {"albumid": albumid},
                    "properties": self.sync_engine._props_for("song")})
                cache.upsert_songs((res or {}).get("songs") or [])
                songs = cache.songs_for_album(albumid)
            return songs

        def work():
            if kind == "song":
                s = cache.song(item_id)
                songs = [s] if s else []
            elif kind == "album":
                songs = songs_of_album(item_id)
            else:   # artist: their albums, newest first as on the artist page
                songs = [s for a in cache.albums_for_artist(item_id) for s in songs_of_album(a.id)]
            return [{"id": s.id, "title": s.title, "artist": s.artist} for s in songs]

        def done(songs):
            pl = store.get(pid)
            if pl is None:
                return
            n = store.add(pid, songs)
            self.emit("playlists-changed")
            if n:
                self.emit("toast", f"Added {n} song{'s' if n != 1 else ''} to “{pl['name']}”")
            else:
                self.emit("toast", f"Already in “{pl['name']}”")

        run_async(work, done=done, error=lambda e: self._toast_problem(f"Kodi: {e}"))

    def play_my_playlist(self, pid: str, position: int = 0, shuffle: bool = False):
        pl = self.playlists.get(pid) if self.playlists is not None else None
        if not self.client or pl is None or not pl["songs"]:
            return
        self._radio = None
        self._expect_busy(20)
        client, ids = self.client, [s["id"] for s in pl["songs"]]

        def work():
            client.call("Playlist.Clear", {"playlistid": AUDIO_PLAYLIST})
            for i in range(0, len(ids), 50):
                client.call("Playlist.Add", {"playlistid": AUDIO_PLAYLIST,
                                             "item": [{"songid": s} for s in ids[i:i + 50]]},
                            timeout=60)
            client.call("Player.Open", {"item": {"playlistid": AUDIO_PLAYLIST,
                                                 "position": position},
                                        "options": {"shuffled": shuffle}})

        def done(_):
            self._refresh_soon()
            self.emit("queue-changed")

        run_async(work, done=done, error=lambda e: self._toast_problem(f"Kodi: {e}"))

    def play_playlist(self, path: str, position: int = 0, shuffle: bool = False):
        # A playlist file added to the audio playlist expands to its tracks.
        self._play_items({"file": path}, position, shuffle)

    def _genre_id(self, name: str) -> int:
        if not getattr(self, "_genre_ids", None):
            res = self.client.call("AudioLibrary.GetGenres") or {}
            self._genre_ids = {g["label"].lower(): g["genreid"] for g in res.get("genres") or []}
        return self._genre_ids.get(name.lower(), 0)

    def play_genre(self, name: str, shuffle: bool = True):
        if not self.client:
            return
        self._radio = None
        self._expect_busy(20)
        client = self.client

        def work():
            gid = self._genre_id(name)
            if not gid:
                raise KodiError(0, f"Kodi has no genre called {name!r}")
            for m, p in (("Playlist.Clear", {"playlistid": AUDIO_PLAYLIST}),
                         ("Playlist.Add", {"playlistid": AUDIO_PLAYLIST, "item": {"genreid": gid}}),
                         ("Player.Open", {"item": {"playlistid": AUDIO_PLAYLIST, "position": 0},
                                          "options": {"shuffled": shuffle}})):
                client.call(m, p)

        def done(_):
            self._refresh_soon()
            self.emit("queue-changed")

        run_async(work, done=done, error=lambda e: self._toast_problem(f"Kodi: {e}"))

    def play_albums(self, albumids: list, shuffle: bool = False):
        """Queue several albums (a decade, a genre page…) and start playing."""
        if not self.client or not albumids:
            return
        self._radio = None
        self._expect_busy(30)
        client = self.client
        ids = list(albumids)[:500]

        def work():
            client.call("Playlist.Clear", {"playlistid": AUDIO_PLAYLIST})
            for i in range(0, len(ids), 50):
                client.call("Playlist.Add", {"playlistid": AUDIO_PLAYLIST,
                                             "item": [{"albumid": a} for a in ids[i:i + 50]]},
                            timeout=60)
            client.call("Player.Open", {"item": {"playlistid": AUDIO_PLAYLIST, "position": 0},
                                        "options": {"shuffled": shuffle}})

        def done(_):
            self._refresh_soon()
            self.emit("queue-changed")

        run_async(work, done=done, error=lambda e: self._toast_problem(f"Kodi: {e}"))
