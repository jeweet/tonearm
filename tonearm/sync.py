"""Incremental library synchronisation Kodi → SQLite cache.

Strategy (cheapest first):

1. ``AudioLibrary.GetProperties`` (Kodi 19+) returns timestamps such as
   ``albumslastadded`` / ``songsmodified``. If nothing changed since the last
   sync, we stop after a single tiny request.
2. For each kind that did change we fetch an *ids-only* list (no properties),
   diff it against the cache and delete what disappeared.
3. Items added or modified since our watermark (the newest ``dateadded`` /
   ``datemodified`` we have, in Kodi's own clock) are fetched with a filter.
4. Anything still missing (e.g. old Kodi without date filters) is fetched by
   id in batched requests; if a lot is missing we fall back to a paged full
   fetch, which is cheaper at that point.
"""
from __future__ import annotations

import datetime as _dt
import time
from dataclasses import dataclass, field

from .cache import LibraryCache
from .kodi import INVALID_PARAMS, KodiClient, KodiError, KodiTimeout

LIB_PROPS = [
    "librarylastupdated", "librarylastcleaned", "artistlinksupdated", "missingartistid",
    "songslastadded", "albumslastadded", "artistslastadded",
    "songsmodified", "albumsmodified", "artistsmodified",
]
COMMON_KEYS = {"librarylastupdated", "librarylastcleaned", "artistlinksupdated", "missingartistid"}
KIND_KEYS = {
    "artist": COMMON_KEYS | {"artistslastadded", "artistsmodified"},
    "album": COMMON_KEYS | {"albumslastadded", "albumsmodified"},
    "song": COMMON_KEYS | {"songslastadded", "songsmodified"},
}

BASE_PROPS = {
    "artist": ["thumbnail", "fanart", "genre"],
    "album": ["title", "artist", "artistid", "displayartist", "year", "genre", "thumbnail"],
    "song": ["title", "artist", "displayartist", "albumid", "track", "disc", "duration"],
}
OPTIONAL_PROPS = {
    "artist": ["sortname", "dateadded", "art"],
    "album": ["dateadded", "datemodified", "art"],
    "song": ["dateadded", "datemodified"],
}
METHODS = {
    "artist": ("AudioLibrary.GetArtists", "artists", "artistid",
               "AudioLibrary.GetArtistDetails", "artistdetails"),
    "album": ("AudioLibrary.GetAlbums", "albums", "albumid",
              "AudioLibrary.GetAlbumDetails", "albumdetails"),
    "song": ("AudioLibrary.GetSongs", "songs", "songid",
             "AudioLibrary.GetSongDetails", "songdetails"),
}
PAGE = {"artist": 2000, "album": 1000, "song": 2500}
DETAILS_BATCH = 50
SYNC_TIMEOUT = 120.0
MIN_PAGE = 100
OLD_KODI_RECHECK = 6 * 3600
FULL_FETCH_THRESHOLD = 400


class SyncCancelled(Exception):
    """The sync was stopped on purpose."""


@dataclass
class SyncResult:
    skipped: bool = False
    full: bool = False
    added: dict = field(default_factory=lambda: {"artist": 0, "album": 0, "song": 0})
    updated: dict = field(default_factory=lambda: {"artist": 0, "album": 0, "song": 0})
    removed: dict = field(default_factory=lambda: {"artist": 0, "album": 0, "song": 0})
    seconds: float = 0.0
    requests: int = 0

    @property
    def changed(self) -> bool:
        return any(self.added.values()) or any(self.updated.values()) or any(self.removed.values())

    def summary(self) -> str:
        if self.skipped:
            return "Library is up to date"
        parts = []
        for label, d in (("added", self.added), ("updated", self.updated), ("removed", self.removed)):
            n = d["album"]
            s = d["song"]
            if n or s:
                parts.append(f"{label} {n} album{'s' if n != 1 else ''}"
                             + (f", {s} song{'s' if s != 1 else ''}" if s else ""))
        if not parts:
            return "No changes"
        return "; ".join(parts).capitalize()


class LibrarySync:
    def __init__(self, client: KodiClient, cache: LibraryCache):
        self.client = client
        self.cache = cache
        self._props: dict[str, list[str]] = {}
        self._date_filter_ok: dict[str, bool] = {}
        self._page: dict[str, int] = {}
        self._cancelled = False
        self._ceiling: dict[str, int] = {}
        self._requests = 0

    # -- helpers ---------------------------------------------------------
    def cancel(self):
        """Stop at the next request (instance switched, app quitting)."""
        self._cancelled = True

    def _call(self, method, params=None):
        if self._cancelled:
            raise SyncCancelled()
        self._requests += 1
        # Big library pages can take a while on small Kodi boxes (Pi, NAS): be patient.
        return self.client.call(method, params, timeout=SYNC_TIMEOUT)

    def _props_for(self, kind: str) -> list[str]:
        if kind not in self._props:
            props = list(BASE_PROPS[kind])
            try:
                major = self.client.get_api_version()[0]
            except Exception:
                major = 0
            if major >= 10:  # Kodi 19 "Matrix" and newer
                props += OPTIONAL_PROPS[kind]
            elif kind == "song":
                props.append("dateadded")
            self._props[kind] = props
        return self._props[kind]

    def _list(self, kind: str, params: dict) -> dict:
        method, key, *_ = METHODS[kind]
        params = dict(params)
        if kind == "artist":
            params["albumartistsonly"] = True
        try:
            return self._call(method, params) or {}
        except KodiError as e:
            # An unknown property on an older Kodi: first drop just "art", then all
            # optional properties, remembering what works for later calls.
            if e.code != INVALID_PARAMS or not params.get("properties"):
                raise
            props = params["properties"]
            for drop in ({"art"}, set(OPTIONAL_PROPS[kind])):
                if not set(props) & drop:
                    continue
                props = [p for p in props if p not in drop]
                params["properties"] = props
                try:
                    res = self._call(method, params) or {}
                except KodiError as e2:
                    if e2.code != INVALID_PARAMS:
                        raise
                    continue
                self._props[kind] = [p for p in self._props_for(kind) if p in props]
                return res
            raise

    def _paged(self, kind: str, properties: list[str], flt: dict | None = None, progress=None,
               on_page=None, start: int = 0):
        """Fetch a whole list in pages. The page size adapts so each request takes a few
        seconds; a timeout halves it and retries the same page. ``on_page(items, next_start)``
        lets callers store each page as it arrives (resumable first sync)."""
        _, key, *_ = METHODS[kind]
        page = self._page.get(kind, PAGE[kind])
        total, out = None, []
        while total is None or start < total:
            params = {"properties": properties, "limits": {"start": start, "end": start + page}}
            if flt:
                params["filter"] = flt
            t0 = time.monotonic()
            try:
                res = self._list(kind, params)
            except KodiTimeout:
                if page <= MIN_PAGE:
                    raise
                self._ceiling[kind] = page  # never grow back to a size that timed out
                page = max(MIN_PAGE, page // 2)
                self._page[kind] = page
                continue
            took = time.monotonic() - t0
            if properties:  # tune only on real data pages, not ids-only lists
                if took > 8 and page > MIN_PAGE:
                    page = max(MIN_PAGE, page // 2)
                elif took < 2 and page * 2 < self._ceiling.get(kind, PAGE[kind] * 4 + 1):
                    page = page * 2
                self._page[kind] = page
            items = res.get(key) or []
            total = (res.get("limits") or {}).get("total", len(items))
            if on_page is not None:
                on_page(items, start + len(items))
            else:
                out.extend(items)
            if not items:
                break
            start += len(items)
            if progress and total:
                progress(min(1.0, start / total), start, total)
        return out

    def _library_props(self) -> dict | None:
        try:
            return self._call("AudioLibrary.GetProperties", {"properties": LIB_PROPS})
        except KodiError:
            return None  # Kodi < 19

    # -- public ----------------------------------------------------------
    def run(self, full: bool = False, progress=None, force: bool = False) -> SyncResult:
        """Bring the cache up to date. ``progress(stage, fraction)`` is optional."""
        t0 = time.monotonic()
        self._requests = 0
        progress = progress or (lambda stage, frac: None)
        result = SyncResult()

        progress("Checking for changes", None)
        self.client.get_api_version()
        lib = self._library_props()
        first = full or not self.cache.get_meta("synced")
        # (A rebuild refetches everything but keeps the old rows until the new ones are
        # in, so the library stays browsable and an interrupted rebuild loses nothing.)
        stored = self.cache.get_meta_json("library_props")
        recent = time.time() - float(self.cache.get_meta("synced") or 0) < OLD_KODI_RECHECK

        # Kodi < 19 can't tell us whether anything changed; don't re-diff the whole
        # library on every start — live events and manual syncs cover the gap.
        unchanged = (lib is not None and stored == lib) or (lib is None and recent and not force)
        if not first and unchanged:
            result.skipped = True
            result.seconds = time.monotonic() - t0
            result.requests = self._requests
            self.cache.set_meta("last_check", str(int(time.time())))
            return result

        changed_keys = None
        if not first and lib is not None and isinstance(stored, dict):
            changed_keys = {k for k in lib if lib.get(k) != stored.get(k)}

        result.full = first
        for kind, label in (("artist", "Artists"), ("album", "Albums"), ("song", "Songs")):
            if changed_keys is not None and not (changed_keys & KIND_KEYS[kind]):
                continue
            def stage(frac, done=None, total=None, label=label):
                if done is not None and total:
                    progress(f"{label} {done:,} / {total:,}", frac)
                else:
                    progress(label, frac)
            if first:
                self._full_kind(kind, result, stage)
            else:
                self._incremental_kind(kind, result, stage)

        if lib is not None:
            self.cache.set_meta("library_props", lib)
        now = str(int(time.time()))
        self.cache.set_meta("synced", now)
        self.cache.set_meta("last_check", now)
        self.cache.set_meta("last_sync_summary", {
            "full": result.full, "added": result.added,
            "updated": result.updated, "removed": result.removed,
        })
        result.seconds = time.monotonic() - t0
        result.requests = self._requests
        return result

    def _upsert(self, kind: str, items: list[dict]):
        {"artist": self.cache.upsert_artists,
         "album": self.cache.upsert_albums,
         "song": self.cache.upsert_songs}[kind](items)

    def _full_kind(self, kind: str, result: SyncResult, progress):
        """Fetch everything of one kind, storing page by page. If it gets interrupted
        (quit, Kodi gone) the next run continues where it stopped."""
        _, _, idkey, *_ = METHODS[kind]
        resume_key = f"resume_{kind}"
        start = int(self.cache.get_meta(resume_key) or 0)
        local = self.cache.ids(kind)
        fetched = 0

        def on_page(items, next_start):
            nonlocal fetched
            self._upsert(kind, items)
            fetched += len(items)
            self.cache.set_meta(resume_key, str(next_start))

        self._paged(kind, self._props_for(kind), progress=progress, on_page=on_page, start=start)
        remote = {i[idkey] for i in self._paged(kind, [])}
        result.removed[kind] += self.cache.delete(kind, local - remote)
        result.added[kind] += len(remote - local)
        result.updated[kind] += len(remote & local)
        self.cache.set_meta(resume_key, "")

    def _incremental_kind(self, kind: str, result: SyncResult, progress):
        _, _, idkey, dmethod, dkey = METHODS[kind]
        progress(0.0)
        local = self.cache.ids(kind)
        remote = {i[idkey] for i in self._paged(kind, [])}
        removed = local - remote
        result.removed[kind] += self.cache.delete(kind, removed)
        local -= removed

        if kind == "artist":
            # Artists are few and cheap; refetch them when anything changed so
            # renamed artists / new thumbnails come through.
            items = self._paged(kind, self._props_for(kind), progress=progress)
            self._upsert(kind, items)
            ids = {i[idkey] for i in items}
            result.added[kind] += len(ids - local)
            result.updated[kind] += len(ids & local)
            return

        changed_ids: set[int] = set()
        wm = self.cache.watermark(kind)
        if wm and self._date_filter_ok.get(kind, True) and "dateadded" in self._props_for(kind):
            since = _day_before(wm)
            rules = [{"field": "dateadded", "operator": "after", "value": since}]
            if "datemodified" in self._props_for(kind):
                rules.append({"field": "datemodified", "operator": "after", "value": since})
            flt = {"or": rules} if len(rules) > 1 else rules[0]
            try:
                items = self._paged(kind, self._props_for(kind), flt)
            except KodiError:
                try:  # datemodified may not be a valid filter field; try dateadded alone
                    items = self._paged(kind, self._props_for(kind), rules[0])
                except KodiError:
                    self._date_filter_ok[kind] = False
                    items = []
            items = [i for i in items if i[idkey] in remote]
            self._upsert(kind, items)
            changed_ids = {i[idkey] for i in items}
            # The day of overlap re-fetches a few unchanged items; only count
            # the ones that are actually newer than our watermark.
            newer = {i[idkey] for i in items
                     if max(i.get("dateadded") or "", i.get("datemodified") or "") > wm}
            result.updated[kind] += len(newer & local)
            result.added[kind] += len(changed_ids - local)
        progress(0.5)

        missing = remote - local - changed_ids
        if len(missing) > FULL_FETCH_THRESHOLD:
            items = self._paged(kind, self._props_for(kind), progress=progress)
            self._upsert(kind, items)
            result.added[kind] += len(missing)
        elif missing:
            got = self.fetch_details(kind, sorted(missing))
            result.added[kind] += len(got)
        progress(1.0)

    def fetch_details(self, kind: str, ids: list[int]) -> list[dict]:
        """Fetch items by id using batched JSON-RPC requests and cache them."""
        _, _, idkey, dmethod, dkey = METHODS[kind]
        props = self._props_for(kind)
        out = []
        for i in range(0, len(ids), DETAILS_BATCH):
            chunk = ids[i:i + DETAILS_BATCH]
            self._requests += 1
            res = self.client.batch([(dmethod, {idkey: x, "properties": props}) for x in chunk],
                                    timeout=SYNC_TIMEOUT)
            for r in res:
                if isinstance(r, dict) and dkey in r:
                    out.append(r[dkey])
        self._upsert(kind, out)
        return out

    # -- targeted updates from notifications -----------------------------
    def apply_update(self, kind: str, item_id: int) -> bool:
        if kind not in METHODS:
            return False
        got = self.fetch_details(kind, [item_id])
        if kind == "song" and got:
            alb = got[0].get("albumid")
            if alb and not self.cache.album(alb):
                self.fetch_details("album", [alb])
        return bool(got)

    def apply_remove(self, kind: str, item_id: int) -> bool:
        if kind not in METHODS:
            return False
        return self.cache.delete(kind, [item_id]) > 0


def _day_before(ts: str) -> str:
    """Kodi date filters compare by day; step back one day for overlap."""
    try:
        d = _dt.datetime.strptime(ts[:10], "%Y-%m-%d").date()
        return (d - _dt.timedelta(days=1)).isoformat()
    except ValueError:
        return "1970-01-01"
