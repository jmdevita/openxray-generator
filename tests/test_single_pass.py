"""Running one pass on its own.

The store rows already show which of the four blocks a title has. A missing
one is now an offer: filling it runs that pass ALONE, which is expressed as
"skip the other three" — the same `skip` a whole-library run already takes.
So the interesting properties are that each pass really does run in isolation,
and that the dashboard offers a fill only where one could actually happen.
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("XRAY_STORE", tempfile.mkdtemp())

from xray import pipeline  # noqa: E402
from xray.service import orchestrator as O  # noqa: E402

PASSES = ["index", "people", "trivia", "music"]


class FakeSource:
    key_prefix = "plex"

    def resolve(self, rating_key):
        return {"ratingKey": rating_key, "type": "movie", "title": "T",
                "tmdbId": "769", "durationMs": 100}


def run_only(pass_name):
    """run_title with everything except `pass_name` skipped. Returns what ran."""
    ran = []
    patches = {
        "xray.passes.index_title.run": "index",
        "xray.passes.index_title.run_level0": "index",
        "xray.passes.people.run": "people",
        "xray.passes.trivia.run": "trivia",
        "xray.passes.music.run": "music",
    }
    with mock.patch("xray.passes.index_title.resolve",
                    return_value=FakeSource().resolve("1")), \
            mock.patch("xray.passes.index_title.content_id_for",
                       return_value="tmdb-movie-769"):
        stack = []
        for target, name in patches.items():
            p = mock.patch(target, side_effect=(lambda n: lambda *a, **k: ran.append(n))(name))
            p.start()
            stack.append(p)
        try:
            result = pipeline.run_title(
                Path(os.environ["XRAY_STORE"]), source=FakeSource(),
                tmdb_key="tk", audd_token="at", rating_key="1",
                skip=set(PASSES) - {pass_name}, level=1, log=lambda *a: None)
        finally:
            for p in stack:
                p.stop()
    return ran, result["steps"]


class TestEachPassRunsAlone(unittest.TestCase):
    def test_only_the_chosen_pass_runs(self):
        for chosen in PASSES:
            ran, _ = run_only(chosen)
            self.assertEqual(ran, [chosen], f"choosing {chosen} ran {ran}")

    def test_the_others_are_reported_as_skipped_by_flag(self):
        """Not silently absent — the job log should say why nothing happened."""
        for chosen in PASSES:
            _, steps = run_only(chosen)
            for other in PASSES:
                if other != chosen:
                    self.assertEqual(steps.get(other), "skipped(flag)",
                                     f"{other} while running {chosen}: {steps}")

    def test_music_alone_still_needs_level_1(self):
        """Level 0 marks music skipped(level0) before the flag is consulted, so
        a music-only run has to be level 1 or it silently does nothing."""
        ran = []
        with mock.patch("xray.passes.index_title.resolve",
                        return_value=FakeSource().resolve("1")), \
                mock.patch("xray.passes.index_title.content_id_for",
                           return_value="tmdb-movie-769"), \
                mock.patch("xray.passes.index_title.run_level0"), \
                mock.patch("xray.passes.music.run",
                           side_effect=lambda *a, **k: ran.append("music")):
            result = pipeline.run_title(
                Path(os.environ["XRAY_STORE"]), source=FakeSource(),
                tmdb_key="tk", audd_token="at", rating_key="1",
                skip={"index", "people", "trivia"}, level=0,
                log=lambda *a: None)
        self.assertEqual(ran, [])
        self.assertEqual(result["steps"]["music"], "skipped(level0)")


class TestTheDashboardOffersFillsHonestly(unittest.TestCase):
    """The store row builds the chips client-side, so these assert on the
    shipped JavaScript rather than on a rendered row."""

    def setUp(self):
        self.js = O.dashboard()

    def test_a_fill_sends_skip_of_the_other_three(self):
        self.assertIn("PASSES.filter(p => p !== pass).join(',')", self.js)

    def test_every_block_is_offerable_not_just_the_paid_one(self):
        for pass_name in PASSES:
            self.assertIn(f"'{pass_name}'", self.js, pass_name)
        # The four chips, each wired to its pipeline pass name.
        for chip, pass_name in (("cast", "people"), ("faces", "index"),
                                ("music", "music"), ("trivia", "trivia")):
            self.assertIn(f"chip('{chip}', '{pass_name}'", self.js)

    def test_a_title_with_no_server_key_offers_nothing(self):
        """A timeline fetched from the hub has no local media to run against,
        so its gaps stay plain chips instead of dead buttons."""
        self.assertIn("if (!rk) return '<span class=\"chip off\">'", self.js)

    def test_only_the_paid_pass_confirms(self):
        self.assertIn("if(pass === 'music' && !confirm(", self.js)
        self.assertIn("$0.005", self.js)

    def test_a_fill_runs_at_level_1(self):
        self.assertIn("rating_key: ratingKey, level: 1,", self.js)


if __name__ == "__main__":
    unittest.main()
