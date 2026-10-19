"""Volume-key bridge against a fake pactl (no audio server needed)."""
import json
import os
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from tonearm import volumebridge  # noqa: E402
from tonearm.volumebridge import SINK, VolumeBridge  # noqa: E402


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = os.path.join(self.tmp.name, "pactl.json")
        with open(self.state, "w") as f:
            json.dump({"default": "alsa_output.speaker", "next": 1, "modules": [],
                       "sinks": [{"index": 1, "name": "alsa_output.speaker", "volume": 70,
                                  "mute": False}]}, f)
        os.environ["FAKE_PACTL_STATE"] = self.state
        self._path = os.environ["PATH"]
        os.environ["PATH"] = os.path.join(ROOT, "tests", "fake_bin") + os.pathsep + self._path
        self.changes = []
        self.bridge = VolumeBridge(lambda v, m: self.changes.append((v, m)))

    def tearDown(self):
        self.bridge.stop()
        os.environ["PATH"] = self._path
        self.tmp.cleanup()

    def st(self):
        with open(self.state) as f:
            return json.load(f)

    def user_sets(self, volume=None, mute=None):
        """What the shell does when the volume keys are pressed."""
        args = (["set-sink-volume", SINK, f"{volume}%"] if volume is not None
                else ["set-sink-mute", SINK, "1" if mute else "0"])
        volumebridge._pactl(*args)

    def wait(self, cond, t=3.0):
        end = time.time() + t
        while time.time() < end:
            if cond():
                return True
            time.sleep(0.02)
        return False

    def test_available(self):
        self.assertTrue(volumebridge.available())

    def test_start_routes_and_stop_restores(self):
        self.bridge.start(40, False)
        st = self.st()
        self.assertEqual(st["default"], SINK)
        ours = [s for s in st["sinks"] if s["name"] == SINK][0]
        self.assertEqual((ours["volume"], ours["mute"]), (40, False))
        loop = [m for m in st["modules"] if m["name"] == "module-loopback"][0]
        self.assertIn("sink=alsa_output.speaker", loop["args"])  # phone sounds still audible
        self.bridge.stop()
        st = self.st()
        self.assertEqual(st["default"], "alsa_output.speaker")
        self.assertEqual(st["modules"], [])
        self.assertEqual([s["name"] for s in st["sinks"]], ["alsa_output.speaker"])

    def test_volume_keys_reach_kodi_without_echo(self):
        self.bridge.start(40, False)
        time.sleep(0.2)               # subscriber running
        self.user_sets(volume=45)     # volume-up key
        self.assertTrue(self.wait(lambda: (45, False) in self.changes))
        self.user_sets(mute=True)
        self.assertTrue(self.wait(lambda: (45, True) in self.changes))
        n = len(self.changes)
        self.bridge.push(45, True)    # Kodi confirms the same values: nothing to do
        self.bridge.push(30, False)   # TV remote changed Kodi: shown on the sink…
        time.sleep(0.3)
        self.assertEqual(len(self.changes), n)  # …but not bounced back to Kodi
        ours = [s for s in self.st()["sinks"] if s["name"] == SINK][0]
        self.assertEqual((ours["volume"], ours["mute"]), (30, False))

    def test_strict_server_falls_back_to_simple_arguments(self):
        os.environ["FAKE_PACTL_STRICT"] = "1"
        try:
            self.bridge.start(40, False)
            st = self.st()
            self.assertEqual(st["default"], SINK)
            loop = [m for m in st["modules"] if m["name"] == "module-loopback"][0]
            self.assertNotIn("dont_move", loop["args"])
            self.assertIn("sink=alsa_output.speaker", loop["args"])
        finally:
            del os.environ["FAKE_PACTL_STRICT"]

    def test_failure_cleans_up_and_explains(self):
        with open(self.state) as f:
            st = json.load(f)
        st["sinks"] = []          # no real output at all
        st["default"] = ""
        with open(self.state, "w") as f:
            json.dump(st, f)
        with self.assertRaises(RuntimeError) as cm:
            self.bridge.start(40, False)
        self.assertIn("no audio output", str(cm.exception))
        self.assertEqual(self.st()["modules"], [])

    def test_leftovers_from_a_crash_are_removed(self):
        self.bridge.start(50, False)
        crashed = VolumeBridge(lambda v, m: None)
        crashed.cleanup_leftovers()   # what a fresh start does
        self.assertEqual(self.st()["modules"], [])
        self.bridge.active = False
        self.bridge._modules = []


if __name__ == "__main__":
    unittest.main()
