"""Artwork cache scheduling tests (fake gi; no GTK needed)."""
import os
import shutil
import sys
import tempfile
import threading
import time
import types
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

    class _Pix:
        def __init__(self, path):
            self.path = path

        @staticmethod
        def new_from_file_at_scale(path, w, h, keep):
            return _Pix(path)

        def get_has_alpha(self):
            return False

        def savev(self, out, fmt, keys, vals):
            shutil.copyfile(self.path, out)

    sys.modules["gi.repository"].GdkPixbuf.Pixbuf = _Pix


class FakeClient:
    def __init__(self):
        self.gate = threading.Event()
        self.fetched = []

    def image_url(self, key):
        return key

    def fetch_bytes(self, url):
        self.fetched.append(url)
        self.gate.wait(5)  # downloads are "slow" until the test opens the gate
        return b"img"


@unittest.skipIf(REAL_GI, "uses the fake gi shim")
class ArtworkTests(unittest.TestCase):
    def setUp(self):
        from tonearm.artwork import ArtworkCache
        self.tmp = tempfile.mkdtemp()
        self.client = FakeClient()
        self.art = ArtworkCache(self.tmp, self.client)

    def tearDown(self):
        self.client.gate.set()
        self.art.shutdown()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def pump(self, until, t=3):
        from tests.fake_gi import pump
        return pump(t, until)

    def test_cached_images_dont_wait_for_downloads(self):
        # one image already on disk
        path = self.art.file_for("image://cached/", "small")
        with open(path, "wb") as f:
            f.write(b"x")
        got = {}
        for i in range(12):  # flood the network queue with slow downloads
            self.art.load(f"image://slow{i}/", "small", lambda t, i=i: got.setdefault(i, t))
        self.art.load("image://cached/", "small", lambda t: got.setdefault("cached", t))
        self.assertTrue(self.pump(lambda: "cached" in got, 1.5))
        self.assertEqual(got["cached"][0], "tex")
        self.assertEqual(len([k for k in got if k != "cached"]), 0)  # downloads still blocked

    def test_cancelled_requests_are_skipped(self):
        busy = [self.art.load(f"image://busy{i}/", "small", lambda t: None) for i in range(6)]
        time.sleep(0.2)  # all six network workers are now stuck in a download
        ticket = self.art.load("image://scrolled-away/", "small", lambda t: None)
        self.art.cancel(ticket)
        self.client.gate.set()
        self.pump(lambda: False, 0.8)
        self.assertNotIn("image://scrolled-away/", self.client.fetched)
        self.assertEqual(len(busy), 6)

    def test_newest_request_first(self):
        from tonearm.workers import DaemonPool
        self.art._net = DaemonPool(1, "test-net", lifo=True)  # one worker: strict order
        self.art.load("image://busy/", "small", lambda t: None)
        time.sleep(0.2)
        for name in ("old", "new"):
            self.art.load(f"image://{name}/", "small", lambda t: None)
        self.client.gate.set()
        self.pump(lambda: "image://old/" in self.client.fetched, 2)
        f = self.client.fetched
        self.assertLess(f.index("image://new/"), f.index("image://old/"))

    def test_transient_failure_is_retried_later(self):
        self.client.gate.set()
        self.client.fetch_bytes = lambda url: (_ for _ in ()).throw(OSError("busy"))
        res = []
        self.art.load("image://flaky/", "small", res.append)
        self.assertTrue(self.pump(lambda: res))
        self.assertIsNone(res[0])
        key = ("image://flaky/", "small")
        self.art._failed[key] -= 120  # pretend a couple of minutes passed
        self.client.fetch_bytes = lambda url: b"ok"
        res.clear()
        self.art.load("image://flaky/", "small", res.append)
        self.assertTrue(self.pump(lambda: res))
        self.assertIsNotNone(res[0])
        self.assertTrue(self.art.cached_file("image://flaky/", "small").endswith(".jpg"))


if __name__ == "__main__":
    unittest.main()
