"""Episode selectors typed into the search box.

The risk here is not failing to parse: it is parsing something that was never
a selector and then searching for the wrong show. So the negative cases carry
as much weight as the positive ones.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from xray import query as qy  # noqa: E402
from xray.service import orchestrator as O  # noqa: E402


class SplitSelector(unittest.TestCase):
    def test_the_forms_people_type(self):
        for text, want in (
            ("smallville s1e1", ("smallville", 1, 1)),
            ("Smallville S01E01", ("Smallville", 1, 1)),
            ("smallville s1 e1", ("smallville", 1, 1)),
            ("smallville 1x01", ("smallville", 1, 1)),
            ("smallville season 1 episode 3", ("smallville", 1, 3)),
            ("the office s9", ("the office", 9, None)),
            ("smallville.s01e02", ("smallville", 1, 2)),
        ):
            self.assertEqual(qy.split_selector(text), want, text)

    def test_specials_parse_like_any_other_season(self):
        """Season 0 is real, so downstream must test `is not None`."""
        self.assertEqual(qy.split_selector("smallville s0e1"), ("smallville", 0, 1))
        self.assertEqual(qy.split_selector("smallville s0"), ("smallville", 0, None))

    def test_titles_that_merely_end_in_numbers_are_left_alone(self):
        """A wrong split searches for the wrong show and says nothing."""
        for text in ("Se7en", "Blade Runner 2049", "Apollo 13", "Ocean's 11",
                     "Toy Story 3", "1917", "Fahrenheit 451"):
            self.assertEqual(qy.split_selector(text), (text, None, None), text)

    def test_a_plain_show_name_is_untouched(self):
        self.assertEqual(qy.split_selector("smallville"), ("smallville", None, None))

    def test_a_bare_selector_is_not_a_search(self):
        """Nothing left to search for once the selector is removed."""
        self.assertEqual(qy.split_selector("s1e1"), ("s1e1", None, None))

    def test_empty_and_blank(self):
        self.assertEqual(qy.split_selector(""), ("", None, None))
        self.assertEqual(qy.split_selector("   "), ("", None, None))
        self.assertEqual(qy.split_selector(None), ("", None, None))


class Pick(unittest.TestCase):
    LEAVES = [{"ratingKey": "1", "season": 0, "episode": 1},
              {"ratingKey": "2", "season": 1, "episode": 1},
              {"ratingKey": "3", "season": 1, "episode": 2},
              {"ratingKey": "4", "season": 2, "episode": 1}]

    def test_a_whole_season(self):
        self.assertEqual([lf["ratingKey"] for lf in qy.pick(self.LEAVES, 1, None)],
                         ["2", "3"])

    def test_one_episode(self):
        self.assertEqual([lf["ratingKey"] for lf in qy.pick(self.LEAVES, 1, 2)],
                         ["3"])

    def test_specials(self):
        self.assertEqual([lf["ratingKey"] for lf in qy.pick(self.LEAVES, 0, 1)],
                         ["1"])

    def test_numbers_reported_as_strings_still_match(self):
        """Backends are inconsistent, and "1" == 1 is False."""
        leaves = [{"ratingKey": "9", "season": "1", "episode": "2"}]
        self.assertEqual(len(qy.pick(leaves, 1, 2)), 1)

    def test_unparseable_numbers_are_dropped_not_matched(self):
        leaves = [{"ratingKey": "9", "season": None, "episode": "x"}]
        self.assertEqual(qy.pick(leaves, 1, 2), [])

    def test_a_season_that_does_not_exist(self):
        self.assertEqual(qy.pick(self.LEAVES, 7, None), [])


class SearchEndpoint(unittest.TestCase):
    """The selector path against a fake backend."""

    class Source:
        def __init__(self):
            self.leaf_calls = 0

        def search(self, q):
            if "smallville" in q.lower():
                return [{"ratingKey": "900", "type": "show",
                         "title": "Smallville", "year": 2001,
                         "grandparentTitle": None, "season": None,
                         "episode": None, "seriesId": None}]
            return []

        def series_leaves(self, series_id):
            self.leaf_calls += 1
            return [{"ratingKey": "901", "type": "episode", "title": "Pilot",
                     "season": 1, "episode": 1},
                    {"ratingKey": "902", "type": "episode", "title": "Metamorphosis",
                     "season": 1, "episode": 2},
                    {"ratingKey": "903", "type": "episode", "title": "Vortex",
                     "season": 2, "episode": 1}]

    def setUp(self):
        self.src = self.Source()
        self._origin, self._source = O._origin, O._source
        O._origin = lambda: "http://plex.example.com"
        O._source = lambda: self.src

    def tearDown(self):
        O._origin, O._source = self._origin, self._source

    def test_one_episode(self):
        out = O.api_search("smallville s1e1")["results"]
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["ratingKey"], "901")
        self.assertEqual(out[0]["label"], "Smallville S01E01 · Pilot")
        self.assertEqual(out[0]["seriesId"], "900")

    def test_a_whole_season(self):
        out = O.api_search("smallville s1")["results"]
        self.assertEqual([r["ratingKey"] for r in out], ["901", "902"])

    def test_no_selector_never_costs_the_extra_request(self):
        O.api_search("smallville")
        self.assertEqual(self.src.leaf_calls, 0)

    def test_a_missing_episode_falls_back_rather_than_erroring(self):
        """A typo should show the show, not an error page."""
        out = O.api_search("smallville s9e9")["results"]
        self.assertEqual([r["type"] for r in out], ["show"])

    def test_an_unknown_show_falls_back(self):
        self.assertEqual(O.api_search("nothing here s1e1")["results"], [])


if __name__ == "__main__":
    unittest.main()
