"""Level-0 seeding: the video-free birth path + the pipeline's level gating.

Run: python -m unittest discover -s tests
Everything network-touching (TMDb credits, Wikidata title card) is stubbed, so
these run offline. Covers: seed doc shape (empty intervals, no faces
provenance, title/year stamped), schema validity, manifest mapping, and that
run_title routes level 0 → seed / skips music / upgrades a seed at level 1.
"""
import json
import tempfile
import unittest
from pathlib import Path

from xray import schema, store as st
from xray.passes import index_title


class FakeSource:
    key_prefix = "plex"

    def __init__(self, item):
        self._item = item

    def resolve(self, item_id):
        return self._item


MOVIE = {
    "ratingKey": "288", "type": "movie", "title": "Casino",
    "durationMs": 10680000, "tmdbId": "769",
}
LABELS = {"title": "Casino", "year": 1995, "series": None}
CAST = [
    {"actorId": "tmdb:380", "name": "Robert De Niro", "character": "Ace",
     "thumb": "https://image.tmdb.org/t/p/w342/x.jpg", "images": []},
    {"actorId": "tmdb:1158", "name": "Joe Pesci", "character": "Nicky",
     "thumb": None, "images": []},
]


def _patch(monkeypatch_targets):
    """Return a context manager that swaps module attrs and restores them."""
    import contextlib

    @contextlib.contextmanager
    def ctx():
        saved = [(m, n, getattr(m, n)) for m, n, _ in monkeypatch_targets]
        for m, n, v in monkeypatch_targets:
            setattr(m, n, v)
        try:
            yield
        finally:
            for m, n, v in saved:
                setattr(m, n, v)
    return ctx()


class TestLevel0Birth(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _seed(self, labels=None):
        from xray import refs as refsmod
        bundle = {"cast": CAST,
                  "labels": LABELS if labels is None else labels}
        with _patch([
            (refsmod, "movie_bundle", lambda *a, **k: bundle),
        ]):
            return index_title.run_level0(
                self.store, source=FakeSource(MOVIE), tmdb_key="KEY",
                rating_key="288")

    def test_seed_doc_shape(self):
        res = self._seed()
        self.assertEqual(res["contentId"], "tmdb-movie-769")
        self.assertEqual(res["intervals"], 0)
        self.assertEqual(res["cast"], 2)
        doc = json.loads((self.store / "tmdb-movie-769.json").read_text())
        self.assertEqual(doc["cast"][0]["name"], "Robert De Niro")
        self.assertEqual(doc["actorIntervals"], [])
        self.assertEqual(doc["musicIntervals"], [])
        # No faces provenance: that absence is the level-0 marker.
        self.assertNotIn("faces", doc.get("provenance") or {})
        self.assertEqual(doc["sourceRuntimeMs"], 10680000)

    def test_seed_carries_the_display_labels(self):
        self._seed()
        doc = json.loads((self.store / "tmdb-movie-769.json").read_text())
        self.assertEqual(doc["title"], "Casino")
        self.assertEqual(doc["year"], 1995)
        self.assertNotIn("series", doc)   # a film has no show

    def test_labels_are_omitted_when_unknown_not_written_as_null(self):
        # Absent reads as "unknown"; a null invites clients to render "".
        self._seed(labels={"title": None, "year": None, "series": None})
        doc = json.loads((self.store / "tmdb-movie-769.json").read_text())
        for key in ("title", "year", "series"):
            self.assertNotIn(key, doc)
        st.validate(doc)

    def test_seed_validates_against_the_contract(self):
        self._seed()
        doc = json.loads((self.store / "tmdb-movie-769.json").read_text())
        st.validate(doc)  # raises if invalid

    def test_seed_maps_the_manifest_lookup(self):
        self._seed()
        manifest = st.load_manifest(self.store)
        self.assertEqual(manifest["lookup"]["plex:288"], "tmdb-movie-769.json")


class TestLevelGating(unittest.TestCase):
    """run_title routing by level, with the index/enrichment passes stubbed."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Path(self.tmp.name)
        self.calls = []

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, level, preexisting=None):
        from xray import pipeline
        from xray.passes import index_title as idx
        from xray.passes import people as people_pass
        from xray.passes import trivia as trivia_pass

        if preexisting is not None:
            st.write_timeline(st.canonical_path(self.store, "tmdb-movie-769"),
                              preexisting)

        def rec(name):
            def f(*a, **k):
                self.calls.append(name)
                # emulate a real index birthing the file with faces provenance
                if name in ("index", "seed"):
                    doc = schema.timeline("tmdb-movie-769", CAST, [], None,
                                          duration_ms=10680000)
                    if name == "index":
                        doc["provenance"]["faces"] = {"generated": "x",
                                                      "version": "sface-v1"}
                    st.write_timeline(
                        st.canonical_path(self.store, "tmdb-movie-769"), doc)
            return f

        with _patch([
            (idx, "run", rec("index")),
            (idx, "run_level0", rec("seed")),
            (people_pass, "run", rec("people")),
            (trivia_pass, "run", rec("trivia")),
        ]):
            return pipeline.run_title(
                self.store, source=FakeSource(MOVIE), tmdb_key="KEY",
                audd_token="", rating_key="288", skip=set(), level=level,
                log=lambda *a: None)

    def test_level0_seeds_and_skips_music(self):
        res = self._run(level=0)
        self.assertIn("seed", self.calls)
        self.assertNotIn("index", self.calls)
        self.assertEqual(res["steps"]["music"], "skipped(level0)")
        # enrichment still runs at level 0
        self.assertIn("people", self.calls)
        self.assertIn("trivia", self.calls)

    def test_level1_full_index_when_absent(self):
        self._run(level=1)
        self.assertIn("index", self.calls)
        self.assertNotIn("seed", self.calls)

    def test_level1_upgrades_a_level0_seed(self):
        seed = schema.timeline("tmdb-movie-769", CAST, [], None,
                               duration_ms=10680000)
        seed["provenance"]["people"] = {"generated": "x", "version": "tmdb-v1"}
        res = self._run(level=1, preexisting=seed)
        self.assertIn("index", self.calls)  # no faces prov → upgrade fires
        self.assertEqual(res["steps"]["index"], "ok")

    def test_level1_skips_a_full_timeline(self):
        full = schema.timeline("tmdb-movie-769", CAST, [], "sface-v1",
                               duration_ms=10680000)
        res = self._run(level=1, preexisting=full)  # already has faces prov
        self.assertNotIn("index", self.calls)
        self.assertEqual(res["steps"]["index"], "exists")

    def test_level0_skips_when_any_timeline_exists(self):
        seed = schema.timeline("tmdb-movie-769", CAST, [], None,
                               duration_ms=10680000)
        res = self._run(level=0, preexisting=seed)
        self.assertNotIn("seed", self.calls)
        self.assertEqual(res["steps"]["index"], "exists")


if __name__ == "__main__":
    unittest.main()
