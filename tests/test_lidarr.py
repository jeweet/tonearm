import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tonearm.lidarr import (LidarrClient, LidarrError, LidarrSettings, album_progress,  # noqa: E402
                             best_image, norm_name)
from tests.mock_lidarr import FakeLidarr, serve  # noqa: E402


class LidarrTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeLidarr()
        self.srv = serve(self.fake)
        self.url = f"127.0.0.1:{self.srv.server_address[1]}"
        self.c = LidarrClient(self.url, "testkey")
        self.settings = LidarrSettings(enabled=True, url=self.url, api_key="testkey",
                                       root_folder="/music/", quality_profile=1, metadata_profile=1)

    def tearDown(self):
        self.srv.shutdown()

    def test_status_and_bad_key(self):
        self.assertEqual(self.c.status()["appName"], "Lidarr")
        with self.assertRaises(LidarrError):
            LidarrClient(self.url, "nope").status()

    def test_lookup_add_flow(self):
        res = self.c.lookup("boards")
        self.assertEqual([r["artistName"] for r in res], ["Boards of Canada", "Boards"])
        self.assertEqual(res[0]["id"], 0)
        self.assertEqual(best_image(res[0]), "lidarr:/MediaCover/1/poster.jpg|https://example.invalid/boc.jpg")
        added = self.c.add_artist(res[0], self.settings, monitor="future", search=False)
        self.assertTrue(added["id"])
        body = self.fake.artists[0]
        self.assertEqual(body["addOptions"], {"monitor": "future", "searchForMissingAlbums": False})
        self.assertEqual(body["rootFolderPath"], "/music/")
        # lookup now marks it as present
        again = self.c.lookup("boards of")
        self.assertEqual(again[0]["id"], added["id"])
        with self.assertRaises(LidarrError) as cm:
            self.c.add_artist(res[0], self.settings)
        self.assertIn("already been added", str(cm.exception))
        self.c.search_artist(added["id"])
        self.assertEqual(self.fake.commands[-1], {"name": "ArtistSearch", "artistId": added["id"]})
        self.assertEqual(album_progress(self.c.albums(added["id"])[0]), (3, 18))
        self.c.set_monitored(self.c.artist(added["id"]), False)
        self.assertFalse(self.fake.artists[0]["monitored"])

    def test_add_requires_defaults(self):
        s = LidarrSettings(enabled=True, url=self.url, api_key="testkey")
        with self.assertRaises(LidarrError):
            self.c.add_artist(self.c.lookup("boards")[0], s)

    def test_lists(self):
        self.assertEqual(self.c.queue()[0]["status"], "downloading")
        self.assertEqual(len(self.c.missing()), 1)
        self.assertEqual(len(self.c.upcoming()), 1)
        self.assertEqual(self.c.root_folders()[0]["path"], "/music/")

    def test_reverse_proxy_prefix(self):
        c = LidarrClient(f"http://{self.url}/", "testkey")
        self.assertEqual(c.prefix, "")
        c2 = LidarrClient(f"https://example.invalid/lidarr", "k")
        self.assertEqual((c2.scheme, c2.port, c2.prefix), ("https", 443, "/lidarr"))

    def test_disk_cache(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            c = LidarrClient(self.url, "testkey", cache_dir=d)
            self.assertIsNone(c.cached("queue"))
            c.queue()
            c.add_artist(c.lookup("boards")[0], self.settings)
            c.artists(max_age=0)
            c2 = LidarrClient(self.url, "testkey", cache_dir=d)
            self.assertEqual(c2.cached("queue")[0]["status"], "downloading")
            self.assertEqual(len(c2.cached("artists")), 1)
            self.srv.shutdown()                       # Lidarr gone: artists still known
            self.assertEqual(len(c2.artists()), 1)
            self.srv = serve(self.fake)               # keep tearDown happy
            other = LidarrClient("otherhost:1", "k", cache_dir=d)
            self.assertIsNone(other.cached("queue"))  # never mix servers

    def test_local_cover_path(self):
        img = {"images": [{"coverType": "poster", "url": "/config/MediaCover/600/poster.jpg"}]}
        self.assertEqual(best_image(img), "lidarr:/MediaCover/600/poster.jpg")

    def test_norm_name(self):
        self.assertEqual(norm_name("The Beatles"), norm_name("beatles"))
        self.assertEqual(norm_name("Simon & Garfunkel"), norm_name("Simon and Garfunkel"))
        self.assertEqual(norm_name("Björk"), norm_name("Bjork"))
        self.assertNotEqual(norm_name("Boards"), norm_name("Boards of Canada"))


if __name__ == "__main__":
    unittest.main()
