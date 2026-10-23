"""Core tests: run with `python3 -m unittest discover -s tests` from the project root."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tonearm.cache import LibraryCache  # noqa: E402
from tonearm.fuzzy import FuzzyIndex, match, parse  # noqa: E402
from tonearm.kodi import Instance, KodiClient  # noqa: E402
from tonearm.sync import LibrarySync  # noqa: E402
from tests.mock_kodi import FakeLibrary, serve  # noqa: E402


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.lib = FakeLibrary(n_albums=30, songs_per_album=8)
        self.srv = serve(self.lib)
        inst = Instance("t", "test", "127.0.0.1", self.srv.server_address[1])
        self.client = KodiClient(inst)
        self.cache = LibraryCache(":memory:")
        self.sync = LibrarySync(self.client, self.cache)

    def tearDown(self):
        self.srv.shutdown()

    def test_initial_then_skip(self):
        r = self.sync.run()
        self.assertTrue(r.full)
        self.assertEqual(self.cache.count("album"), 30)
        self.assertEqual(self.cache.count("song"), 240)
        self.assertEqual(self.cache.count("artist"), 10)
        self.lib.calls.clear()
        r = self.sync.run()
        self.assertTrue(r.skipped)
        self.assertEqual(self.lib.calls, ["AudioLibrary.GetProperties"])

    def test_incremental_add_modify_remove(self):
        self.sync.run()
        # add an album, modify one, remove one
        self.lib.add_album(100, 1, "Brand New", "2026-06-01 09:00:00", 5, 9000)
        self.lib.albums[3]["title"] = "Renamed"
        self.lib.albums[3]["datemodified"] = "2026-06-01 09:00:00"
        del self.lib.albums[5]
        for sid in [s for s, v in self.lib.songs.items() if v["albumid"] == 5]:
            del self.lib.songs[sid]
        self.lib.touch("albumslastadded", "albumsmodified", "songslastadded", "librarylastcleaned")
        self.lib.calls.clear()
        r = self.sync.run()
        self.assertFalse(r.full)
        self.assertEqual(r.added["album"], 1)
        self.assertEqual(r.updated["album"], 1)
        self.assertEqual(r.removed["album"], 1)
        self.assertEqual(r.added["song"], 5)
        self.assertEqual(self.cache.album(3).title, "Renamed")
        self.assertIsNone(self.cache.album(5))
        self.assertEqual(len(self.cache.songs_for_album(100)), 5)
        self.assertEqual(self.cache.count("album"), 30)
        # no full refetch of everything happened
        print("incremental calls:", self.lib.calls, "|", r.summary())

    def test_old_kodi_without_properties(self):
        self.lib.api_major = 9
        self.sync.run()
        self.assertEqual(self.cache.count("album"), 30)
        self.lib.add_album(200, 2, "Old Kodi Add", "2026-06-01 09:00:00", 3, 9500)
        self.assertTrue(self.sync.run().skipped)        # automatic check soon after: skipped
        r = self.sync.run(force=True)                   # user pressed Sync
        self.assertEqual(r.added["album"], 1)
        self.assertEqual(r.added["song"], 3)

    def test_notification_update(self):
        self.sync.run()
        self.lib.albums[2]["title"] = "Live edit"
        self.sync.apply_update("album", 2)
        self.assertEqual(self.cache.album(2).title, "Live edit")
        self.sync.apply_remove("album", 2)
        self.assertIsNone(self.cache.album(2))

    def test_art_and_genre_fallbacks(self):
        self.sync.run()
        a = [x for x in self.cache.artists() if x.album_count][0]
        self.assertEqual(a.genre, "Rock")              # from the artist's albums
        self.assertTrue(a.fanart.startswith("image://fanart-"))  # from the art map
        self.assertEqual(self.cache.artist(a.id).genre, "Rock")
        alb = self.cache.albums_for_artist(a.id)[0]
        self.assertEqual(self.cache.album_fanart(alb.id), f"image://fanart-{a.id}.jpg/")

    def test_old_kodi_without_art_keeps_dates(self):
        # a Kodi that rejects "art" must still get dateadded for incremental sync
        orig = self.lib._project

        def project(item, props, idkey):
            if "art" in props:
                raise KeyError("art")
            return orig(item, props, idkey)
        self.lib._project = project
        self.sync.run()
        self.assertNotIn("art", self.sync._props_for("album"))
        self.assertIn("dateadded", self.sync._props_for("album"))
        self.assertTrue(self.cache.watermark("album"))

    def test_slow_kodi_shrinks_pages(self):
        import tonearm.sync as sync_mod
        old = sync_mod.SYNC_TIMEOUT
        sync_mod.SYNC_TIMEOUT = 0.4
        try:
            self.lib.slow_over, self.lib.slow_delay = 120, 1.0
            self.sync.run()
            self.assertEqual(self.cache.count("song"), 240)
            self.assertLessEqual(self.sync._page["song"], 120)
        finally:
            sync_mod.SYNC_TIMEOUT = old

    def test_interrupted_first_sync_resumes(self):
        import tonearm.sync as sync_mod
        from tonearm.kodi import KodiError
        old = sync_mod.PAGE["song"]
        sync_mod.PAGE["song"] = 50
        try:
            self.sync._page["song"] = 50
            self.lib.fail_after = 4   # artists(1) + albums(1) + 2 song pages, then "outage"
            with self.assertRaises(KodiError):
                self.sync.run()
            partial = self.cache.count("song")
            self.assertGreater(partial, 0)
            self.assertLess(partial, 240)
            resume = int(self.cache.get_meta("resume_song"))
            self.lib.fail_after = None
            self.lib.calls.clear()
            self.sync.run()
            self.assertEqual(self.cache.count("song"), 240)
            self.assertEqual(self.cache.get_meta("resume_song"), "")
            self.assertGreater(resume, 0)
        finally:
            sync_mod.PAGE["song"] = old

    def test_genres_and_decades(self):
        c = self.cache
        c.upsert_albums([
            {"albumid": 901, "title": "A", "genre": ["Rock", "Pop"], "year": 1999, "thumbnail": "t1",
             "dateadded": "2024-01-01"},
            {"albumid": 902, "title": "B", "genre": ["rock"], "year": 2003, "thumbnail": "t2",
             "dateadded": "2025-01-01"},
            {"albumid": 903, "title": "C", "genre": ["Jazz / Fusion"], "year": 0}])
        genres = {g: n for g, n, _ in c.genres()}
        self.assertEqual(genres, {"Rock": 2, "Pop": 1, "Jazz": 1, "Fusion": 1})
        self.assertEqual({a.title for a in c.albums_for_genre("ROCK")}, {"A", "B"})
        self.assertNotIn("A", {a.title for a in c.albums_for_genre("Ro")})  # whole genres only
        self.assertEqual([(d, n) for d, n, _ in c.decades()], [(2000, 1), (1990, 1), (0, 1)])
        self.assertEqual(c.decade_covers(), {2000: ["t2"], 1990: ["t1"]})
        c.upsert_artists([{"artistid": 7, "artist": "Ann", "thumbnail": "a7"},
                          {"artistid": 8, "artist": "Various Artists"}])
        c.upsert_albums([{"albumid": 904, "title": "D", "genre": ["Rock"], "year": 2011, "artistid": [7]},
                         {"albumid": 905, "title": "E", "genre": ["Rock"], "year": 2012, "artistid": [7, 8]}])
        self.assertEqual(c.top_artists_for_albums([904, 905, 901]), [(7, "Ann", "a7", 2)])
        self.assertEqual({r[0] for r in c.genre_rows()}, {"Rock, Pop", "rock", "Jazz / Fusion", "Rock"})
        self.assertEqual([a.title for a in c.albums_for_decade(0)], ["C"])

    def test_cancel_stops_sync(self):
        from tonearm.sync import SyncCancelled
        self.sync.cancel()
        with self.assertRaises(SyncCancelled):
            self.sync.run()

    def test_progress_reports_counts(self):
        import tonearm.sync as sync_mod
        old = sync_mod.PAGE["song"]
        sync_mod.PAGE["song"] = 100
        try:
            seen = []
            self.sync.run(progress=lambda stage, frac: seen.append(stage))
            self.assertIn("Songs 240 / 240", seen)
            self.assertTrue(any(s.startswith("Songs 100 / 240") for s in seen))
        finally:
            sync_mod.PAGE["song"] = old

    def test_recent_order(self):
        self.sync.run()
        recent = self.cache.albums("recent", limit=5)
        dates = [a.dateadded for a in recent]
        self.assertEqual(dates, sorted(dates, reverse=True))

    def test_recent_digest_and_spotlight(self):
        c = self.cache
        c.upsert_artists([{"artistid": 1, "artist": "Solo"}, {"artistid": 2, "artist": "Duo"},
                          {"artistid": 3, "artist": "Various Artists"}])
        c.upsert_albums([
            {"albumid": 10, "title": "a", "artistid": [1], "dateadded": "2026-10-10 10:00:00"},
            {"albumid": 11, "title": "b", "artistid": [2], "dateadded": "2026-10-11 10:00:00"},
            {"albumid": 12, "title": "c", "artistid": [2], "dateadded": "2026-10-09 10:00:00"},
            {"albumid": 13, "title": "d", "artistid": [3], "dateadded": "2026-10-12 10:00:00"},
            {"albumid": 14, "title": "e", "artistid": [3], "dateadded": "2026-10-12 09:00:00"},
            {"albumid": 15, "title": "f", "artistid": [3], "dateadded": "2026-10-12 08:00:00"},
        ])
        c.upsert_songs([{"songid": 1, "albumid": 11, "title": "x", "duration": 100},
                        {"songid": 2, "albumid": 11, "title": "y", "duration": 50}])
        recent = c.recent_albums(4)
        self.assertEqual([a.id for a in recent], [13, 14, 15, 11])
        self.assertEqual((recent[3].song_count, recent[3].duration), (2, 150))
        # "Various Artists" never wins; Duo has two albums
        ids = [a.id for a in c.recent_albums(10)]
        self.assertEqual(c.spotlight_artists(ids), [(2, [11, 12]), (1, [10])])
        self.assertEqual(c.spotlight_artists(ids, 1), [(2, [11, 12])])
        self.assertEqual(c.spotlight_artists([]), [])
        self.assertEqual(c.spotlight_artists(ids, exclude={2}), [(1, [10])])
        for _ in range(10):  # artists with albums only, never Various Artists, never excluded
            self.assertIn(c.random_artist(exclude={1}), (2,))
        self.assertIsNone(c.random_artist(exclude={1, 2}))

    def test_recent_page_picks_vary_artists(self):
        from tonearm.digest import pick
        c = self.cache
        c.upsert_artists([{"artistid": i, "artist": f"A{i}",
                           "fanart": "" if i == 7 else f"f{i}.jpg"} for i in range(1, 12)])
        albums, day = [], 0
        # artist 1 dumped a whole discography last; others trickled in before
        for i in range(6):
            albums.append({"albumid": 100 + i, "title": f"one{i}", "artistid": [1],
                           "displayartist": "A1", "dateadded": f"2026-10-17 10:0{i}:00"})
        for art in (2, 2, 3, 4, 5, 5, 6, 7, 8, 9, 10, 11):
            day += 1
            albums.append({"albumid": 200 + day, "title": f"x{day}", "artistid": [art],
                           "displayartist": f"A{art}", "dateadded": f"2026-10-{16 - day:02d} 10:00:00"})
        c.upsert_albums(albums)
        p = pick(c)
        artists = [p["hero"].artist] + [a.artist for a in p["stories"]]
        self.assertEqual(artists[0], "A1")
        self.assertEqual(len(set(artists)), len(artists))          # all different
        spot_ids = [a.id for a, _ in p["spots"]]
        self.assertTrue(spot_ids and not set(spot_ids) & {1, 2, 3, 4, 5})
        self.assertEqual(spot_ids, [6, 8, 9])         # 3 spots, newest first, only with fanart
        self.assertIn(p["rediscover"][0].id, (7, 10, 11))           # someone else again
        shown = {p["hero"].id} | {a.id for a in p["stories"]}
        self.assertEqual(len(shown), 5)
        # the "Just added" carousel: the newest first, then others not in the stories
        heroes = p["heroes"]
        self.assertEqual(heroes[0], p["hero"])
        self.assertEqual(len(heroes), 5)
        self.assertFalse({h.id for h in heroes[1:]} & {a.id for a in p["stories"]})


class FuzzyTests(unittest.TestCase):
    def test_basic(self):
        self.assertIsNotNone(match(parse("dsm"), "Dark Side of the Moon"))
        self.assertIsNone(match(parse("xyz"), "Dark Side of the Moon"))

    def test_accents_and_smartcase(self):
        self.assertIsNotNone(match(parse("beyonce"), "Beyoncé"))
        self.assertIsNone(match(parse("Dark"), "dark side"))
        self.assertIsNotNone(match(parse("dark"), "Dark Side"))

    def test_extended(self):
        t = "Pink Floyd · The Wall"
        self.assertIsNotNone(match(parse("^pink"), t))
        self.assertIsNone(match(parse("^floyd"), t))
        self.assertIsNotNone(match(parse("wall$"), t))
        self.assertIsNone(match(parse("!floyd"), t))
        self.assertIsNotNone(match(parse("'floyd wall"), t))
        self.assertIsNotNone(match(parse("zeppelin | floyd"), t))

    def test_ranking(self):
        idx = FuzzyIndex(["Abbey Road", "A Bad Boy Room", "Absolutely Bad", "Revolver"])
        res = [e for _, e, _ in idx.search("abr")]
        self.assertEqual(res[0], "Abbey Road")
        self.assertNotIn("Revolver", res)

    def test_positions(self):
        _, pos = match(parse("dsotm"), "Dark Side of the Moon")
        self.assertEqual(pos, [0, 5, 10, 13, 17])


if __name__ == "__main__":
    unittest.main()


class ListenerTests(unittest.TestCase):
    def test_split_and_concatenated_events(self):
        import socket
        import threading
        import time
        from tonearm.kodi import NotificationListener
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = srv.getsockname()[1]
        payload = (b'{"jsonrpc":"2.0","method":"Player.OnPlay","params":{"data":{"item":{"id":1}}}}'
                   b'{"jsonrpc":"2.0","method":"AudioLibrary.OnUpdate","params":{"data":{"id":7,"type":"album"}}}'
                   b'{"jsonrpc":"2.0","id":0,"result":"pong"}'
                   b'{"jsonrpc":"2.0","method":"Application.OnVolumeChanged","params":{"data":{"volume":5}}}')

        def serve():
            c, _ = srv.accept()
            for i in range(0, len(payload), 17):  # dribble bytes to split objects
                c.sendall(payload[i:i + 17])
                time.sleep(0.002)
            time.sleep(0.3)
            c.close()

        threading.Thread(target=serve, daemon=True).start()
        got, states = [], []
        lst = NotificationListener(Instance("x", "x", "127.0.0.1", tcp_port=port),
                                   lambda m, p: got.append(m), states.append)
        lst.start()
        deadline = time.time() + 3
        while len(got) < 3 and time.time() < deadline:
            time.sleep(0.02)
        lst.stop()
        srv.close()
        self.assertEqual(got, ["Player.OnPlay", "AudioLibrary.OnUpdate", "Application.OnVolumeChanged"])
        self.assertEqual(states[0], True)


class ColorTests(unittest.TestCase):
    def test_dominant_color(self):
        from tonearm.colors import dominant_color, tune_for_glow
        w = h = 10
        # mostly black cover with a red band: red is the dominant *colour*
        px = bytearray()
        for y in range(h):
            for x in range(w):
                px += bytes((220, 30, 30, 255) if y < 3 else (5, 5, 5, 255))
        r, g, b = dominant_color(bytes(px), w, h, w * 4)
        self.assertGreater(r, 0.7)
        self.assertLess(g, 0.2)
        # all grey: falls back to the average
        grey = bytes((128, 128, 128) * (w * h))
        r, g, b = dominant_color(grey, w, h, w * 3, channels=3)
        self.assertAlmostEqual(r, 128 / 255, places=2)
        self.assertIsNone(dominant_color(b"", 0, 0, 0))
        tr, tg, tb = tune_for_glow((0.05, 0.02, 0.02), dark=True)
        self.assertGreater(max(tr, tg, tb), 0.3)   # lifted out of the dark


class FavouritePlaylistTests(unittest.TestCase):
    def test_playlists_and_groups(self):
        from tonearm.favourites import grouped, normalise, playlist_request, remove_request
        favs = normalise([
            {"title": "Chill", "type": "media", "path": "special://profile/playlists/music/chill.xsp"},
            {"title": "Mix", "type": "window", "window": "music",
             "windowparameter": "special://musicplaylists/mix.m3u"},
            {"title": "Kid A", "type": "window", "window": "music",
             "windowparameter": "musicdb://albums/1/"},
            {"title": "Song", "type": "media", "path": "/music/a.flac"},
            {"title": "Weather", "type": "window", "window": "weather"},
            {"title": "Films", "type": "media", "path": "special://videoplaylists/films.xsp"}])
        self.assertEqual([f["kind"] for f in favs],
                         ["playlist", "playlist", "album", "song", "window", "media"])
        self.assertEqual(favs[1]["playlist"], "special://musicplaylists/mix.m3u")
        self.assertEqual([(k, len(v)) for k, _h, v in grouped(favs)],
                         [("album", 1), ("song", 1), ("playlist", 2)])
        req = playlist_request("special://x/a.xsp", "A")
        self.assertEqual(req, {"title": "A", "type": "media", "path": "special://x/a.xsp"})
        self.assertEqual(remove_request(normalise([req])[0])["path"], "special://x/a.xsp")


class FavouritesTests(unittest.TestCase):
    def test_normalise_and_requests(self):
        from tonearm import favourites as fv
        favs = fv.normalise([
            {"title": "Kid A", "type": "window", "window": "music",
             "windowparameter": "musicdb://albums/42/"},
            {"title": "Björk", "type": "window", "window": "music",
             "windowparameter": "musicdb://artists/7/"},
            {"title": "Genre album", "type": "window", "window": "10502",
             "windowparameter": "musicdb://genres/3/artists/7/albums/9/"},
            {"title": "Song", "type": "media", "path": "/music/a.flac"},
            {"title": "Video", "type": "media", "path": "/videos/b.mkv"},
            {"title": "Db song", "type": "media", "path": "musicdb://songs/77.mp3"},
            {"title": "Weather", "type": "window", "window": "weather"},
            {"title": "Script", "type": "script", "path": "script.foo"},
            {"title": "?", "type": "unknown"},
        ])
        self.assertEqual([f["kind"] for f in favs],
                         ["album", "artist", "album", "song", "media", "song", "window",
                          "script", "other"])
        self.assertEqual(favs[5]["songid"], 77)
        self.assertEqual((favs[0]["albumid"], favs[1]["artistid"], favs[2]["albumid"]), (42, 7, 9))
        self.assertEqual([k for k, _h, _v in fv.grouped(favs)],
                         ["artist", "album", "song"])
        self.assertEqual(fv.song_request("/m/x.flac", "X"),
                         {"title": "X", "type": "media", "path": "/m/x.flac"})
        self.assertEqual(fv.album_request(42, "Kid A")["windowparameter"], "musicdb://albums/42/")
        rm = fv.remove_request(favs[3])
        self.assertEqual(rm, {"title": "Song", "type": "media", "path": "/music/a.flac"})


class UpcomingTests(unittest.TestCase):
    def test_pick_upcoming(self):
        import datetime as dt
        from tonearm.digest import pick_upcoming, when_label
        today = dt.date(2026, 10, 18)
        def alb(t, date, artist, have=0, total=10):
            return {"title": t, "releaseDate": f"{date}T00:00:00Z",
                    "artist": {"foreignArtistId": artist}, "statistics": {
                        "trackFileCount": have, "totalTrackCount": total}}
        items = [alb("old", "2026-09-01", "a"), alb("done", "2026-10-15", "b", 10, 10),
                 alb("just out", "2026-10-14", "c"), alb("soon", "2026-10-20", "d"),
                 alb("soon2", "2026-10-21", "d"), alb("later", "2026-11-30", "e"),
                 alb("today", "2026-10-18", "f")]
        got = pick_upcoming(items, 4, today)
        self.assertEqual([a["title"] for a in got], ["today", "soon", "later", "just out"])
        self.assertEqual([when_label(a["days"], a["day"]) for a in got],
                         ["Out today", "In 2 days", "Mon 30 Nov", "Out now"])
        # more room than artists: their second releases fill up, after the rest
        self.assertEqual([a["title"] for a in pick_upcoming(items, 8, today)],
                         ["today", "soon", "later", "just out", "soon2"])


class ListeningTests(unittest.TestCase):
    def test_jump_back_in(self):
        import datetime as dt
        from tonearm.listening import jump_back_in
        now = dt.datetime(2026, 10, 18, 9, 0)
        tracks = {1: [11, 12, 13, 14], 2: [21, 22, 23], 3: [31, 32, 33], 4: [41, 42, 43, 44]}
        songs = [
            # album 1: tracks 1-2 last night → resume at track 3
            {"songid": 11, "albumid": 1, "lastplayed": "2026-10-17 21:00:00"},
            {"songid": 12, "albumid": 1, "lastplayed": "2026-10-17 21:05:00"},
            # album 2: all three played → finished
            {"songid": 21, "albumid": 2, "lastplayed": "2026-10-16 10:00:00"},
            {"songid": 22, "albumid": 2, "lastplayed": "2026-10-16 10:04:00"},
            {"songid": 23, "albumid": 2, "lastplayed": "2026-10-16 10:08:00"},
            # album 3: one track (radio) → not counted
            {"songid": 32, "albumid": 3, "lastplayed": "2026-10-18 08:00:00"},
            # album 4: two tracks, but two months ago → too old
            {"songid": 41, "albumid": 4, "lastplayed": "2026-08-01 10:00:00"},
            {"songid": 42, "albumid": 4, "lastplayed": "2026-08-01 10:05:00"},
        ]
        got = jump_back_in(songs, tracks, now)
        self.assertEqual([(a["albumid"], a["played"], a["total"], a["resume"]) for a in got],
                         [(1, 2, 4, 2)])

    def test_radio_picks(self):
        from tonearm.listening import radio_picks
        songs = [{"genre": ["Hip Hop"], "year": 1994, "playcount": 30},
                 {"genre": ["Hip Hop"], "year": 1996, "playcount": 20},
                 {"genre": ["Jazz"], "year": 1962, "playcount": 25},
                 {"genre": ["Rock"], "year": 2005, "playcount": 5}]
        picks = radio_picks(songs, {"Hip Hop", "Jazz", "Rock"}, 4)
        self.assertEqual([p["label"] for p in picks][:2], ["90s Hip Hop", "60s Jazz"])
        self.assertEqual(len(picks), 4)
        self.assertEqual(len({p["label"] for p in picks}), 4)
        empty = radio_picks([], set(), 3, fallback_genres=["Pop", "Rock"], fallback_decades=[1980])
        self.assertEqual([p["label"] for p in empty], ["Pop", "Rock", "80s"])
        many = radio_picks(songs, {"Hip Hop", "Jazz", "Rock"}, 8,
                           fallback_genres=["Pop"], fallback_decades=[1980])
        self.assertEqual(len({p["label"] for p in many}), len(many))   # no repeats
        self.assertGreaterEqual(len(many), 7)


class ListLayoutTests(unittest.TestCase):
    def test_choose(self):
        from tonearm.listlayout import Spec, choose
        specs = [Spec("cover", 40), Spec("title"), Spec("artist", need=460),
                 Spec("year", 48), Spec("genre", need=640), Spec("more", 34)]
        self.assertEqual(choose(400, specs), ["cover", "title", "year", "more"])
        self.assertEqual(choose(700, specs), ["cover", "title", "artist", "year", "genre", "more"])


class ArtistAlbumSortTests(unittest.TestCase):
    def test_orders(self):
        from types import SimpleNamespace as A
        from tonearm.sorting import sort_artist_albums
        al = [A(title="b", year=2001, dateadded="2020-01-01"),
              A(title="a", year=2001, dateadded="2024-01-01"),
              A(title="c", year=1999, dateadded="2022-01-01"),
              A(title="d", year=0, dateadded="")]
        t = lambda xs: [x.title for x in xs]  # noqa: E731
        self.assertEqual(t(sort_artist_albums(al, "year")), ["a", "b", "c", "d"])
        self.assertEqual(t(sort_artist_albums(al, "year", True)), ["d", "c", "a", "b"])
        self.assertEqual(t(sort_artist_albums(al, "title")), ["a", "b", "c", "d"])
        self.assertEqual(t(sort_artist_albums(al, "recent")), ["a", "c", "b", "d"])
        self.assertEqual(t(sort_artist_albums(al, "bogus")), ["a", "b", "c", "d"])


class GenreTests(unittest.TestCase):
    def test_overview_top_and_order(self):
        from tonearm import genres as G
        rows = [("Rock, Pop", "c1", "A"), ("rock", "c2", "A"), ("Rock", "c3", "B"),
                ("Jazz / Fusion", "c4", "C"), ("Ambient", "", "D")]
        st = {s["name"]: s for s in G.overview(rows)}
        self.assertEqual(st["Rock"]["count"], 3)
        self.assertEqual(st["Rock"]["covers"], ["c1", "c3"])      # one per artist
        self.assertEqual(st["Ambient"]["covers"], [])
        stats = G.overview(rows, plays=[{"genre": ["Jazz"], "playcount": 9}])
        self.assertEqual([s["name"] for s in G.top(stats, 2)], ["Jazz", "Rock"])
        self.assertEqual([s["name"] for s in G.ordered(stats)],
                         ["Ambient", "Fusion", "Jazz", "Pop", "Rock"])
        self.assertEqual([s["name"] for s in G.ordered(stats, "count", hide_small=True)], ["Rock"])
        self.assertEqual([(k, len(v)) for k, v in G.by_letter(G.ordered(stats))],
                         [("A", 1), ("F", 1), ("J", 1), ("P", 1), ("R", 1)])
        self.assertEqual(G.initial("2Pac Rap"), "#")

    def test_related_decades_colours(self):
        from tonearm import genres as G
        tags = ["Soul, Funk", "Soul / Funk / R&B", "soul; Jazz", "Soul"]
        self.assertEqual(G.related(tags, "Soul"), [("Funk", 2), ("Jazz", 1), ("R&B", 1)])
        self.assertEqual(G.decades([1971, 1975, 1988, 0, None]), [(1970, 2), (1980, 1)])
        cols = G.spread_colors(["a", "a", "a", "a"])
        self.assertEqual(len(set(cols)), 4)                     # never twice on a page
        self.assertEqual(G.color_index("Rock"), G.color_index("rock"))   # stable

    def test_distinct_colors(self):
        from tonearm.colors import TILE_COLORS, distinct_colors
        self.assertEqual(distinct_colors([3, 3, 4, 13, 13]), [3, 4, 5, 13, 0])
        many = distinct_colors([0] * (TILE_COLORS + 2))
        self.assertEqual(len(set(many[:TILE_COLORS])), TILE_COLORS)
        self.assertTrue(all(a != b for a, b in zip(many, many[1:])))


class SleepTimerTests(unittest.TestCase):
    def test_modes(self):
        from tonearm.sleeptimer import FADE, SleepTimer
        t = SleepTimer()
        kw = dict(playing=True, position=10, duration=200, albumid=1, next_albumid=1,
                  last_in_queue=False)
        self.assertIsNone(t.decide(0, **kw))
        t.set("30", now=100)
        self.assertEqual(t.label(100), "Sleep in 30 min")
        self.assertIsNone(t.decide(100, **kw))
        self.assertEqual(t.decide(100 + 1800 - FADE / 2, **kw), "fade")
        self.assertEqual(t.fade_volume(80, 100 + 1800 - FADE / 2), 40)
        self.assertEqual(t.decide(100 + 1800, **kw), "stop")
        self.assertIsNone(t.decide(100 + 1800, **dict(kw, playing=False)))
        t.set("track", now=0, albumid=1)
        self.assertIsNone(t.decide(0, **kw))
        self.assertEqual(t.decide(0, **dict(kw, position=199.5)), "stop")
        t.set("album", now=0, albumid=1)
        self.assertIsNone(t.decide(0, **dict(kw, position=199.5)))                  # same album next
        self.assertEqual(t.decide(0, **dict(kw, position=199.5, next_albumid=2)), "stop")
        self.assertEqual(t.decide(0, **dict(kw, position=199.5, next_albumid=None,
                                            last_in_queue=True)), "stop")
        t.set("off", now=0)
        self.assertFalse(t.active)


class LoggerTests(unittest.TestCase):
    def test_log_file_and_report(self):
        import tempfile
        from tonearm import logger
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "state", "tonearm.log")
            for h in list(logger.log.handlers):
                logger.log.removeHandler(h)
            self.assertEqual(logger.setup_logging("INFO", path), path)
            logger.log.info("hello from the test")
            for h in logger.log.handlers:
                h.flush()
            self.assertIn("hello from the test", logger.tail(50, path))
            rep = logger.debug_report("9.9", {"GTK": "4.18"}, {"name": "Kodi", "version": "21.1",
                                                              "api": "13.5.0"},
                                      {"Lidarr": "no"}, log_file=path)
            self.assertIn("Tonearm", rep)
            self.assertIn("Kodi 21.1 (JSON-RPC 13.5.0)", rep)
            self.assertIn("hello from the test", rep)
            for h in list(logger.log.handlers):
                logger.log.removeHandler(h)
                h.close()


class PlaylistStoreTests(unittest.TestCase):
    def test_store(self):
        import tempfile, os
        from tonearm.playlists import PlaylistStore
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "x", "playlists.json")
            st = PlaylistStore(path)
            a = st.create("  Road Trip ")
            b = st.create("Ambient")
            self.assertEqual([p["name"] for p in st.all()], ["Ambient", "Road Trip"])
            self.assertEqual(st.add(a["id"], [{"id": 1, "title": "one"}, {"id": 2}]), 2)
            self.assertEqual(st.add(a["id"], [{"id": 2}, {"id": 3}]), 1)   # no doubles
            st.remove(a["id"], 0)
            st.rename(b["id"], "Chill")
            again = PlaylistStore(path)                                    # persisted
            self.assertEqual([s["id"] for s in again.get(a["id"])["songs"]], [2, 3])
            self.assertEqual(again.get(b["id"])["name"], "Chill")
            again.delete(b["id"])
            self.assertIsNone(PlaylistStore(path).get(b["id"]))
