import os
import tempfile
import unittest

from tonearm.playlists import PlaylistStore


class PlaylistEditTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = PlaylistStore(os.path.join(self.tmp.name, "p.json"))
        self.pl = self.store.create("Mix")
        self.store.add(self.pl["id"], [{"id": i, "title": f"t{i}"} for i in (1, 2, 3, 4)])

    def tearDown(self):
        self.tmp.cleanup()

    def ids(self, pid=None):
        return [s["id"] for s in self.store.get(pid or self.pl["id"])["songs"]]

    def test_move(self):
        pid = self.pl["id"]
        self.store.move(pid, 3, 0)
        self.assertEqual(self.ids(), [4, 1, 2, 3])
        self.store.move(pid, 0, 99)            # past the end: last
        self.assertEqual(self.ids(), [1, 2, 3, 4])
        self.store.move(pid, 1, 2)
        self.assertEqual(self.ids(), [1, 3, 2, 4])

    def test_reorder_duplicate_names(self):
        pid = self.pl["id"]
        self.store.reorder(pid, [3, 1, 99])
        self.assertEqual(self.ids(), [3, 1, 2, 4])
        copy = self.store.duplicate(pid, self.store.unique_name("Mix"))
        self.assertEqual(copy["name"], "Mix 2")
        self.assertEqual(self.ids(copy["id"]), [3, 1, 2, 4])
        self.store.move(copy["id"], 0, 3)
        self.assertEqual(self.ids(), [3, 1, 2, 4])     # the original is untouched
        new = self.store.create_with("Queue", [{"id": 7}, {"id": 7}, {"id": 8}])
        self.assertEqual(self.ids(new["id"]), [7, 8])
        again = PlaylistStore(self.store.path)           # saved to disk
        self.assertEqual([s["id"] for s in again.get(pid)["songs"]], [3, 1, 2, 4])


if __name__ == "__main__":
    unittest.main()
