"""Display labels: title / year / series.

These are the only fields in the contract that exist purely for humans, so
what matters is that they never pretend to know something (absent beats
null), never grow past the schema's cap, and never get mistaken for identity.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from xray import schema, store as st  # noqa: E402
from xray.passes.index_title import merge_preserved  # noqa: E402
from xray.refs import _year  # noqa: E402


class TestYearParsing(unittest.TestCase):
    def test_pulls_the_leading_year(self):
        self.assertEqual(_year("1990-09-19"), 1990)
        self.assertEqual(_year("2016"), 2016)

    def test_junk_and_missing_dates_are_none_not_zero(self):
        for value in (None, "", "n/a", "TBA", "--"):
            self.assertIsNone(_year(value), value)


class TestDisplayLabels(unittest.TestCase):
    def test_film_gets_title_and_year_but_no_series(self):
        out = schema.display_labels({"title": "Goodfellas", "year": 1990,
                                     "series": None})
        self.assertEqual(out, {"title": "Goodfellas", "year": 1990})

    def test_episode_keeps_the_series(self):
        out = schema.display_labels({"title": "Pilot", "year": 2016,
                                     "series": "Billions"})
        self.assertEqual(out["series"], "Billions")
        self.assertEqual(out["title"], "Pilot")

    def test_nothing_is_written_when_nothing_is_known(self):
        # Absent, not null: a key that isn't there reads as "unknown".
        self.assertEqual(schema.display_labels(None), {})
        self.assertEqual(schema.display_labels({}), {})
        self.assertEqual(
            schema.display_labels({"title": None, "year": None,
                                   "series": None}), {})

    def test_blank_and_whitespace_titles_drop_out(self):
        self.assertEqual(schema.display_labels({"title": "   "}), {})

    def test_titles_are_trimmed_and_capped_at_the_schema_limit(self):
        out = schema.display_labels({"title": "  " + "x" * 500 + "  "})
        self.assertEqual(len(out["title"]), schema.LABEL_MAX)

    def test_implausible_years_are_dropped(self):
        for bad in (0, 12, "1990", None, 1500):
            self.assertNotIn("year", schema.display_labels({"year": bad}), bad)


class TestTimelineIntegration(unittest.TestCase):
    def test_labels_land_on_the_doc_and_it_still_validates(self):
        doc = schema.timeline("tmdb-tv-62852-s01e01", [],
                              labels={"title": "Pilot", "year": 2016,
                                      "series": "Billions"})
        st.validate(doc)
        self.assertEqual((doc["title"], doc["series"]), ("Pilot", "Billions"))

    def test_a_timeline_without_labels_is_still_valid(self):
        st.validate(schema.timeline("tmdb-movie-769", []))

    def test_an_over_long_title_cannot_reach_the_doc(self):
        # The cap is enforced on write, not trusted from the caller, so the
        # schema's maxLength can never be the thing that catches it.
        doc = schema.timeline("tmdb-movie-769", [],
                              labels={"title": "y" * 900})
        st.validate(doc)


class TestUpgradePreservesLabels(unittest.TestCase):
    def test_a_failed_refetch_does_not_strip_an_existing_title(self):
        old = {"title": "Goodfellas", "year": 1990}
        new = {"contentId": "tmdb-movie-769"}          # TMDb gave nothing
        merged = merge_preserved(old, new)
        self.assertEqual(merged["title"], "Goodfellas")
        self.assertEqual(merged["year"], 1990)

    def test_a_fresh_title_wins_over_the_old_one(self):
        old = {"title": "Old Name"}
        new = {"contentId": "tmdb-movie-769", "title": "Corrected Name"}
        self.assertEqual(merge_preserved(old, new)["title"], "Corrected Name")


def indexed(**over):
    """A fully indexed timeline: face intervals and a faces stamp."""
    doc = {"contentId": "tmdb-tv-62852-s01e01",
           "actorIntervals": [{"actorId": "tmdb:1", "startMs": 0, "endMs": 9,
                               "confidence": 0.9}] * 239,
           "musicIntervals": [{"title": "Layla", "startMs": 0, "endMs": 5}],
           "trivia": [{"text": "A fact."}],
           "provenance": {"faces": {"generated": "2026-07-20T00:00:00Z",
                                    "version": "sface-v1"},
                          "music": {"generated": "…", "version": "audd-v1"}}}
    doc.update(over)
    return doc


def seed(**over):
    """What run_level0 writes: cast and labels, no intervals, no faces stamp."""
    doc = {"contentId": "tmdb-tv-62852-s01e01", "actorIntervals": [],
           "musicIntervals": [], "trivia": [], "provenance": {},
           "title": "Pilot", "year": 2016, "series": "Billions"}
    doc.update(over)
    return doc


class TestASeedNeverOverwritesAnIndex(unittest.TestCase):
    """A level-0 pass over a library must not undo the video work.

    run_level0 writes empty intervals and no faces stamp by design, and every
    write goes through merge_preserved. Without a guard there, seeding a title
    that was already indexed throws away minutes of frame decoding and face
    embedding — and says nothing about it."""

    def test_face_intervals_survive_a_seed(self):
        self.assertEqual(len(merge_preserved(indexed(), seed())["actorIntervals"]),
                         239)

    def test_the_faces_stamp_survives_a_seed(self):
        merged = merge_preserved(indexed(), seed())
        self.assertEqual(merged["provenance"]["faces"]["version"], "sface-v1")

    def test_the_seed_still_contributes_its_labels(self):
        """The reason to re-seed an indexed title at all: it backfills labels."""
        merged = merge_preserved(indexed(), seed())
        self.assertEqual((merged["series"], merged["title"], merged["year"]),
                         ("Billions", "Pilot", 2016))

    def test_a_real_reindex_still_replaces_intervals(self):
        """The guard keys off the absence of a faces stamp, so an actual
        re-index — which writes both — is unaffected."""
        fresh = seed(actorIntervals=[{"actorId": "tmdb:2", "startMs": 0,
                                      "endMs": 5, "confidence": 0.8}],
                     provenance={"faces": {"generated": "2026-07-25T00:00:00Z",
                                           "version": "sface-v2"}})
        merged = merge_preserved(indexed(), fresh)
        self.assertEqual(len(merged["actorIntervals"]), 1)
        self.assertEqual(merged["provenance"]["faces"]["version"], "sface-v2")

    def test_paid_and_cached_work_survives_either_way(self):
        reindex = seed(actorIntervals=[{"actorId": "tmdb:2", "startMs": 0,
                                        "endMs": 5, "confidence": 0.8}],
                       provenance={"faces": {"generated": "x",
                                             "version": "sface-v2"}})
        for new in (seed(), reindex):
            merged = merge_preserved(indexed(), new)
            self.assertEqual(len(merged["musicIntervals"]), 1, new)
            self.assertEqual(len(merged["trivia"]), 1, new)

    def test_seeding_a_title_that_does_not_exist_yet_is_a_plain_seed(self):
        merged = merge_preserved({}, seed())
        self.assertEqual(merged["actorIntervals"], [])
        self.assertNotIn("faces", merged.get("provenance", {}))

    def test_reseeding_a_seed_stays_a_seed(self):
        """No faces on either side: nothing to protect, nothing invented."""
        merged = merge_preserved(seed(title="Old"), seed())
        self.assertEqual(merged["actorIntervals"], [])
        self.assertNotIn("faces", merged.get("provenance", {}))


if __name__ == "__main__":
    unittest.main()
