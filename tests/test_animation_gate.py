"""Animated titles are refused before the media is pulled, not after.

The face stack is YuNet + SFace, and both need five-point human landmarks.
Most animated principals do not have a human face at all (Donkey, Puss,
Gingy), so an animated title cannot produce actorIntervals — see
docs/ANIMATION.md.

What matters here is the ORDER: refusing after a 93-minute extraction wastes
the expensive part and still hands back an empty timeline. So the gate has to
fire before the engine is ever touched, and it must say why.
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from xray import refs as refsmod  # noqa: E402
from xray.passes import index_title  # noqa: E402


class TestGenreDetection(unittest.TestCase):
    def test_animation_genre_is_recognised(self):
        self.assertTrue(refsmod._is_animated(
            {"genres": [{"id": 16, "name": "Animation"},
                        {"id": 35, "name": "Comedy"}]}))

    def test_live_action_is_not(self):
        self.assertFalse(refsmod._is_animated(
            {"genres": [{"id": 18, "name": "Drama"},
                        {"id": 80, "name": "Crime"}]}))

    def test_matched_on_id_not_name(self):
        """`name` is localised; only the numeric id is stable."""
        self.assertTrue(refsmod._is_animated(
            {"genres": [{"id": 16, "name": "Animación"}]}))
        self.assertFalse(refsmod._is_animated(
            {"genres": [{"id": 99, "name": "Animation"}]}))

    def test_missing_and_malformed_genre_data_is_not_animated(self):
        """Absent genre data must not be read as a positive: guessing wrong
        here silently skips a live-action title's whole face pass."""
        for payload in ({}, {"genres": None}, {"genres": []},
                        {"genres": ["Animation"]}, {"genres": [None]},
                        {"genres": [{"name": "Animation"}]}):
            self.assertFalse(refsmod._is_animated(payload), payload)


class TestBundlesCarryTheFlag(unittest.TestCase):
    """Free: `genres` already rides in the details response the cast call
    makes, so this must cost no extra request."""

    def _resp(self, payload):
        r = mock.Mock()
        r.ok = True
        r.json.return_value = payload
        r.raise_for_status.return_value = None
        return r

    def test_movie_bundle_reports_animated_without_extra_calls(self):
        payload = {"title": "Shrek 2", "release_date": "2004-05-19",
                   "genres": [{"id": 16, "name": "Animation"}],
                   "credits": {"cast": []}}
        with mock.patch.object(refsmod.requests, "get",
                               return_value=self._resp(payload)) as g:
            out = refsmod.movie_bundle(809, "key", max_images=0)
        self.assertTrue(out["animated"])
        self.assertEqual(g.call_count, 1)

    def test_movie_bundle_live_action(self):
        payload = {"title": "Goodfellas", "release_date": "1990-09-19",
                   "genres": [{"id": 18, "name": "Drama"}],
                   "credits": {"cast": []}}
        with mock.patch.object(refsmod.requests, "get",
                               return_value=self._resp(payload)):
            out = refsmod.movie_bundle(769, "key", max_images=0)
        self.assertFalse(out["animated"])

    def test_episode_bundle_takes_it_from_the_series(self):
        """An episode has no genres of its own; the show carries them."""
        payload = {"name": "The Simpsons",
                   "genres": [{"id": 16, "name": "Animation"}],
                   "aggregate_credits": {"cast": []}}
        with mock.patch.object(refsmod.requests, "get",
                               return_value=self._resp(payload)):
            out = refsmod.episode_bundle(456, "key", season=None, episode=None,
                                         max_images=0)
        self.assertTrue(out["animated"])


class TestGateFiresBeforeExtraction(unittest.TestCase):
    ITEM = {"type": "movie", "title": "Shrek 2", "ratingKey": "1",
            "tmdbId": 809, "showTmdbId": None, "season": None,
            "episode": None, "durationMs": 5600000}

    def _run(self, animated):
        bundle = {"cast": [], "animated": animated,
                  "labels": {"title": "Shrek 2", "year": 2004, "series": None}}
        with mock.patch.object(index_title, "resolve", return_value=self.ITEM), \
             mock.patch.object(index_title.refsmod, "movie_bundle",
                               return_value=bundle), \
             mock.patch.object(index_title, "engines") as eng:
            # Stop the run immediately AFTER the gate, so a title that gets
            # past it exits on a recognisable sentinel rather than wandering
            # into ffmpeg. Lets one helper serve both branches.
            eng.face_transport.return_value.ready.return_value = (
                False, "STOPPED-PAST-GATE")
            try:
                index_title.run(Path("/tmp/store"), Path("/tmp/work"),
                                source=mock.Mock(key_prefix="plex"),
                                tmdb_key="k")
                return None, eng
            except (SystemExit, index_title.Unsupported) as e:
                return str(e), eng

    def test_animated_title_is_refused(self):
        msg, _ = self._run(True)
        self.assertIsNotNone(msg, "an animated title must not be indexed")
        self.assertIn("animated", msg.lower())

    def test_it_raises_Unsupported_not_a_generic_error(self):
        """The TYPE is what tells the runner this is a limitation rather than
        a crash. Any other exception would be recorded as `failed:`."""
        bundle = {"cast": [], "animated": True,
                  "labels": {"title": "Shrek 2", "year": 2004, "series": None}}
        with mock.patch.object(index_title, "resolve", return_value=self.ITEM), \
             mock.patch.object(index_title.refsmod, "movie_bundle",
                               return_value=bundle), \
             mock.patch.object(index_title, "engines"):
            with self.assertRaises(index_title.Unsupported):
                index_title.run(Path("/tmp/store"), Path("/tmp/work"),
                                source=mock.Mock(key_prefix="plex"),
                                tmdb_key="k")

    def test_the_engine_is_never_touched(self):
        """The whole point: stop before the expensive media pull."""
        _, eng = self._run(True)
        eng.face_transport.assert_not_called()
        eng.face_engine.assert_not_called()

    def test_the_message_explains_and_offers_a_way_forward(self):
        msg, _ = self._run(True)
        self.assertIn("landmark", msg.lower())   # why, not just that
        self.assertIn("speakers", msg.lower())   # what to run instead

    def test_it_does_not_still_recommend_the_cast_only_pass(self):
        """It said "use level 0" before the speakers pass existed. That was
        honest then and is misleading now: level 0 gives a cast panel with no
        intervals, where speakers gives who-talks-when."""
        msg, _ = self._run(True)
        self.assertNotIn("level 0", msg.lower())

    def test_live_action_still_reaches_the_engine(self):
        """The gate must not swallow ordinary titles."""
        msg, eng = self._run(False)
        eng.face_transport.assert_called()
        self.assertIn("STOPPED-PAST-GATE", msg)

    def test_a_bundle_without_the_key_is_not_treated_as_animated(self):
        """Older callers / fakes omit it; absent must mean live action."""
        bundle = {"cast": [],
                  "labels": {"title": "X", "year": 2000, "series": None}}
        with mock.patch.object(index_title, "resolve", return_value=self.ITEM), \
             mock.patch.object(index_title.refsmod, "movie_bundle",
                               return_value=bundle), \
             mock.patch.object(index_title, "engines") as eng:
            eng.face_transport.return_value.ready.return_value = (
                False, "STOPPED-PAST-GATE")
            with self.assertRaises(SystemExit) as cm:
                index_title.run(Path("/tmp/store"), Path("/tmp/work"),
                                source=mock.Mock(key_prefix="plex"),
                                tmdb_key="k")
            self.assertIn("STOPPED-PAST-GATE", str(cm.exception))
            eng.face_transport.assert_called()


if __name__ == "__main__":
    unittest.main()


class TestSurfacedAsSkipNotFailure(unittest.TestCase):
    """A library with cartoons in it must not read as a broken batch."""

    def test_pipeline_records_skipped_not_failed(self):
        from xray import pipeline
        from xray.passes import index_title as it

        result = {"steps": {}}
        # Mirror of pipeline.step's except-ladder ordering. The real one is a
        # closure over run_title's locals, so this asserts the classification,
        # which is what the dashboard keys on.
        try:
            raise it.Unsupported("animated title — face recognition needs …")
        except pipeline.Cancelled:
            result["steps"]["index"] = "cancelled"
        except it.Unsupported as e:
            result["steps"]["index"] = f"skipped: {e}"
        except (SystemExit, Exception) as e:
            result["steps"]["index"] = f"failed: {e}"

        self.assertTrue(result["steps"]["index"].startswith("skipped: "))
        self.assertFalse(result["steps"]["index"].startswith("failed"))

    def test_unsupported_is_not_confused_with_the_skip_flag(self):
        """`skipped(flag)` is the user's own --skip and predates this; the
        dashboard filters on 'skipped: ' with the colon for that reason."""
        self.assertFalse("skipped(flag)".startswith("skipped: "))

    def test_dashboard_styles_skips_apart_from_failures(self):
        from xray.service import orchestrator as O
        page = O.dashboard()
        self.assertIn("q.skip", page)          # its own style, not .bad
        self.assertIn("skipped: ", page)       # filtered with the colon

    def test_dashboard_js_still_parses(self):
        """A stray quote in a Python triple-quoted block would break the whole
        dashboard script, and only a parser catches it."""
        import shutil
        import subprocess
        import tempfile
        from pathlib import Path as P
        from xray.service import orchestrator as O
        node = shutil.which("node")
        if not node:
            self.skipTest("node not available")
        page = O.dashboard()
        body = page.split("<script>", 1)[1].rsplit("</script>", 1)[0]
        with tempfile.TemporaryDirectory() as d:
            f = P(d) / "dash.js"
            f.write_text(body)
            cp = subprocess.run([node, "--check", str(f)],
                                capture_output=True, text=True)
        self.assertEqual(cp.returncode, 0, cp.stderr[-800:])
