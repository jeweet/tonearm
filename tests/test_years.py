import unittest

from tonearm.cache import LibraryCache
from tonearm.years import clean_year, year_from_date


class YearsTest(unittest.TestCase):
    def test_clean(self):
        self.assertEqual([clean_year(v) for v in (1, "1", 0, None, "", 1992, "1992", 19)],
                         [0, 0, 0, 0, 0, 1992, 1992, 0])
        self.assertEqual(year_from_date("1992-05-01"), 1992)
        self.assertEqual(year_from_date("0001-01-01"), 0)

    def test_cache_marks_year_one(self):
        c = LibraryCache(":memory:")
        c.upsert_albums([{"albumid": 1, "title": "A", "year": 1},
                         {"albumid": 2, "title": "B", "year": 1999},
                         {"albumid": 3, "title": "C", "year": 0}])
        self.assertEqual([c.album(i).year for i in (1, 2, 3)], [0, 1999, 0])
        self.assertEqual(c.year_fix_ids(), [1])
        c.fix_years({1: 1992}, [1])
        self.assertEqual(c.album(1).year, 1992)
        self.assertEqual(c.year_fix_ids(), [])


if __name__ == "__main__":
    unittest.main()
