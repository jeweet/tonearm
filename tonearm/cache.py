"""SQLite cache of a Kodi music library, one database per instance."""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from dataclasses import dataclass

SCHEMA_VERSION = 3  # v3: album fanart, artist art fallbacks

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS artists (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    sortname TEXT,
    genre TEXT,
    thumbnail TEXT,
    fanart TEXT,
    description TEXT,
    dateadded TEXT
);
CREATE TABLE IF NOT EXISTS albums (
    id INTEGER PRIMARY KEY,
    title TEXT NOT NULL,
    artist TEXT,
    year INTEGER,
    genre TEXT,
    thumbnail TEXT,
    fanart TEXT,
    dateadded TEXT,
    datemodified TEXT
);
CREATE TABLE IF NOT EXISTS album_artists (
    albumid INTEGER NOT NULL,
    artistid INTEGER NOT NULL,
    position INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (albumid, artistid)
);
CREATE TABLE IF NOT EXISTS songs (
    id INTEGER PRIMARY KEY,
    albumid INTEGER,
    title TEXT NOT NULL,
    artist TEXT,
    track INTEGER,
    disc INTEGER,
    duration INTEGER,
    dateadded TEXT,
    datemodified TEXT
);
-- Kodi's user ratings (1–10) of songs and albums; only rated items (kind: song / album)
CREATE TABLE IF NOT EXISTS ratings (
    kind TEXT NOT NULL,
    id INTEGER NOT NULL,
    rating INTEGER NOT NULL,
    PRIMARY KEY (kind, id)
);
CREATE INDEX IF NOT EXISTS songs_album ON songs(albumid);
CREATE INDEX IF NOT EXISTS aa_artist ON album_artists(artistid);
CREATE INDEX IF NOT EXISTS albums_added ON albums(dateadded);
"""


@dataclass
class Album:
    id: int
    title: str
    artist: str
    year: int
    genre: str
    thumbnail: str
    dateadded: str
    song_count: int = 0
    duration: int = 0
    rating: int = 0        # Kodi's user rating, 0 (none) … 10


@dataclass
class Artist:
    id: int
    name: str
    genre: str
    thumbnail: str
    fanart: str
    description: str
    album_count: int = 0
    song_count: int = 0
    duration: int = 0
    year_from: int = 0
    year_to: int = 0
    sortname: str = ""

    @property
    def years(self) -> str:
        if not self.year_from:
            return ""
        return str(self.year_from) if self.year_from == self.year_to else f"{self.year_from}–{self.year_to}"


@dataclass
class Song:
    id: int
    albumid: int
    title: str
    artist: str
    track: int
    disc: int
    duration: int


def _art(item: dict, prop: str, *art_keys: str) -> str:
    """A Kodi image path from the plain property, else from the ``art`` map."""
    v = item.get(prop)
    if v:
        return v
    art = item.get("art") or {}
    for k in art_keys:
        if art.get(k):
            return art[k]
    return ""


def _various(name) -> bool:
    return (name or "").strip().lower() in ("various artists", "various", "va")


def _join(v) -> str:
    if isinstance(v, list):
        return ", ".join(x for x in v if x)
    return v or ""


class _NoLock:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class LibraryCache:
    """SQLite library cache, safe to use from any thread.

    Every thread gets its own connection to the WAL-mode database, so the UI can
    read while a sync is writing — nothing waits on a Python lock. SQLite itself
    serialises the (few) writers. ``:memory:`` databases (tests) can't be shared
    between connections, so they fall back to one connection behind a lock.
    """

    def __init__(self, path: str):
        self.path = path
        self._memory = path == ":memory:"
        if not self._memory:
            os.makedirs(os.path.dirname(path), exist_ok=True)
        self._local = threading.local()
        self._conns: list = []
        self._conns_lock = threading.Lock()
        self._closed = False
        self._lock = threading.RLock() if self._memory else _NoLock()
        self._shared = self._open() if self._memory else None
        with self._lock:
            self._db.executescript(SCHEMA)
            if self.get_meta("schema") != str(SCHEMA_VERSION):
                # Layout changed: rebuild the tables; the next sync refills them.
                for t in ("artists", "albums", "album_artists", "songs", "ratings", "meta"):
                    self._db.execute(f"DROP TABLE IF EXISTS {t}")
                self._db.executescript(SCHEMA)
                self._reset_locked()

    def _open(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, check_same_thread=False, timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA temp_store=MEMORY")
        conn.execute("PRAGMA cache_size=-8000")  # ~8 MB page cache per connection
        with self._conns_lock:
            self._conns.append(conn)
        return conn

    @property
    def _db(self) -> sqlite3.Connection:
        if self._shared is not None:
            return self._shared
        conn = getattr(self._local, "conn", None)
        if conn is None:
            if self._closed:
                raise sqlite3.ProgrammingError("cache is closed")
            conn = self._local.conn = self._open()
        return conn

    def close(self):
        self._closed = True
        with self._conns_lock:
            conns, self._conns = self._conns, []
        for c in conns:
            try:
                c.close()
            except Exception:  # noqa: BLE001
                pass

    def _reset_locked(self):
        for t in ("artists", "albums", "album_artists", "songs", "ratings", "meta"):
            self._db.execute(f"DELETE FROM {t}")
        self._db.execute("INSERT OR REPLACE INTO meta VALUES ('schema', ?)", (str(SCHEMA_VERSION),))
        self._db.commit()

    def reset(self):
        with self._lock:
            self._reset_locked()

    # -- meta ------------------------------------------------------------
    def get_meta(self, key: str, default=None):
        with self._lock:
            row = self._db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    def get_meta_json(self, key: str, default=None):
        v = self.get_meta(key)
        if v is None:
            return default
        try:
            return json.loads(v)
        except ValueError:
            return default

    def set_meta(self, key: str, value):
        if not isinstance(value, str):
            value = json.dumps(value)
        with self._lock:
            self._db.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (key, value))
            self._db.commit()

    # -- writes ----------------------------------------------------------
    def upsert_artists(self, artists: list[dict]):
        rows = [(
            a["artistid"], a.get("artist") or a.get("label") or "",
            a.get("sortname") or "", _join(a.get("genre")), _art(a, "thumbnail", "thumb"),
            _art(a, "fanart", "fanart", "fanart1"), a.get("description") or "",
            a.get("dateadded") or "",
        ) for a in artists]
        with self._lock:
            self._db.executemany(
                """INSERT INTO artists (id,name,sortname,genre,thumbnail,fanart,description,dateadded)
                   VALUES (?,?,?,?,?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET name=excluded.name, sortname=excluded.sortname,
                   genre=excluded.genre, thumbnail=excluded.thumbnail, fanart=excluded.fanart,
                   description=CASE WHEN excluded.description != '' THEN excluded.description
                                    ELSE artists.description END,
                   dateadded=excluded.dateadded""", rows)
            self._db.commit()

    def set_artist_description(self, artistid: int, description: str):
        with self._lock:
            self._db.execute("UPDATE artists SET description=? WHERE id=?", (description, artistid))
            self._db.commit()

    def upsert_albums(self, albums: list[dict]):
        rows, links, ids = [], [], []
        for a in albums:
            aid = a["albumid"]
            ids.append((aid,))
            rows.append((
                aid, a.get("title") or a.get("label") or "",
                a.get("displayartist") or _join(a.get("artist")),
                int(a.get("year") or 0), _join(a.get("genre")), _art(a, "thumbnail", "thumb"),
                _art(a, "fanart", "fanart", "artist.fanart", "albumartist.fanart"),
                a.get("dateadded") or "", a.get("datemodified") or "",
            ))
            for pos, artid in enumerate(a.get("artistid") or []):
                links.append((aid, artid, pos))
        with self._lock:
            self._db.executemany(
                """INSERT OR REPLACE INTO albums
                   (id,title,artist,year,genre,thumbnail,fanart,dateadded,datemodified)
                   VALUES (?,?,?,?,?,?,?,?,?)""", rows)
            self._db.executemany("DELETE FROM album_artists WHERE albumid=?", ids)
            self._db.executemany("INSERT OR IGNORE INTO album_artists VALUES (?,?,?)", links)
            self._db.commit()

    def upsert_songs(self, songs: list[dict]):
        rows = [(
            s["songid"], s.get("albumid") or 0, s.get("title") or s.get("label") or "",
            s.get("displayartist") or _join(s.get("artist")),
            int(s.get("track") or 0), int(s.get("disc") or 0), int(s.get("duration") or 0),
            s.get("dateadded") or "", s.get("datemodified") or "",
        ) for s in songs]
        with self._lock:
            self._db.executemany(
                """INSERT OR REPLACE INTO songs
                   (id,albumid,title,artist,track,disc,duration,dateadded,datemodified)
                   VALUES (?,?,?,?,?,?,?,?,?)""", rows)
            self._db.commit()

    def delete(self, kind: str, ids) -> int:
        ids = [(i,) for i in ids]
        if not ids:
            return 0
        table = {"artist": "artists", "album": "albums", "song": "songs"}[kind]
        with self._lock:
            self._db.executemany(f"DELETE FROM {table} WHERE id=?", ids)
            if kind == "album":
                self._db.executemany("DELETE FROM album_artists WHERE albumid=?", ids)
                self._db.executemany("DELETE FROM songs WHERE albumid=?", ids)
            elif kind == "artist":
                self._db.executemany("DELETE FROM album_artists WHERE artistid=?", ids)
            self._db.commit()
        return len(ids)

    # -- reads -----------------------------------------------------------
    def ids(self, kind: str) -> set[int]:
        table = {"artist": "artists", "album": "albums", "song": "songs"}[kind]
        with self._lock:
            return {r[0] for r in self._db.execute(f"SELECT id FROM {table}")}

    def count(self, kind: str) -> int:
        table = {"artist": "artists", "album": "albums", "song": "songs"}[kind]
        with self._lock:
            return self._db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    def watermark(self, kind: str) -> str:
        """Latest dateadded/datemodified seen for albums or songs (Kodi's own clock)."""
        table = {"album": "albums", "song": "songs"}[kind]
        with self._lock:
            r = self._db.execute(
                f"SELECT MAX(MAX(dateadded), MAX(datemodified)) FROM {table}").fetchone()
        return r[0] or ""

    def albums(self, order: str = "title", limit: int | None = None) -> list[Album]:
        order_sql = {
            "title": "a.title COLLATE NOCASE",
            "artist": "a.artist COLLATE NOCASE, a.year, a.title COLLATE NOCASE",
            "year": "a.year DESC, a.artist COLLATE NOCASE",
            "recent": "a.dateadded DESC, a.id DESC",
            "rating": "COALESCE(r.rating, 0) DESC, a.artist COLLATE NOCASE, a.title COLLATE NOCASE",
        }.get(order, "a.title COLLATE NOCASE")
        sql = ("SELECT a.id,a.title,a.artist,a.year,a.genre,a.thumbnail,a.dateadded, 0, 0, "
               "COALESCE(r.rating, 0) FROM albums a "
               "LEFT JOIN ratings r ON r.kind = 'album' AND r.id = a.id "
               f"ORDER BY {order_sql}")
        if limit:
            sql += f" LIMIT {int(limit)}"
        with self._lock:
            return [Album(*r) for r in self._db.execute(sql)]

    # -- ratings (Kodi's user ratings, 1–10) -----------------------------------
    def ratings(self, kind: str) -> dict[int, int]:
        with self._lock:
            return dict(self._db.execute("SELECT id, rating FROM ratings WHERE kind=?", (kind,)))

    def set_rating(self, kind: str, itemid: int, rating: int):
        with self._lock:
            if rating > 0:
                self._db.execute("INSERT OR REPLACE INTO ratings VALUES (?,?,?)",
                                 (kind, itemid, int(rating)))
            else:
                self._db.execute("DELETE FROM ratings WHERE kind=? AND id=?", (kind, itemid))
            self._db.commit()

    def replace_ratings(self, kind: str, ratings: dict[int, int]):
        with self._lock:
            self._db.execute("DELETE FROM ratings WHERE kind=?", (kind,))
            self._db.executemany("INSERT OR REPLACE INTO ratings VALUES (?,?,?)",
                                 [(kind, i, int(r)) for i, r in ratings.items() if r > 0])
            self._db.commit()

    def top_rated_albums(self, limit: int = 300) -> list[Album]:
        """Rated albums, best first (then A→Z by artist), with song count and length."""
        with self._lock:
            return [Album(*r) for r in self._db.execute(
                """SELECT a.id,a.title,a.artist,a.year,a.genre,a.thumbnail,a.dateadded,
                          (SELECT COUNT(*) FROM songs s WHERE s.albumid = a.id),
                          (SELECT COALESCE(SUM(s.duration), 0) FROM songs s WHERE s.albumid = a.id),
                          r.rating
                   FROM ratings r JOIN albums a ON a.id = r.id
                   WHERE r.kind = 'album' AND r.rating > 0
                   ORDER BY r.rating DESC, a.artist COLLATE NOCASE, a.year, a.title COLLATE NOCASE
                   LIMIT ?""", (int(limit),))]

    def top_rated_songs(self, limit: int = 500) -> list[tuple]:
        """(Song, rating, album title, album thumbnail) of rated songs, best first."""
        with self._lock:
            rows = self._db.execute(
                """SELECT s.id,s.albumid,s.title,s.artist,s.track,s.disc,s.duration, r.rating,
                          COALESCE(a.title, ''), COALESCE(a.thumbnail, '')
                   FROM ratings r JOIN songs s ON s.id = r.id
                   LEFT JOIN albums a ON a.id = s.albumid
                   WHERE r.kind = 'song' AND r.rating > 0
                   ORDER BY r.rating DESC, s.artist COLLATE NOCASE, a.title COLLATE NOCASE,
                            s.disc, s.track
                   LIMIT ?""", (int(limit),)).fetchall()
        return [(Song(*r[:7]), r[7], r[8], r[9]) for r in rows]

    def album_track_order(self, albumids) -> dict:
        """albumid → its song ids in play order (disc, track)."""
        ids = list(albumids)
        if not ids:
            return {}
        marks = ",".join("?" * len(ids))
        out: dict = {}
        with self._lock:
            for aid, sid in self._db.execute(
                    f"SELECT albumid, id FROM songs WHERE albumid IN ({marks}) "
                    "ORDER BY albumid, disc, track, id", ids):
                out.setdefault(aid, []).append(sid)
        return out

    def recent_albums(self, limit: int = 120) -> list[Album]:
        """Newest albums first, with song count and length (Spotlight page)."""
        with self._lock:
            return [Album(*r) for r in self._db.execute(
                """WITH recent AS (SELECT id,title,artist,year,genre,thumbnail,dateadded FROM albums
                                  ORDER BY dateadded DESC, id DESC LIMIT ?)
                   SELECT r.*, COUNT(s.id), COALESCE(SUM(s.duration), 0)
                   FROM recent r LEFT JOIN songs s ON s.albumid = r.id
                   GROUP BY r.id ORDER BY r.dateadded DESC, r.id DESC""", (int(limit),))]

    def spotlight_artists(self, album_ids: list[int], n: int = 3,
                          exclude=()) -> list[tuple[int, list[int]]]:
        """Artists with the most of these (recent) albums — ties go to the one added most
        recently; compilations' "Various Artists" and ``exclude`` (artist ids already shown
        elsewhere) never count. [(artistid, album ids)]."""
        if not album_ids:
            return []
        order = {aid: i for i, aid in enumerate(album_ids)}
        marks = ",".join("?" * len(album_ids))
        with self._lock:
            rows = self._db.execute(
                f"""SELECT aa.artistid, aa.albumid, ar.name FROM album_artists aa
                    JOIN artists ar ON ar.id = aa.artistid
                    WHERE aa.albumid IN ({marks})""", list(album_ids)).fetchall()
        per: dict[int, list[int]] = {}
        for artistid, albumid, name in rows:
            if artistid in exclude or _various(name):
                continue
            per.setdefault(artistid, []).append(albumid)
        ranked = sorted(per, key=lambda a: (-len(per[a]), min(order[x] for x in per[a])))
        return [(a, sorted(per[a], key=order.get)) for a in ranked[:n]]

    def random_artist(self, exclude=()) -> int | None:
        """A random artist from the library to rediscover: one with albums, preferably
        with a picture or a biography to show. Never "Various Artists"."""
        ex = list(exclude) or [-1]
        marks = ",".join("?" * len(ex))
        base = f"""SELECT ar.id FROM artists ar
                   WHERE ar.id NOT IN ({marks})
                     AND LOWER(TRIM(ar.name)) NOT IN ('various artists', 'various', 'va')
                     AND EXISTS (SELECT 1 FROM album_artists aa WHERE aa.artistid = ar.id)"""
        with self._lock:
            for extra in (" AND (ar.thumbnail != '' OR ar.fanart != '' OR ar.description != '')", ""):
                r = self._db.execute(base + extra + " ORDER BY RANDOM() LIMIT 1", ex).fetchone()
                if r is not None:
                    return r[0]
        return None

    def album(self, albumid: int) -> Album | None:
        with self._lock:
            r = self._db.execute(
                "SELECT id,title,artist,year,genre,thumbnail,dateadded FROM albums WHERE id=?",
                (albumid,)).fetchone()
        return Album(*r) if r else None

    def album_fanart(self, albumid: int) -> str:
        """Best wide artwork for an album page: album-level artist fanart, then any
        of its artists' fanart."""
        with self._lock:
            r = self._db.execute(
                """SELECT COALESCE(NULLIF(al.fanart, ''),
                          (SELECT ar.fanart FROM album_artists aa JOIN artists ar ON ar.id = aa.artistid
                           WHERE aa.albumid = al.id AND ar.fanart != '' ORDER BY aa.position LIMIT 1),
                          '')
                   FROM albums al WHERE al.id = ?""", (albumid,)).fetchone()
        return r[0] if r else ""

    def set_artist_art(self, artistid: int, thumbnail: str, fanart: str):
        with self._lock:
            self._db.execute(
                """UPDATE artists SET thumbnail = COALESCE(NULLIF(?, ''), thumbnail),
                                      fanart = COALESCE(NULLIF(?, ''), fanart) WHERE id = ?""",
                (thumbnail, fanart, artistid))
            self._db.commit()

    # -- genres & decades (derived from album tags; no extra sync needed) -----
    @staticmethod
    def split_genres(genre: str) -> list[str]:
        out = []
        for part in (genre or "").replace(" / ", ",").replace(";", ",").split(","):
            g = part.strip()
            if g and g not in out:
                out.append(g)
        return out

    def genres(self) -> list[tuple[str, int, str]]:
        """(genre, album count, a cover) sorted by name; from the albums' genre tags."""
        counts: dict = {}
        covers: dict = {}
        with self._lock:
            rows = self._db.execute(
                "SELECT genre, thumbnail FROM albums WHERE genre != '' ORDER BY dateadded DESC").fetchall()
        names: dict = {}
        for genre, thumb in rows:
            for g in self.split_genres(genre):
                key = g.lower()
                if key not in names or (names[key].islower() and not g.islower()):
                    names[key] = g  # prefer "Rock" over "rock"
                counts[key] = counts.get(key, 0) + 1
                if thumb and key not in covers:
                    covers[key] = thumb  # newest album's cover represents the genre
        return sorted(((names[k], n, covers.get(k, "")) for k, n in counts.items()),
                      key=lambda t: t[0].lower())

    def albums_for_genre(self, genre: str) -> list[Album]:
        want = genre.lower()
        with self._lock:
            rows = self._db.execute(
                """SELECT id,title,artist,year,genre,thumbnail,dateadded FROM albums
                   WHERE genre LIKE ? ORDER BY artist COLLATE NOCASE, year, title COLLATE NOCASE""",
                (f"%{genre}%",)).fetchall()
        return [Album(*r) for r in rows if want in (g.lower() for g in self.split_genres(r[4]))]

    def genre_rows(self) -> list[tuple[str, str, str]]:
        """(genre tag, thumbnail, artist) of every tagged album, newest first — for
        genres.overview()."""
        with self._lock:
            return self._db.execute(
                "SELECT genre, COALESCE(thumbnail, ''), COALESCE(artist, '') FROM albums "
                "WHERE genre != '' ORDER BY dateadded DESC").fetchall()

    def top_artists_for_albums(self, album_ids: list[int], limit: int = 12
                               ) -> list[tuple[int, str, str, int]]:
        """(artist id, name, thumbnail, albums among ``album_ids``), most albums first.
        Album artists only (not "Various Artists"-style compilations' track artists)."""
        if not album_ids:
            return []
        out: dict[int, list] = {}
        with self._lock:
            for i in range(0, len(album_ids), 500):
                chunk = album_ids[i:i + 500]
                q = ",".join("?" * len(chunk))
                for aid, name, thumb, n in self._db.execute(
                        f"""SELECT ar.id, ar.name, COALESCE(ar.thumbnail, ''), COUNT(*)
                            FROM album_artists aa JOIN artists ar ON ar.id = aa.artistid
                            WHERE aa.albumid IN ({q}) GROUP BY ar.id""", chunk):
                    if aid in out:
                        out[aid][3] += n
                    else:
                        out[aid] = [aid, name, thumb, n]
        rows = [r for r in out.values() if (r[1] or "").lower() not in ("various artists", "")]
        rows.sort(key=lambda r: (-r[3], (r[1] or "").lower()))
        return [tuple(r) for r in rows[:limit]]

    def decades(self) -> list[tuple[int, int, str]]:
        """(decade start year, album count, a cover), newest decade first; 0 = unknown year."""
        with self._lock:
            rows = self._db.execute(
                "SELECT year, thumbnail FROM albums ORDER BY dateadded DESC").fetchall()
        counts: dict = {}
        covers: dict = {}
        for year, thumb in rows:
            d = (year // 10) * 10 if year and year > 0 else 0
            counts[d] = counts.get(d, 0) + 1
            if thumb and d not in covers:
                covers[d] = thumb
        return [(d, counts[d], covers.get(d, "")) for d in sorted(counts, reverse=True)]

    def decade_covers(self, per: int = 3) -> dict[int, list[str]]:
        """decade → up to ``per`` covers from different artists, most recently added
        first (for the Decades tiles)."""
        with self._lock:
            rows = self._db.execute(
                "SELECT year, thumbnail, artist FROM albums WHERE thumbnail != '' "
                "ORDER BY dateadded DESC").fetchall()
        out: dict[int, list[str]] = {}
        seen: dict[int, set] = {}
        for year, thumb, artist in rows:
            d = (year // 10) * 10 if year and year > 0 else 0
            got = out.setdefault(d, [])
            who = seen.setdefault(d, set())
            if len(got) < per and (artist or "").lower() not in who:
                got.append(thumb)
                who.add((artist or "").lower())
        return out

    # -- radio ("what should I play?") -----------------------------------
    def radio_album_ids(self, decade: int | None, genre: str | None) -> list[int] | None:
        """Albums matching a decade (0 = unknown year) and/or genre; None = no filter."""
        if decade is None and not genre:
            return None
        where, params = [], []
        if decade is not None:
            if decade == 0:
                where.append("(year <= 0 OR year IS NULL)")
            else:
                where.append("year BETWEEN ? AND ?")
                params += [decade, decade + 9]
        if genre:
            where.append("genre LIKE ?")
            params.append(f"%{genre}%")
        with self._lock:
            rows = self._db.execute(f"SELECT id, genre FROM albums WHERE {' AND '.join(where)}",
                                    params).fetchall()
        if genre:
            want = genre.lower()
            rows = [r for r in rows if want in (g.lower() for g in self.split_genres(r[1]))]
        return [r[0] for r in rows]

    def radio_count(self, album_ids: list[int] | None) -> int:
        with self._lock:
            if album_ids is None:
                return self._db.execute("SELECT COUNT(*) FROM songs").fetchone()[0]
            return self._db.execute(
                "SELECT COUNT(*) FROM songs WHERE albumid IN (SELECT value FROM json_each(?))",
                (json.dumps(album_ids),)).fetchone()[0]

    def radio_songs(self, album_ids: list[int] | None, limit: int, exclude=()) -> list[int]:
        """Random song ids from those albums (or the whole library), skipping ``exclude``."""
        excl = json.dumps(list(exclude))
        with self._lock:
            if album_ids is None:
                rows = self._db.execute(
                    """SELECT id FROM songs WHERE id NOT IN (SELECT value FROM json_each(?))
                       ORDER BY RANDOM() LIMIT ?""", (excl, limit))
            else:
                rows = self._db.execute(
                    """SELECT id FROM songs WHERE albumid IN (SELECT value FROM json_each(?))
                         AND id NOT IN (SELECT value FROM json_each(?))
                       ORDER BY RANDOM() LIMIT ?""", (json.dumps(album_ids), excl, limit))
            return [r[0] for r in rows]

    def albums_for_decade(self, decade: int) -> list[Album]:
        with self._lock:
            if decade == 0:
                q = ("SELECT id,title,artist,year,genre,thumbnail,dateadded FROM albums "
                     "WHERE year <= 0 OR year IS NULL ORDER BY artist COLLATE NOCASE, title", ())
            else:
                q = ("SELECT id,title,artist,year,genre,thumbnail,dateadded FROM albums "
                     "WHERE year BETWEEN ? AND ? ORDER BY year DESC, artist COLLATE NOCASE, title",
                     (decade, decade + 9))
            return [Album(*r) for r in self._db.execute(*q)]

    def albums_for_artist(self, artistid: int) -> list[Album]:
        with self._lock:
            return [Album(*r) for r in self._db.execute(
                """SELECT a.id,a.title,a.artist,a.year,a.genre,a.thumbnail,a.dateadded,
                          COUNT(s.id), COALESCE(SUM(s.duration), 0)
                   FROM albums a JOIN album_artists aa ON aa.albumid=a.id
                   LEFT JOIN songs s ON s.albumid=a.id
                   WHERE aa.artistid=? GROUP BY a.id
                   ORDER BY a.year DESC, a.title COLLATE NOCASE""",
                (artistid,))]

    def artists(self) -> list[Artist]:
        with self._lock:
            return [Artist(*r) for r in self._db.execute(
                """WITH alb AS (
                       SELECT aa.artistid, COUNT(*) AS n,
                              MIN(NULLIF(a.year, 0)) AS y0, MAX(NULLIF(a.year, 0)) AS y1
                       FROM album_artists aa JOIN albums a ON a.id = aa.albumid
                       GROUP BY aa.artistid),
                   sng AS (
                       SELECT aa.artistid, COUNT(s.id) AS n, SUM(s.duration) AS d
                       FROM album_artists aa JOIN songs s ON s.albumid = aa.albumid
                       GROUP BY aa.artistid)
                   SELECT ar.id, ar.name,
                          COALESCE(NULLIF(ar.genre, ''), (SELECT a.genre FROM album_artists aa JOIN albums a ON a.id = aa.albumid
                             WHERE aa.artistid = ar.id AND a.genre != ''
                             GROUP BY a.genre ORDER BY COUNT(*) DESC LIMIT 1), ''),
                          ar.thumbnail, ar.fanart, ar.description,
                          COALESCE(alb.n, 0), COALESCE(sng.n, 0), COALESCE(sng.d, 0),
                          COALESCE(alb.y0, 0), COALESCE(alb.y1, 0), COALESCE(ar.sortname, '')
                   FROM artists ar
                   LEFT JOIN alb ON alb.artistid = ar.id
                   LEFT JOIN sng ON sng.artistid = ar.id
                   ORDER BY COALESCE(NULLIF(ar.sortname,''), ar.name) COLLATE NOCASE""")]

    def artist_names(self) -> list[tuple[int, str]]:
        """Just (id, name) — cheap, for name matching."""
        with self._lock:
            return [(r[0], r[1]) for r in self._db.execute("SELECT id, name FROM artists")]

    def artist(self, artistid: int) -> Artist | None:
        with self._lock:
            r = self._db.execute(
                """SELECT ar.id, ar.name,
                          COALESCE(NULLIF(ar.genre, ''), (SELECT a.genre FROM album_artists aa JOIN albums a ON a.id = aa.albumid
                             WHERE aa.artistid = ar.id AND a.genre != ''
                             GROUP BY a.genre ORDER BY COUNT(*) DESC LIMIT 1), ''),
                          ar.thumbnail, ar.fanart, ar.description,
                          (SELECT COUNT(*) FROM album_artists aa WHERE aa.artistid=ar.id),
                          (SELECT COUNT(s.id) FROM album_artists aa JOIN songs s
                             ON s.albumid=aa.albumid WHERE aa.artistid=ar.id),
                          (SELECT COALESCE(SUM(s.duration), 0) FROM album_artists aa JOIN songs s
                             ON s.albumid=aa.albumid WHERE aa.artistid=ar.id),
                          (SELECT COALESCE(MIN(NULLIF(a.year, 0)), 0) FROM album_artists aa JOIN albums a
                             ON a.id=aa.albumid WHERE aa.artistid=ar.id),
                          (SELECT COALESCE(MAX(NULLIF(a.year, 0)), 0) FROM album_artists aa JOIN albums a
                             ON a.id=aa.albumid WHERE aa.artistid=ar.id)
                   FROM artists ar WHERE ar.id=?""", (artistid,)).fetchone()
        return Artist(*r) if r else None

    def album_artist_ids(self, albumid: int) -> list[int]:
        with self._lock:
            return [r[0] for r in self._db.execute(
                "SELECT artistid FROM album_artists WHERE albumid=? ORDER BY position", (albumid,))]

    def songs_for_album(self, albumid: int) -> list[Song]:
        with self._lock:
            return [Song(*r) for r in self._db.execute(
                """SELECT id,albumid,title,artist,track,disc,duration FROM songs
                   WHERE albumid=? ORDER BY disc, track, title""", (albumid,))]

    def song(self, songid: int) -> Song | None:
        with self._lock:
            r = self._db.execute(
                "SELECT id,albumid,title,artist,track,disc,duration FROM songs WHERE id=?",
                (songid,)).fetchone()
        return Song(*r) if r else None

    def search_entries(self):
        """Rows for the fuzzy index: (kind, id, title, subtitle, thumbnail)."""
        with self._lock:
            out = [("artist", r[0], r[1], r[2] or "Artist", r[3] or "")
                   for r in self._db.execute("SELECT id,name,genre,thumbnail FROM artists")]
            out += [("album", r[0], r[1], f"{r[2]}" + (f" · {r[3]}" if r[3] else ""), r[4] or "")
                    for r in self._db.execute("SELECT id,title,artist,year,thumbnail FROM albums")]
            out += [("song", r[0], r[1], f"{r[2]} · {r[3] or ''}".rstrip(" ·"), r[4] or "")
                    for r in self._db.execute(
                        """SELECT s.id, s.title, s.artist, a.title, a.thumbnail
                           FROM songs s LEFT JOIN albums a ON a.id=s.albumid""")]
        return out

    def size_bytes(self) -> int:
        if self.path == ":memory:":
            return 0
        total = 0
        for suffix in ("", "-wal", "-shm"):
            try:
                total += os.path.getsize(self.path + suffix)
            except OSError:
                pass
        return total
