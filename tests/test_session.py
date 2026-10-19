"""Session tests against the mock Kodi, using a fake `gi` when GTK isn't available."""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import gi  # noqa: F401
    gi.require_version("Gtk", "4.0")
    REAL_GI = not getattr(gi, "FAKE", False)
except Exception:  # noqa: BLE001
    REAL_GI = False
    for m in [m for m in sys.modules if m == "gi" or m.startswith("gi.")]:
        del sys.modules[m]
    from tests import fake_gi
    fake_gi.install()


@unittest.skipIf(REAL_GI, "uses the fake gi shim; run in a headless env")
class SessionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["XDG_CONFIG_HOME"] = os.path.join(self.tmp.name, "cfg")
        os.environ["XDG_CACHE_HOME"] = os.path.join(self.tmp.name, "cache")
        from tonearm.config import Config
        from tonearm.session import Session
        from tests.mock_kodi import FakeLibrary, serve
        self.lib = FakeLibrary(n_albums=12, songs_per_album=6)
        self.srv = serve(self.lib)
        self.config = Config()
        self.config.prefs["notifications"] = False  # no TCP listener in tests
        inst = self.config.new_instance(name="Test", host="127.0.0.1",
                                        port=self.srv.server_address[1], username="", password="")
        self.session = Session(self.config)
        self.events = []
        for sig in ("library-changed", "player-changed", "track-changed", "sync-finished",
                    "seeked", "toast"):
            self.session.connect(sig, lambda s, *a, sig=sig: self.events.append((sig, a)))
        self.session.activate(inst)

    def tearDown(self):
        self.session.activate(None)   # stop its polls: they'd keep the shared workers busy
        self.srv.shutdown()
        self.tmp.cleanup()

    def pump(self, until, t=5):
        from tests.fake_gi import pump
        return pump(t, until)

    def test_connect_sync_search_play(self):
        s = self.session
        self.assertTrue(self.pump(lambda: any(e[0] == "sync-finished" for e in self.events)))
        self.assertTrue(s.props.connected)
        self.assertEqual(s.cache.count("album"), 12)
        self.assertEqual(s.kodi_info["api"], "13.5.0")

        results = []
        s.search("track 3", "song", results.append)
        self.assertTrue(self.pump(lambda: results))
        kinds = {e[0] for _, e, _ in results[0]}
        self.assertEqual(kinds, {"song"})

        s.play_album(4, position=2)
        self.assertTrue(self.pump(lambda: s.state.active and s.state.playing))
        self.assertEqual(s.state.albumid, 4)
        self.assertEqual(s.state.queue_position, 2)
        self.assertIn("Track 3", s.state.title)
        self.assertTrue(any(e[0] == "track-changed" for e in self.events))

        s.next()
        self.assertTrue(self.pump(lambda: "Track 4" in s.state.title))
        s.seek(100)
        self.assertTrue(self.pump(lambda: self.lib.player["time"] == 100))
        self.assertTrue(any(e[0] == "seeked" for e in self.events))
        s.play_pause()
        self.assertFalse(s.state.playing)  # optimistic update
        self.assertTrue(self.pump(lambda: not self.lib.player["playing"]))
        s.set_volume(42)
        self.assertTrue(self.pump(lambda: self.lib.volume == 42))
        s.cycle_repeat()
        self.assertTrue(self.pump(lambda: self.lib.player["repeat"] == "all"))

        q = []
        s.get_queue(q.append)
        self.assertTrue(self.pump(lambda: q))
        self.assertEqual(len(q[0]), 6)
        # drag to reorder: item 0 to position 3 (neighbour swaps on Kodi)
        before = list(self.lib.queue)
        s.move_in_queue(0, 3)
        want = before[1:4] + [before[0]] + before[4:]
        self.assertTrue(self.pump(lambda: self.lib.queue == want))
        s.move_in_queue(3, 0)
        self.assertTrue(self.pump(lambda: self.lib.queue == before))

    def test_kodi_audio_power_mac(self):
        s = self.session
        self.assertTrue(self.pump(lambda: s.props.connected))
        self.assertTrue(self.pump(lambda: s.power_options == {"quit", "shutdown", "reboot"}))
        self.assertTrue(self.pump(lambda: s.instance.mac == "AA:BB:CC:DD:EE:FF"))
        self.assertTrue(s.can_wake())
        got = []
        s.fetch_audio_settings(lambda st, err: got.append((st, err)))
        self.assertTrue(self.pump(lambda: got))
        st, err = got[0]
        self.assertEqual(err, "")
        self.assertEqual(set(st), {"audiooutput.audiodevice", "musicplayer.crossfade",
                                   "musicplayer.replaygaintype", "audiooutput.passthrough"})
        self.assertEqual(st["audiooutput.passthrough"]["type"], "boolean")
        s.set_kodi_setting("audiooutput.audiodevice", "ALSA:hdmi")
        self.assertTrue(self.pump(
            lambda: self.lib.settings["audiooutput.audiodevice"]["value"] == "ALSA:hdmi"))
        fmt = []
        s.fetch_stream_info(fmt.append)
        self.assertTrue(self.pump(lambda: fmt))
        self.assertEqual(fmt[0], "FLAC · 24-bit / 96 kHz")

    def test_ratings(self):
        s = self.session
        self.lib.songs[3]["userrating"] = 8          # rated on Kodi before
        self.lib.albums[2]["userrating"] = 5
        self.assertTrue(self.pump(lambda: any(e[0] == "sync-finished" for e in self.events)))
        self.assertTrue(self.pump(lambda: s.ratings == {"song": {3: 8}, "album": {2: 5}}))
        self.assertEqual(s.cache.ratings("song"), {3: 8})
        s.rate("album", 4, 10)                       # here: at once, and on Kodi
        self.assertEqual(s.rating("album", 4), 10)
        self.assertTrue(self.pump(lambda: self.lib.albums[4]["userrating"] == 10))
        top = s.cache.top_rated_albums()
        self.assertEqual([(a.id, a.rating) for a in top], [(4, 10), (2, 5)])
        self.assertEqual([a.id for a in s.cache.albums("rating")][:2], [4, 2])
        s.rate("song", 3, 0)                         # cleared
        self.assertTrue(self.pump(lambda: self.lib.songs[3]["userrating"] == 0))
        self.assertEqual(s.cache.top_rated_songs(), [])
        # rated on Kodi meanwhile: its update event brings it in
        self.lib.songs[5]["userrating"] = 6
        s._on_event(s._generation, "AudioLibrary.OnUpdate", {"data": {"item": {"type": "song", "id": 5}}})
        self.assertTrue(self.pump(lambda: s.rating("song", 5) == 6, t=6))

    def test_partymode(self):
        s = self.session
        self.assertTrue(self.pump(lambda: any(e[0] == "sync-finished" for e in self.events)))
        s.start_partymode()
        self.assertTrue(self.pump(lambda: s.state.partymode))
        self.assertEqual(self.lib.partymode_source, "music")
        self.assertEqual(s.radio_label, "Party Mode")
        s.start_partymode("special://profile/playlists/music/jazz.xsp", "Jazz")
        self.assertTrue(self.pump(lambda: s.radio_label == "Party Mode · Jazz"))
        self.assertEqual(self.lib.partymode_source, "special://profile/playlists/music/jazz.xsp")
        s.stop_partymode()
        self.assertTrue(self.pump(lambda: not s.state.partymode))
        self.assertTrue(s.state.active)              # what plays keeps playing
        self.assertIsNone(s.radio_label)

    def test_folders(self):
        from tonearm.folders import normalise, sort_entries
        s = self.session
        self.assertTrue(self.pump(lambda: s.props.connected))
        got = []
        s.fetch_sources(lambda src, err: got.append((src, err)))
        self.assertTrue(self.pump(lambda: got))
        self.assertEqual([x["label"] for x in got[0][0]], ["Music", "Downloads"])
        self.assertEqual(got[0][1], "")
        files = []
        s.fetch_directory("/music/", lambda f, err: files.append((f, err)))
        self.assertTrue(self.pump(lambda: files))
        entries = sort_entries(normalise(files[0][0]))
        self.assertEqual([(e["name"], e["kind"]) for e in entries],
                         [("Zebra", "folder"), ("a.mp3", "song"), ("b.flac", "song"),
                          ("mix.m3u", "playlist")])
        self.assertEqual(entries[2]["songid"], 2)
        self.assertEqual(entries[1]["songid"], 0)      # not in the library
        # a big folder: its paths go in requests small enough for Kodi's web server
        from tonearm.session import Session
        many = [f"smb://nas/Music/Some Long Artist Name/Album {i}/{i:03d} title.flac"
                for i in range(600)]
        batches = Session.file_batches(many)
        self.assertGreater(len(batches), 1)
        self.assertEqual(sum(batches, []), many)
        import json as _json
        self.assertTrue(all(len(_json.dumps([{"file": f} for f in b])) < 20000 for b in batches))
        s.play_files(many[:120], 5)
        self.assertTrue(self.pump(lambda: s.state.active and self.lib.player["position"] == 5))
        s.play_folder("/music/Zebra/", shuffle=True)
        self.assertTrue(self.pump(lambda: getattr(self.lib, "added_dirs", None) == ["/music/Zebra/"]))
        self.assertTrue(self.pump(lambda: s.state.active))

    def test_kodi_favourites(self):
        s = self.session
        self.assertTrue(self.pump(lambda: any(e[0] == "sync-finished" for e in self.events)))
        self.assertTrue(self.pump(lambda: len(s.favourites) == 2))
        # the mock's media favourite is an audio file: a song favourite
        self.assertEqual([f["kind"] for f in s.favourites], ["song", "window"])
        self.assertFalse(s.favourites_stale)              # online: no "offline" banner
        s.toggle_album_favourite(3)                       # add …
        self.assertTrue(self.pump(lambda: 3 in s.fav_album_ids()))
        fav = next(f for f in self.lib.favourites if f.get("windowparameter") == "musicdb://albums/3/")
        self.assertEqual(fav["window"], "music")
        s.toggle_album_favourite(3)                       # … and Kodi's toggle removes it
        self.assertTrue(self.pump(lambda: 3 not in s.fav_album_ids()))
        self.assertEqual(len(self.lib.favourites), 2)
        s.toggle_artist_favourite(1)
        self.assertTrue(self.pump(lambda: 1 in s.fav_artist_ids()))
        win = next(f for f in s.favourites if f["kind"] == "window")
        s.open_favourite(win)
        self.assertTrue(self.pump(lambda: self.lib.windows == [("settings", None)]))
        media = next(f for f in s.favourites if f["kind"] == "song")
        s.open_favourite(media)
        self.assertTrue(self.pump(lambda: "Player.Open" in self.lib.calls))
        # songs: a "media" favourite of the song's file, matched back by path
        song = self.lib.songs[1]
        s.toggle_song_favourite(1)
        self.assertTrue(self.pump(lambda: any(f.get("path") == song["file"]
                                              for f in self.lib.favourites)))
        self.assertTrue(self.pump(lambda: 1 in s.fav_song_ids()))
        s.toggle_song_favourite(1)
        self.assertTrue(self.pump(lambda: 1 not in s.fav_song_ids()))

    def test_listening(self):
        s = self.session
        self.assertTrue(self.pump(lambda: any(e[0] == "sync-finished" for e in self.events)))
        for sid, when in ((1, "2026-10-17 21:00:00"), (2, "2026-10-17 21:04:00")):
            self.lib.songs[sid].update(playcount=3, lastplayed=when)
        got = []
        s.fetch_listening(lambda d, cached: got.append((d, cached)))
        self.assertTrue(self.pump(lambda: got))
        data, cached = got[0]
        self.assertFalse(cached)
        self.assertEqual([x["songid"] for x in data["recent"]], [2, 1])
        self.assertEqual(len(data["top"]), 2)
        order = s.cache.album_track_order([self.lib.songs[1]["albumid"]])
        self.assertEqual(order[self.lib.songs[1]["albumid"]][:2], [1, 2])

    def test_my_playlists(self):
        s = self.session
        self.assertTrue(self.pump(lambda: any(e[0] == "sync-finished" for e in self.events)))
        pl = s.create_playlist("Mix")
        s.add_to_playlist(pl["id"], "album", 2)
        self.assertTrue(self.pump(lambda: len(s.playlists.get(pl["id"])["songs"]) > 0))
        n = len(s.playlists.get(pl["id"])["songs"])
        s.add_to_playlist(pl["id"], "song", 1)
        self.assertTrue(self.pump(lambda: len(s.playlists.get(pl["id"])["songs"]) == n + 1))
        s.play_my_playlist(pl["id"], position=1)
        self.assertTrue(self.pump(lambda: s.state.active))
        self.assertEqual(len(self.lib.queue), n + 1)
        s.remove_from_playlist(pl["id"], 0)
        self.assertEqual(len(s.playlists.get(pl["id"])["songs"]), n)
        s.delete_playlist(pl["id"])
        self.assertEqual(s.my_playlists(), [])

    def test_queue_end_releases_player(self):
        s = self.session
        self.assertTrue(self.pump(lambda: any(e[0] == "sync-finished" for e in self.events)))
        s.play_album(4, position=5)  # last of 6 tracks
        self.assertTrue(self.pump(lambda: s.state.active and s.state.playing))
        # Kodi parks on the last track, paused at the very end
        self.lib.player.update(playing=False, time=200)
        s.refresh_player()
        self.assertTrue(self.pump(lambda: not s.state.active))
        self.assertTrue(self.pump(lambda: not self.lib.player["active"]))  # Player.Stop sent

    def test_paused_mid_queue_is_kept(self):
        s = self.session
        self.assertTrue(self.pump(lambda: any(e[0] == "sync-finished" for e in self.events)))
        s.play_album(4, position=1)
        self.assertTrue(self.pump(lambda: s.state.active))
        self.lib.player.update(playing=False, time=200)  # end of track 2, but more to come
        s.refresh_player()
        self.pump(lambda: False, 0.5)
        self.assertTrue(s.state.active)

    def test_played_genres_decades_playlists(self):
        s = self.session
        self.assertTrue(self.pump(lambda: any(e[0] == "sync-finished" for e in self.events)))
        self.lib.albums[3].update(playcount=9, lastplayed="2026-10-01 10:00:00")
        self.lib.albums[5].update(playcount=2, lastplayed="2026-10-15 10:00:00")
        out = {}
        s.fetch_played("recent", lambda a, cached: out.setdefault("recent", (a, cached)))
        s.fetch_played("most", lambda a, cached: out.setdefault("most", (a, cached)))
        self.assertTrue(self.pump(lambda: len(out) == 2))
        self.assertEqual([a["albumid"] for a in out["recent"][0]], [5, 3])
        self.assertEqual([a["albumid"] for a in out["most"][0]], [3, 5])
        self.assertFalse(out["recent"][1])
        # offline: same answer from the cache
        s.props.connected = False
        again = []
        s.fetch_played("most", lambda a, cached: again.append((a, cached)))
        self.assertEqual(([a["albumid"] for a in again[0][0]], again[0][1]), ([3, 5], True))
        s.props.connected = True

        self.assertEqual([g for g, n, c in s.cache.genres()], ["Rock"])
        self.assertEqual(sum(n for d, n, c in s.cache.decades()), 12)

        pls = []
        s.fetch_playlists(lambda p, cached: pls.append(p))
        self.assertTrue(self.pump(lambda: pls))
        self.assertEqual([p["label"] for p in pls[0]], ["Party", "Road trip"])
        self.assertTrue(pls[0][0]["smart"])
        items = []
        s.fetch_playlist_items(pls[0][1]["file"], items.append)
        self.assertTrue(self.pump(lambda: items))
        self.assertEqual(len(items[0]), 5)

        s.play_playlist(pls[0][0]["file"], position=2)
        self.assertTrue(self.pump(lambda: self.lib.player["active"] and len(self.lib.queue) == 5))
        self.assertEqual(self.lib.player["position"], 2)
        s.play_genre("rock")
        self.assertTrue(self.pump(lambda: len(self.lib.queue) == 10))
        s.play_albums([1, 2], shuffle=True)
        self.assertTrue(self.pump(lambda: len(self.lib.queue) == 12 and self.lib.player["shuffled"]))

    def test_switching_instance_mid_sync_does_not_block(self):
        s = self.session
        # an instance whose sync never finishes quickly
        self.lib.slow_over, self.lib.slow_delay = 1, 2.0
        self.assertTrue(self.pump(lambda: s.props.syncing))
        from tests.mock_kodi import FakeLibrary, serve
        lib2 = FakeLibrary(n_albums=5, songs_per_album=2, seed=7)
        srv2 = serve(lib2)
        try:
            inst2 = self.config.new_instance(name="Second", host="127.0.0.1",
                                             port=srv2.server_address[1], username="", password="")
            self.events.clear()
            s.activate(inst2)
            self.assertTrue(self.pump(lambda: any(e[0] == "sync-finished" for e in self.events), 8))
            self.assertEqual(s.cache.count("album"), 5)
        finally:
            srv2.shutdown()

    def test_volume_keys_bridge(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        state = os.path.join(self.tmp.name, "pactl.json")
        import json
        with open(state, "w") as f:
            json.dump({"default": "speaker", "next": 1, "modules": [],
                       "sinks": [{"index": 1, "name": "speaker", "volume": 70, "mute": False}]}, f)
        os.environ["FAKE_PACTL_STATE"] = state
        old_path = os.environ["PATH"]
        os.environ["PATH"] = os.path.join(root, "tests", "fake_bin") + os.pathsep + old_path
        try:
            def pactl_state():
                with open(state) as f:
                    return json.load(f)

            s = self.session
            self.assertTrue(self.pump(lambda: any(e[0] == "sync-finished" for e in self.events)))
            self.config.prefs["volume_keys"] = True
            s.play_album(2)
            self.assertTrue(self.pump(lambda: pactl_state()["default"] == "tonearm_kodi"))
            sink = [x for x in pactl_state()["sinks"] if x["name"] == "tonearm_kodi"][0]
            self.assertEqual(sink["volume"], 80)            # starts at Kodi's volume
            import time as _t
            _t.sleep(0.2)
            from tonearm.volumebridge import _pactl
            _pactl("set-sink-volume", "tonearm_kodi", "55%")  # volume key pressed
            self.assertTrue(self.pump(lambda: self.lib.volume == 55, 4))
            s.stop()
            self.assertTrue(self.pump(lambda: pactl_state()["default"] == "speaker"
                                      and pactl_state()["modules"] == [], 4))
        finally:
            s.shutdown()
            os.environ["PATH"] = old_path

    def test_radio_by_decade_and_genre(self):
        s = self.session
        self.assertTrue(self.pump(lambda: any(e[0] == "sync-finished" for e in self.events)))
        # mock: album years are 2000 + id % 25 → the 2000s hold albums 1–9
        ids = s.cache.radio_album_ids(2000, "rock")
        self.assertEqual(sorted(ids), list(range(1, 10)))
        self.assertEqual(s.cache.radio_count(ids), 9 * 6)
        self.assertEqual(s.cache.radio_album_ids(2000, "Jazz"), [])
        self.assertIsNone(s.cache.radio_album_ids(None, None))

        s.start_radio(2000, "Rock")
        self.assertTrue(self.pump(lambda: s.radio_label and len(self.lib.queue) == 40, 4))
        self.assertEqual(s.radio_label, "2000s · Rock")
        allowed = {sid for sid, v in self.lib.songs.items() if v["albumid"] in ids}
        self.assertTrue(set(self.lib.queue) <= allowed)
        # near the end of the queue the radio tops itself up with new songs
        self.lib.player["position"] = 38
        s.refresh_player()
        self.assertTrue(self.pump(lambda: len(self.lib.queue) == 54, 4))  # only 14 unplayed left
        self.assertEqual(len(set(self.lib.queue)), 54)
        # playing an album ends the radio
        s.play_album(3)
        self.assertTrue(self.pump(lambda: s.radio_label is None))

    def test_offline_cache_then_resync(self):
        s = self.session
        self.assertTrue(self.pump(lambda: any(e[0] == "sync-finished" for e in self.events)))
        # second session on the same cache: library is available before any network
        from tonearm.session import Session
        s2 = Session(self.config)
        self.srv.shutdown()
        s2.activate(self.config.active)
        self.assertEqual(s2.cache.count("album"), 12)
        self.assertTrue(self.pump(lambda: not s2.props.connected, 3))

    def test_lidarr_search_skips_kodi_artists(self):
        from tests.mock_lidarr import FakeLidarr, serve as serve_lidarr
        s = self.session
        self.assertTrue(self.pump(lambda: any(e[0] == "sync-finished" for e in self.events)))
        lsrv = serve_lidarr(FakeLidarr())
        try:
            st = self.config.lidarr
            st.enabled, st.url, st.api_key = True, f"127.0.0.1:{lsrv.server_address[1]}", "testkey"
            s.configure_lidarr()
            self.assertIsNotNone(s.lidarr)
            # "Velvet Moons" is in Kodi; Lidarr has "The Velvet Moons" -> must be filtered
            s.cache.upsert_artists([{"artistid": 999, "artist": "Velvet Moons"}])
            s._invalidate_index()
            out = []
            s.lidarr_search("velvet", out.append)
            self.assertTrue(self.pump(lambda: out))
            self.assertEqual(out[0], [])
            out.clear()
            s.lidarr_search("boards", out.append)
            self.assertTrue(self.pump(lambda: out))
            self.assertEqual([a["artistName"] for a in out[0]], ["Boards of Canada", "Boards"])
            # stale queries never report back
            got = []
            s.lidarr_search("boards", lambda r: got.append("old"))
            s.lidarr_search("boards of", lambda r: got.append("new"))
            self.pump(lambda: "new" in got and len(got) >= 1, 3)
            self.assertNotIn("old", got)
            self.assertEqual(s.kodi_artist_id("the velvet moons"), 999)
        finally:
            lsrv.shutdown()


if __name__ == "__main__":
    unittest.main()
