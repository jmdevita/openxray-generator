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

from xray import cli, pipeline  # noqa: E402
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

    def test_a_selection_sends_skip_of_everything_unticked(self):
        """Chips tick rather than run, and Deepen runs the ticked set as ONE
        job: two jobs would stream the media twice, where one harvests the
        audio during frame extraction for the music pass to reuse."""
        self.assertIn("PASSES.filter(p => !sel.has(p)).join(',')", self.js)

    def test_an_empty_selection_still_fills_every_gap(self):
        self.assertIn(": runSkip(1);", self.js)

    def test_ticking_survives_the_poll_repaint(self):
        """The store table is rebuilt every couple of seconds, so selection
        state cannot live in the DOM or it would clear itself mid-click."""
        self.assertIn("const picked = new Map();", self.js)

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

    def test_money_confirms_including_the_no_selection_case(self):
        """Deepen with nothing ticked runs music too when a token is set, and
        used to bill without asking."""
        self.assertIn("const willBillForMusic = sel && sel.size", self.js)
        self.assertIn("? sel.has('music')", self.js)
        self.assertIn("(SETUP && SETUP.auddConfigured)", self.js)
        self.assertIn("$0.005", self.js)

    def test_a_fill_runs_at_level_1(self):
        self.assertIn("{rating_key: ratingKey, level: 1, skip: skip}", self.js)



class FakeShow:
    """A show with Specials (season 0) plus two real seasons."""
    key_prefix = "plex"

    LEAVES = ([{"ratingKey": f"s0e{i}", "season": 0, "episode": i} for i in (1, 2)]
              + [{"ratingKey": f"s1e{i}", "season": 1, "episode": i} for i in (1, 2, 3)]
              + [{"ratingKey": f"s2e{i}", "season": 2, "episode": i} for i in (1,)])

    def series_leaves(self, series_id):
        return list(self.LEAVES)

    def section_leaves(self, name):
        return list(self.LEAVES)


def targets(**kw):
    return pipeline.enumerate_targets(FakeShow(), **kw)


class TestSeasonFilter(unittest.TestCase):
    def test_no_season_is_the_whole_show(self):
        self.assertEqual(len(targets(series="99")), 6)

    def test_a_season_narrows_to_its_episodes(self):
        self.assertEqual(targets(series="99", season=1),
                         ["s1e1", "s1e2", "s1e3"])

    def test_specials_are_a_real_season(self):
        """Season 0 is Specials. A truthiness check would treat asking for it
        as asking for the whole show — six episodes instead of two."""
        self.assertEqual(targets(series="99", season=0), ["s0e1", "s0e2"])

    def test_an_empty_season_refuses_rather_than_running_nothing(self):
        with self.assertRaises(ValueError) as cm:
            targets(series="99", season=7)
        self.assertIn("season 7", str(cm.exception))

    def test_season_needs_a_series_to_mean_anything(self):
        """Passing it with a library must not silently filter the library."""
        self.assertEqual(len(targets(library="Movies", season=1)), 6)

    def test_a_leaf_with_no_season_never_matches(self):
        class Odd(FakeShow):
            def series_leaves(self, series_id):
                return [{"ratingKey": "x", "season": None, "episode": 1},
                        {"ratingKey": "y", "season": 1, "episode": 1}]
        self.assertEqual(
            pipeline.enumerate_targets(Odd(), series="99", season=1), ["y"])

    def test_max_titles_still_applies_within_a_season(self):
        self.assertEqual(targets(series="99", season=1, max_titles=2),
                         ["s1e1", "s1e2"])


class TestSeasonReachesTheDashboard(unittest.TestCase):
    def setUp(self):
        self.js = O.dashboard()

    def test_the_season_option_is_offered(self):
        self.assertIn("Season ' + sn + ' only:", self.js)

    def test_specials_are_offerable_in_the_ui_too(self):
        self.assertIn("x.season !== null && x.season !== undefined", self.js)

    def test_a_season_is_only_sent_when_chosen(self):
        self.assertIn("if(season !== undefined && season !== '') "
                      "body.season = Number(season);", self.js)

    def test_the_request_model_carries_it(self):
        self.assertIn("season", O.RunRequest.model_fields)
        self.assertIsNone(O.RunRequest().season)

    def test_a_season_job_is_labelled_apart_from_the_show(self):
        job = O._submit(O.RunRequest(series="99", season=3))
        self.assertTrue(job["target"].endswith("S03"), job["target"])
        whole = O._submit(O.RunRequest(series="99"))
        self.assertEqual(whole["target"], "99")

class FakeLibrary:
    """search() returns EPISODES; a show is only reachable via their seriesId."""
    def __init__(self, results): self.results = results
    def search(self, q): return self.results


def ep(series_id, show):
    return {"ratingKey": "1", "type": "episode", "seriesId": series_id,
            "grandparentTitle": show, "season": 1, "episode": 1}


class TestResolveSeriesByName(unittest.TestCase):
    """The CLI has no search subcommand, so a raw series id would be
    undiscoverable — --series takes the show's name."""

    def test_a_name_resolves_to_the_show_id(self):
        src = FakeLibrary([ep("1234", "Billions"), ep("1234", "Billions")])
        self.assertEqual(cli.resolve_series(src, "billions"), "1234")

    def test_an_id_passes_straight_through(self):
        src = FakeLibrary([ep("1234", "Billions")])
        self.assertEqual(cli.resolve_series(src, "1234"), "1234")

    def test_no_match_says_why_rather_than_running_everything(self):
        with self.assertRaises(SystemExit) as cm:
            cli.resolve_series(FakeLibrary([]), "nope")
        self.assertIn("no show matching", str(cm.exception))

    def test_a_movie_only_match_is_not_a_show(self):
        movies = [{"ratingKey": "288", "type": "movie", "seriesId": None}]
        with self.assertRaises(SystemExit):
            cli.resolve_series(FakeLibrary(movies), "goodfellas")

    def test_an_ambiguous_name_lists_the_candidates(self):
        src = FakeLibrary([ep("1", "The Office (US)"), ep("2", "The Office (UK)")])
        with self.assertRaises(SystemExit) as cm:
            cli.resolve_series(src, "the office")
        msg = str(cm.exception)
        self.assertIn("more than one show", msg)
        self.assertIn("The Office (UK)", msg)
        self.assertIn("The Office (US)", msg)


class TestRunArguments(unittest.TestCase):
    def parse(self, *argv):
        return cli.build_parser().parse_args(["run", *argv])

    def test_series_and_season_are_accepted(self):
        a = self.parse("--series", "Billions", "--season", "1")
        self.assertEqual((a.series, a.season), ("Billions", 1))

    def test_season_defaults_to_none_not_zero(self):
        """Zero would mean Specials, so the default cannot be falsy-equal."""
        self.assertIsNone(self.parse("--series", "Billions").season)

    def test_specials_parse_as_a_season(self):
        self.assertEqual(self.parse("--series", "B", "--season", "0").season, 0)

    def test_a_season_without_a_series_is_refused(self):
        args = self.parse("--season", "1")
        with mock.patch.object(cli, "_store"), \
                mock.patch.object(cli, "_make_source"), \
                mock.patch.object(cli.k, "tmdb_key", return_value="tk"):
            with self.assertRaises(SystemExit) as cm:
                cli.cmd_run(args)
        self.assertIn("--season narrows --series", str(cm.exception))

    def test_an_empty_season_is_a_message_not_a_traceback(self):
        """enumerate_targets raises ValueError for the service; on a terminal
        that is a stack trace for what is really a typo."""
        class Show:
            key_prefix = "plex"
            def search(self, q):
                return [{"seriesId": "1", "grandparentTitle": "B",
                         "type": "episode"}]
            def series_leaves(self, sid):
                return [{"ratingKey": "a", "season": 1, "episode": 1}]
        args = self.parse("--series", "B", "--season", "9")
        with mock.patch.object(cli, "_store"), \
                mock.patch.object(cli, "_make_source", return_value=Show()), \
                mock.patch.object(cli.k, "tmdb_key", return_value="tk"):
            with self.assertRaises(SystemExit) as cm:
                cli.cmd_run(args)
        self.assertIn("season 9", str(cm.exception))


if __name__ == "__main__":
    unittest.main()


class TestSearchSurfacesShows(unittest.TestCase):
    """Plex answers "smallville" with the SHOW, not its episodes.

    Episodes are called "Pilot" and "Metamorphosis", so a show-name query
    matches none of them. Filtering the show out made those searches return
    nothing at all, which reads as "not in the library".
    """

    class Source:
        def search(self, q):
            return [
                {"ratingKey": "900", "type": "show", "title": "Smallville",
                 "year": 2001, "grandparentTitle": None, "season": None,
                 "episode": None, "seriesId": None},
                {"ratingKey": "901", "type": "episode", "title": "Pilot",
                 "year": 2001, "grandparentTitle": "Smallville", "season": 1,
                 "episode": 1, "seriesId": "900"},
                {"ratingKey": "902", "type": "artist", "title": "Smallville",
                 "year": None, "grandparentTitle": None, "season": None,
                 "episode": None, "seriesId": None},
            ]

    def setUp(self):
        self._origin, self._source = O._origin, O._source
        O._origin = lambda: "http://plex.example.com"
        O._source = lambda: self.Source()

    def tearDown(self):
        O._origin, O._source = self._origin, self._source

    def test_the_show_comes_back(self):
        types = [r["type"] for r in O.api_search("smallville")["results"]]
        self.assertIn("show", types)

    def test_a_show_is_its_own_series_target(self):
        """series_leaves takes the show's own key, so seriesId is ratingKey."""
        show = next(r for r in O.api_search("smallville")["results"]
                    if r["type"] == "show")
        self.assertEqual(show["seriesId"], "900")
        self.assertEqual(show["series"], "Smallville")
        self.assertIsNone(show["season"])

    def test_episodes_still_point_at_their_parent(self):
        ep = next(r for r in O.api_search("smallville")["results"]
                  if r["type"] == "episode")
        self.assertEqual(ep["seriesId"], "900")
        self.assertEqual(ep["label"], "Smallville S01E01 · Pilot")

    def test_unplayable_kinds_are_still_dropped(self):
        types = [r["type"] for r in O.api_search("smallville")["results"]]
        self.assertNotIn("artist", types)

    def test_a_show_row_offers_the_series_not_a_rating_key(self):
        """A show has no single file behind it, so no per-item buttons."""
        js = O.dashboard()
        self.assertIn("if(x.type === 'show'){", js)
        self.assertIn(">Seed all</button>", js)
        self.assertIn(">Full index all</button>", js)
        # The show branch returns before `rk` is ever used in markup.
        show_branch = js.split("if(x.type === 'show'){", 1)[1].split(" }", 1)[0]
        self.assertIn("data-act=\"series\"", show_branch)
        self.assertNotIn("data-act=\"queue\"", show_branch)
