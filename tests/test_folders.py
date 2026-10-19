import unittest

from tonearm.folders import human_size, kind_of, name_of, normalise, sort_entries


class FoldersTest(unittest.TestCase):
    def test_names_kinds(self):
        self.assertEqual(name_of("smb://nas/My%20Music/"), "My Music")
        self.assertEqual(name_of("/mnt/x/song.flac"), "song.flac")
        self.assertEqual(kind_of({"filetype": "directory", "file": "/a/"}), "folder")
        self.assertEqual(kind_of({"filetype": "file", "file": "/a/b.M3U"}), "playlist")
        self.assertEqual(kind_of({"filetype": "file", "file": "/a/b.ogg"}), "song")

    def test_sort(self):
        es = normalise([
            {"file": "/m/b/", "label": "b", "filetype": "directory"},
            {"file": "/m/A/", "label": "A", "filetype": "directory"},
            {"file": "/m/z.flac", "filetype": "file", "duration": 10, "artist": ["Q"]},
            {"file": "/m/y.flac", "filetype": "file", "duration": 300, "artist": ["P"]}])
        self.assertEqual([e["name"] for e in sort_entries(es)], ["A", "b", "y.flac", "z.flac"])
        self.assertEqual([e["name"] for e in sort_entries(es, "duration", True)],
                         ["A", "b", "y.flac", "z.flac"])
        self.assertEqual([e["name"] for e in sort_entries(es, "name", True)],
                         ["b", "A", "z.flac", "y.flac"])
        self.assertEqual(sort_entries(es, "artist")[2]["artist"], "P")

    def test_size(self):
        self.assertEqual(human_size(0), "")
        self.assertEqual(human_size(999), "999 B")
        self.assertEqual(human_size(1_500_000), "1.5 MB")


if __name__ == "__main__":
    unittest.main()
