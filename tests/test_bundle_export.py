"""Bundle export: a library as one JSON Lines file, safe to hand to a hub.

Two things matter here. Share-safety must be identical to the single-file
export, because a bundle is the path that will actually carry a whole library
off the machine. And chunking must respect the caps a hub enforces, since a
bundle that is refused wholesale is worse than several that are not.
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("XRAY_STORE", tempfile.mkdtemp())

from xray import share, store as st  # noqa: E402


def timeline(cid="tmdb-movie-769", **kw):
    doc = {"contentId": cid, "version": 1, "generated": "2026-07-25T00:00:00Z",
           "sourceRuntimeMs": 6000000, "cast": [], "actorIntervals": [],
           "musicIntervals": [], "trivia": []}
    doc.update(kw)
    return doc


LICENSED = {"actorId": "tmdb:380", "name": "Robert De Niro",
            "character": "James Conway",
            "thumb": "https://image.tmdb.org/t/p/w185/x.jpg",
            "person": {"bio": "…", "knownFor": []}}


class BundleCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Path(self.tmp.name)
        self.out = self.store / "exports"

    def put(self, doc):
        st.write_timeline(st.canonical_path(self.store, doc["contentId"]), doc)
        return doc["contentId"]


class TestShareSafety(BundleCase):
    def test_a_bundle_line_is_stripped_exactly_like_a_file_export(self):
        """One stripping implementation, so the two paths cannot drift."""
        cid = self.put(timeline(cast=[dict(LICENSED)]))
        single = json.loads(share.export_timeline(
            self.store, cid, self.out / "single").read_text())
        files = share.export_bundle(self.store, [cid], self.out)
        line = share.read_bundle(files[0])[0]
        self.assertEqual(single, line)

    def test_licensed_person_data_never_reaches_the_bundle(self):
        cid = self.put(timeline(cast=[dict(LICENSED)]))
        files = share.export_bundle(self.store, [cid], self.out)
        raw = files[0].read_text()
        self.assertNotIn("knownFor", raw)
        self.assertNotIn("image.tmdb.org", raw)
        doc = share.read_bundle(files[0])[0]
        self.assertNotIn("person", doc["cast"][0])
        self.assertNotIn("thumb", doc["cast"][0])
        self.assertEqual(doc["cast"][0]["name"], "Robert De Niro")

    def test_every_line_is_a_valid_timeline_on_its_own(self):
        ids = [self.put(timeline(f"tmdb-movie-{i}")) for i in range(5)]
        files = share.export_bundle(self.store, ids, self.out)
        for doc in share.read_bundle(files[0]):
            st.validate(doc)  # raises if not

    def test_one_record_per_line(self):
        ids = [self.put(timeline(f"tmdb-movie-{i}")) for i in range(4)]
        files = share.export_bundle(self.store, ids, self.out)
        lines = files[0].read_text().splitlines()
        self.assertEqual(len(lines), 4)
        for line in lines:
            json.loads(line)  # each line stands alone


class TestChunking(BundleCase):
    def test_one_chunk_is_named_without_a_number(self):
        ids = [self.put(timeline(f"tmdb-movie-{i}")) for i in range(3)]
        files = share.export_bundle(self.store, ids, self.out)
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0].name, f"bundle{share.BUNDLE_SUFFIX}")

    def test_the_item_cap_splits_and_loses_nothing(self):
        ids = [self.put(timeline(f"tmdb-movie-{i}")) for i in range(10)]
        files = share.export_bundle(self.store, ids, self.out, max_items=4)
        self.assertEqual([f.name for f in files],
                         [f"bundle-{n}{share.BUNDLE_SUFFIX}" for n in (1, 2, 3)])
        got = [d["contentId"] for f in files for d in share.read_bundle(f)]
        self.assertEqual(sorted(got), sorted(ids))

    def test_the_byte_cap_splits_too(self):
        ids = [self.put(timeline(f"tmdb-movie-{i}")) for i in range(6)]
        one = len(json.dumps(share.share_doc(self.store, ids[0], warn=False)[1],
                             ensure_ascii=False, separators=(",", ":"))) + 1
        files = share.export_bundle(self.store, ids, self.out,
                                    max_bytes=one * 2 + 1)
        self.assertGreater(len(files), 1)
        got = [d["contentId"] for f in files for d in share.read_bundle(f)]
        self.assertEqual(sorted(got), sorted(ids))

    def test_no_chunk_exceeds_the_byte_cap(self):
        ids = [self.put(timeline(f"tmdb-movie-{i}")) for i in range(12)]
        cap = 900
        for f in share.export_bundle(self.store, ids, self.out, max_bytes=cap):
            self.assertLessEqual(f.stat().st_size, cap)

    def test_generator_input_is_not_consumed_before_counting(self):
        """`keys` used to be iterated twice; a generator would come up empty."""
        ids = [self.put(timeline(f"tmdb-movie-{i}")) for i in range(3)]
        files = share.export_bundle(self.store, (i for i in ids), self.out)
        self.assertEqual(len(share.read_bundle(files[0])), 3)


class TestSkipping(BundleCase):
    def test_an_unshareable_title_is_skipped_not_fatal(self):
        """One bad title must not cost the caller the rest of the library."""
        good = [self.put(timeline(f"tmdb-movie-{i}")) for i in range(3)]
        files = share.export_bundle(self.store, good + ["tmdb-movie-404"],
                                    self.out)
        got = [d["contentId"] for d in share.read_bundle(files[0])]
        self.assertEqual(sorted(got), sorted(good))

    def test_all_unshareable_writes_nothing(self):
        self.assertEqual(share.export_bundle(self.store, ["tmdb-movie-404"],
                                             self.out), [])


class TestReadBundle(BundleCase):
    def test_a_broken_line_names_itself(self):
        p = self.out
        p.mkdir(parents=True, exist_ok=True)
        f = p / f"x{share.BUNDLE_SUFFIX}"
        f.write_text('{"contentId":"tmdb-movie-1"}\n{broken\n')
        with self.assertRaises(SystemExit) as cm:
            share.read_bundle(f)
        self.assertIn("line 2", str(cm.exception))

    def test_blank_lines_are_tolerated(self):
        p = self.out
        p.mkdir(parents=True, exist_ok=True)
        f = p / f"x{share.BUNDLE_SUFFIX}"
        f.write_text('{"contentId":"a"}\n\n{"contentId":"b"}\n')
        self.assertEqual(len(share.read_bundle(f)), 2)


class TestUploadTokenHonesty(unittest.TestCase):
    def test_no_token_configured_by_default(self):
        """The dashboard keys off this to offer a file instead of a dead POST."""
        saved = os.environ.pop("XRAY_HUB_UPLOAD_TOKEN", None)
        try:
            self.assertEqual(share.upload_token(), "")
        finally:
            if saved is not None:
                os.environ["XRAY_HUB_UPLOAD_TOKEN"] = saved

    def test_a_configured_token_is_picked_up(self):
        os.environ["XRAY_HUB_UPLOAD_TOKEN"] = "  s3cret  "
        try:
            self.assertEqual(share.upload_token(), "s3cret")
        finally:
            del os.environ["XRAY_HUB_UPLOAD_TOKEN"]


if __name__ == "__main__":
    unittest.main()
