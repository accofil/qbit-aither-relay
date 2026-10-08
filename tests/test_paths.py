import os
import tempfile
import unittest

from aither_relay.paths import absolute_files, hardlinked_files, is_under, sharing_torrents


class PathTests(unittest.TestCase):
    def test_is_under(self):
        self.assertTrue(is_under("/a/b/c", "/a/b"))
        self.assertTrue(is_under("/a/b", "/a/b/"))
        self.assertFalse(is_under("/a/bc", "/a/b"))

    def test_sharing(self):
        sets = {
            "a": absolute_files("/hdd", [{"name": "Movie/movie.mkv"}, {"name": "Movie/movie.nfo"}]),
            "b": absolute_files("/hdd", [{"name": "Movie/movie.mkv"}]),
            "c": absolute_files("/hdd", [{"name": "Other/x.mkv"}]),
        }
        self.assertEqual(sharing_torrents("a", sets), {"b"})
        self.assertEqual(sharing_torrents("c", sets), set())

    def test_hardlinks(self):
        with tempfile.TemporaryDirectory() as d:
            p, q = os.path.join(d, "f"), os.path.join(d, "g")
            open(p, "w").close()
            self.assertEqual(hardlinked_files({p}), [])
            os.link(p, q)
            self.assertEqual(hardlinked_files({p, os.path.join(d, "missing")}), [p])


if __name__ == "__main__":
    unittest.main()
