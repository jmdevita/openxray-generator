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


if __name__ == "__main__":
    unittest.main()
