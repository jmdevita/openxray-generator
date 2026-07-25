"""Run planning: coverage arithmetic and the cost estimate.

The point of these is that the plan must agree with pipeline.run_title. A
plan that quietly disagrees is worse than no plan, so the level-0/level-1
branching differences are pinned here explicitly rather than assumed.
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from xray import plan as pl  # noqa: E402
from xray import store as st  # noqa: E402


class FakeLibrary:
    """A source whose whole library is declared up front."""

    key_prefix = "fake"

    def __init__(self, ids):
        self.ids = ids

    def content_ids(self, section_key):
        return dict(self.ids)


def write(store, cid, blocks):
    doc = {"contentId": cid, "provenance": {b: {"generated": "x"} for b in blocks}}
    st.canonical_path(store, cid).write_text(json.dumps(doc))


class PlanCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def plan(self, ids, *, catalog=None, audd=False, hub="http://hub",
             kind="movie", headroom=None):
        with mock.patch.object(pl, "hub_catalog_for", return_value=catalog):
            return pl.library_plan(self.store, FakeLibrary(ids), "Movies",
                                   hub_url=hub if catalog is not None else "",
                                   audd=audd, kind=kind,
                                   audd_headroom=headroom)


class TestCoverage(PlanCase):
    def test_unidentified_titles_are_counted_not_planned(self):
        p = self.plan({"a": "tmdb-movie-1", "b": None, "c": None})
        self.assertEqual(p["total"], 3)
        self.assertEqual(p["unidentified"], 2)
        self.assertEqual(p["distinct"], 1)
        self.assertEqual(p["levels"]["0"]["todo"], 1)

    def test_duplicate_files_are_one_unit_of_work(self):
        # Two rips of the same film share a content id: one timeline, one job.
        p = self.plan({"a": "tmdb-movie-1", "b": "tmdb-movie-1"})
        self.assertEqual(p["total"], 2)
        self.assertEqual(p["distinct"], 1)
        self.assertEqual(p["levels"]["1"]["todo"], 1)

    def test_seed_and_full_are_distinguished_by_the_faces_block(self):
        write(self.store, "tmdb-movie-1", ["people", "trivia"])       # seed
        write(self.store, "tmdb-movie-2", ["people", "faces"])        # full
        p = self.plan({"a": "tmdb-movie-1", "b": "tmdb-movie-2"})
        self.assertEqual((p["haveSeed"], p["haveFull"]), (1, 1))

    def test_unreadable_timeline_counts_as_a_seed_not_a_crash(self):
        st.canonical_path(self.store, "tmdb-movie-1").write_text("{not json")
        p = self.plan({"a": "tmdb-movie-1"})
        self.assertEqual(p["haveSeed"], 1)


class TestLevelsMirrorThePipeline(PlanCase):
    def test_level0_skips_anything_already_stored(self):
        write(self.store, "tmdb-movie-1", ["people"])      # a seed
        write(self.store, "tmdb-movie-2", ["faces"])       # a full index
        p = self.plan({"a": "tmdb-movie-1", "b": "tmdb-movie-2",
                       "c": "tmdb-movie-3"})
        self.assertEqual(p["levels"]["0"]["todo"], 1)      # only the new one

    def test_level0_ignores_the_hub(self):
        # run_title never fetches at level 0: seeding is local and cheap.
        cat = {"tmdb-movie-3": {"units": ["faces"]}}
        p = self.plan({"c": "tmdb-movie-3"}, catalog=cat)
        self.assertEqual(p["levels"]["0"]["todo"], 1)
        self.assertEqual(p["levels"]["0"]["fromHub"], 0)

    def test_level1_subtracts_hub_hits_for_titles_with_nothing_local(self):
        cat = {"tmdb-movie-3": {"units": ["faces"]}}
        p = self.plan({"c": "tmdb-movie-3", "d": "tmdb-movie-4"}, catalog=cat)
        self.assertEqual(p["levels"]["1"]["fromHub"], 1)
        self.assertEqual(p["levels"]["1"]["todo"], 1)      # only movie-4

    def test_level1_upgrades_local_seeds_even_when_the_hub_has_them(self):
        # This mirrors a real quirk of run_title: the hub is only consulted
        # when NOTHING is stored locally, so a seed is always upgraded here.
        write(self.store, "tmdb-movie-1", ["people"])
        cat = {"tmdb-movie-1": {"units": ["faces", "music"]}}
        p = self.plan({"a": "tmdb-movie-1"}, catalog=cat)
        self.assertEqual(p["levels"]["1"]["todo"], 1)
        self.assertEqual(p["levels"]["1"]["fromHub"], 0)
        self.assertEqual(p["levels"]["1"]["hubCouldServe"], 1)  # the gap, named

    def test_a_seed_only_hub_entry_still_counts_as_a_hub_hit(self):
        # run_title takes any hub hit and skips indexing, units regardless.
        cat = {"tmdb-movie-3": {"units": ["trivia"]}}
        p = self.plan({"c": "tmdb-movie-3"}, catalog=cat)
        self.assertEqual(p["levels"]["1"]["fromHub"], 1)
        self.assertEqual(p["levels"]["1"]["todo"], 0)

    def test_fully_indexed_titles_are_work_at_neither_level(self):
        write(self.store, "tmdb-movie-2", ["faces"])
        p = self.plan({"b": "tmdb-movie-2"})
        self.assertEqual(p["levels"]["0"]["todo"], 0)
        self.assertEqual(p["levels"]["1"]["todo"], 0)


class TestHubReachability(PlanCase):
    def test_unreachable_hub_is_not_reported_as_empty(self):
        p = self.plan({"a": "tmdb-movie-1"}, catalog=None, hub="http://hub")
        self.assertFalse(p["hubChecked"])

    def test_empty_hub_is_reported_as_checked(self):
        p = self.plan({"a": "tmdb-movie-1"}, catalog={})
        self.assertTrue(p["hubChecked"])
        self.assertEqual(p["hubCatalog"], 0)


class TestEstimate(PlanCase):
    def test_money_only_applies_to_a_full_index_with_audd(self):
        ids = {"a": "tmdb-movie-1", "b": "tmdb-movie-2"}
        self.assertEqual(self.plan(ids)["levels"]["1"]["dollars"], [0.0, 0.0])
        with_audd = self.plan(ids, audd=True)
        lo, hi = pl.CUES_PER_TITLE["movie"]
        self.assertEqual(with_audd["levels"]["1"]["dollars"],
                         [round(2 * lo * pl.AUDD_PER_CALL, 2),
                          round(2 * hi * pl.AUDD_PER_CALL, 2)])
        # Seeding never opens the audio, so it is free whatever the token says.
        self.assertEqual(with_audd["levels"]["0"]["dollars"], [0.0, 0.0])

    def test_billing_is_per_cue_so_shows_cost_less_than_movies(self):
        # A TV episode carries a third of a movie's cues; one flat per-title
        # rate would misprice whichever of the two you did not calibrate on.
        ids = {"a": "tmdb-movie-1", "b": "tmdb-movie-2"}
        movie = self.plan(ids, audd=True, kind="movie")["levels"]["1"]
        show = self.plan(ids, audd=True, kind="show")["levels"]["1"]
        self.assertGreater(movie["dollars"][1], show["dollars"][1])
        self.assertEqual(show["cues"], [2 * 5, 2 * 15])

    def test_budget_cap_is_reported_not_silently_applied(self):
        # The quote stays the true cost of the work; the cap is a separate
        # warning, because "it will stop early" is the useful part.
        ids = {chr(97 + i): f"tmdb-movie-{i}" for i in range(20)}
        lv = self.plan(ids, audd=True, headroom=300)["levels"]["1"]
        self.assertEqual(lv["dollars"][1], round(20 * 40 * 0.005, 2))  # $4.00
        self.assertEqual(lv["titlesBeforeCap"], 300 // 40)             # 7

    def test_no_cap_reported_when_the_budget_covers_the_work(self):
        lv = self.plan({"a": "tmdb-movie-1"}, audd=True,
                       headroom=10_000)["levels"]["1"]
        self.assertIsNone(lv["titlesBeforeCap"])

    def test_unlimited_budget_never_reports_a_cap(self):
        ids = {chr(97 + i): f"tmdb-movie-{i}" for i in range(20)}
        lv = self.plan(ids, audd=True, headroom=None)["levels"]["1"]
        self.assertIsNone(lv["titlesBeforeCap"])

    def test_time_scales_with_work_and_a_full_index_costs_more(self):
        p = self.plan({"a": "tmdb-movie-1", "b": "tmdb-movie-2"})
        self.assertEqual(p["levels"]["0"]["seconds"],
                         [2 * pl.SEED_SECONDS[0], 2 * pl.SEED_SECONDS[1]])
        self.assertGreater(p["levels"]["1"]["seconds"][0],
                           p["levels"]["0"]["seconds"][1])

    def test_nothing_to_do_costs_nothing(self):
        write(self.store, "tmdb-movie-2", ["faces"])
        lv = self.plan({"b": "tmdb-movie-2"})["levels"]["1"]
        self.assertEqual((lv["seconds"], lv["dollars"]), ([0, 0], [0.0, 0.0]))


if __name__ == "__main__":
    unittest.main()
