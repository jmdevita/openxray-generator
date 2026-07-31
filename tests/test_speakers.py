"""The speakers pass and the voiceprint store.

The load-bearing property is what this pass DOESN'T do: it must never write
actorIntervals. Diarization produces anonymous speakers, and a pass that
guessed at names would put wrong characters on screen with the same confidence
as right ones. Naming needs a human, so the pass stops short on purpose.

The other half is the two audio floors, which are not symmetric and were
measured that way: a short speaker is fine as a REFERENCE and dangerous as a
PROBE.
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import tempfile  # noqa: E402

from xray import voiceprints as vp  # noqa: E402
from xray.passes import speakers  # noqa: E402


TURNS = [[0.0, 90.0, "SPEAKER_00"],     # 90s  -> enrollable AND matchable
         [95.0, 135.0, "SPEAKER_01"],   # 40s  -> enrollable, NOT matchable
         [140.0, 150.0, "SPEAKER_02"]]  # 10s  -> neither
LABELS = ["SPEAKER_00", "SPEAKER_01", "SPEAKER_02"]
EMB = [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], None]


class TestTwoFloors(unittest.TestCase):
    """One symmetric floor was doing a necessary job and an unnecessary one.
    Measured: reference>=2min + unfiltered probe let a 0.754 false positive
    through; probe>=2min + unfiltered reference topped out at 0.512."""

    def _clusters(self, d):
        vp.write_clusters(Path(d), "tmdb-movie-1", turns=TURNS, labels=LABELS,
                          embeddings=EMB, generated="2026-07-29T00:00:00Z",
                          version="test")
        return {s["speaker"]: s for s in
                vp.read_clusters(Path(d), "tmdb-movie-1")["speakers"]}

    def test_the_floors_are_not_the_same_number(self):
        self.assertLess(vp.ENROLL_MIN_S, vp.MATCH_MIN_S)

    def test_a_mid_length_speaker_may_enrol_but_not_be_matched(self):
        with tempfile.TemporaryDirectory() as d:
            s = self._clusters(d)["SPEAKER_01"]        # 40 seconds
            self.assertTrue(s["enrollable"], "40s is a usable reference")
            self.assertFalse(s["matchable"], "40s invents similarity as a probe")

    def test_a_long_speaker_qualifies_for_both(self):
        with tempfile.TemporaryDirectory() as d:
            s = self._clusters(d)["SPEAKER_00"]
            self.assertTrue(s["enrollable"] and s["matchable"])

    def test_a_very_short_speaker_qualifies_for_neither(self):
        with tempfile.TemporaryDirectory() as d:
            s = self._clusters(d)["SPEAKER_02"]
            self.assertFalse(s["enrollable"] or s["matchable"])


class TestClusterStore(unittest.TestCase):
    def test_null_embeddings_keep_their_index(self):
        """pyannote returns NaN for a speaker with too little audio. Dropping
        those rows would silently misalign embeddings against labels."""
        with tempfile.TemporaryDirectory() as d:
            vp.write_clusters(Path(d), "x", turns=TURNS, labels=LABELS,
                              embeddings=EMB, generated="g", version="v")
            spk = vp.read_clusters(Path(d), "x")["speakers"]
            self.assertEqual(len(spk), 3)
            self.assertIsNone(spk[2]["embedding"])
            self.assertEqual(spk[0]["speaker"], "SPEAKER_00")

    def test_missing_clusters_read_as_none_not_an_error(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(vp.read_clusters(Path(d), "nope"))


class TestSuggest(unittest.TestCase):
    def _enrolled(self, d):
        vp.enroll(Path(d), "tmdb:1", actor_id="tmdb:1", character="Shrek",
                  embedding=[1.0, 0.0, 0.0], content_id="tmdb-movie-1")

    def test_an_identical_voice_is_suggested(self):
        with tempfile.TemporaryDirectory() as d:
            self._enrolled(d)
            got = vp.suggest(Path(d), [1.0, 0.0, 0.0])
            self.assertEqual(got["character"], "Shrek")
            self.assertGreaterEqual(got["sim"], vp.MATCH_THRESHOLD)

    def test_an_unrelated_voice_is_not(self):
        with tempfile.TemporaryDirectory() as d:
            self._enrolled(d)
            self.assertIsNone(vp.suggest(Path(d), [0.0, 1.0, 0.0]))

    def test_a_speaker_is_never_suggested_against_its_own_title(self):
        with tempfile.TemporaryDirectory() as d:
            self._enrolled(d)
            self.assertIsNone(vp.suggest(Path(d), [1.0, 0.0, 0.0],
                                         exclude_content="tmdb-movie-1"))

    def test_a_null_embedding_suggests_nothing_rather_than_crashing(self):
        with tempfile.TemporaryDirectory() as d:
            self._enrolled(d)
            self.assertIsNone(vp.suggest(Path(d), None))

    def test_enrolling_a_null_embedding_is_a_no_op(self):
        with tempfile.TemporaryDirectory() as d:
            vp.enroll(Path(d), "k", actor_id="a", character="c",
                      embedding=None, content_id="x")
            self.assertEqual(vp.read_prints(Path(d)), {})


class TestPassStopsBeforeNaming(unittest.TestCase):
    ITEM = {"type": "movie", "title": "Shrek 2", "ratingKey": "1",
            "tmdbId": 809, "showTmdbId": None, "season": None, "episode": None,
            "durationMs": 5600000, "downloadUrl": "http://x/media.mkv"}

    def _run(self, tmp):
        bundle = {"cast": [{"actorId": "tmdb:1", "name": "Mike Myers",
                            "character": "Shrek", "thumb": None}],
                  "animated": True,
                  "labels": {"title": "Shrek 2", "year": 2004, "series": None}}
        src = mock.Mock(key_prefix="plex")
        with mock.patch.object(speakers, "resolve", return_value=self.ITEM), \
             mock.patch.object(speakers.refsmod, "movie_bundle",
                               return_value=bundle), \
             mock.patch.object(speakers, "extract_audio") as ex, \
             mock.patch.object(speakers.engines, "speaker_transport") as tr:
            ex.side_effect = lambda url, out, **kw: (
                Path(out).parent.mkdir(parents=True, exist_ok=True)
                or Path(out).write_bytes(b"x") or Path(out))
            tr.return_value.ready.return_value = (True, "")
            tr.return_value.diarize.return_value = {
                "turns": TURNS, "labels": LABELS, "embeddings": EMB}
            return speakers.run(tmp, tmp / "work", source=src, tmdb_key="k")

    def test_it_writes_no_intervals(self):
        """The whole point. A pass that guessed names would put wrong
        characters on screen as confidently as right ones."""
        import json
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            out = self._run(tmp)
            doc = json.loads(
                (tmp / f"{out['contentId']}.json").read_text())
            self.assertEqual(doc["actorIntervals"], [])
            self.assertTrue(doc["cast"], "cast is still written")

    def test_it_reports_that_a_human_is_needed(self):
        with tempfile.TemporaryDirectory() as d:
            out = self._run(Path(d))
            self.assertTrue(out["needsLabelling"])
            self.assertEqual(out["speakers"], 3)
            self.assertEqual(out["nameable"], 2)   # the 10s one is excluded

    def test_clusters_are_stored_for_the_labelling_screen(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            out = self._run(tmp)
            cl = vp.read_clusters(tmp, out["contentId"])
            self.assertEqual(len(cl["speakers"]), 3)
            self.assertEqual(len(cl["turns"]), 3)

    def test_the_timeline_is_stamped_so_the_dashboard_can_find_it(self):
        import json
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            out = self._run(tmp)
            doc = json.loads((tmp / f"{out['contentId']}.json").read_text())
            self.assertIn("speakers", doc["provenance"])


class TestEngineIsCheckedBeforeTheAudioPull(unittest.TestCase):
    """Discovering a missing container after streaming a feature wastes
    exactly the expensive part."""

    def _run_without(self, transport):
        src = mock.Mock(key_prefix="plex")
        with mock.patch.object(speakers, "resolve",
                               return_value=TestPassStopsBeforeNaming.ITEM), \
             mock.patch.object(speakers, "extract_audio") as ex, \
             mock.patch.object(speakers.engines, "speaker_transport",
                               return_value=transport):
            with self.assertRaises(speakers.Unsupported):
                speakers.run(Path("/tmp/x"), Path("/tmp/x"), source=src,
                             tmdb_key="k")
            return ex

    def test_no_engine_means_no_download(self):
        ex = self._run_without(None)
        ex.assert_not_called()

    def test_an_unready_engine_means_no_download(self):
        t = mock.Mock()
        t.ready.return_value = (False, "engine-speakers has no model weights")
        ex = self._run_without(t)
        ex.assert_not_called()


if __name__ == "__main__":
    unittest.main()


class TestDashboardSurfacesTheOwedWork(unittest.TestCase):
    """A diarized title is the only state in the system that means "your
    turn". Every other block's presence means finished."""

    def setUp(self):
        from xray.service import orchestrator as O
        self.html = O.dashboard()

    def test_speakers_is_offered_as_a_pass(self):
        self.assertIn("chip('speakers', 'speakers'", self.html)

    def test_a_diarized_title_says_what_is_still_owed(self):
        """State chip + action button: the fact lives with the chips, the
        click lives with the actions. Progress leads with dialogue COVERAGE
        (pct), not speaker count — five of 24 names covered 47% of the first
        real title, and counting speakers understates that."""
        self.assertIn("function speakerNote(", self.html)
        self.assertIn("function speakerAction(", self.html)
        self.assertIn("% named", self.html)
        self.assertIn('data-act="label"', self.html,
                      "the button must open the labelling screen")

    def test_the_action_disappears_when_naming_is_done(self):
        """A done title keeps its state chip but offers no button — the acts
        column only ever shows things there are to do."""
        import re
        fn = self.html[self.html.index("function speakerAction("):]
        fn = fn[:fn.index("\n}")]
        self.assertIn("named >= s.nameable", fn)
        self.assertIn("return ''", fn)

    def test_owed_and_done_are_visually_distinct(self):
        """An owed row that looked like a finished one would be scrolled past.

        The class names are DISCOVERED from speakerNote rather than written in
        here. This test protects the distinction, not a spelling -- and the
        hard-coded version of it broke on a rename that fixed a real bug,
        which is the wrong way round for a test to earn its keep.
        """
        import re
        fn = self.html[self.html.index("function speakerNote("):]
        fn = fn[:fn.index("\n}")]
        classes = re.findall(r'class="([\w-]+ (?:owed|ok))"', fn)
        self.assertEqual(len(classes), 2,
                         f"expected an owed and a done class, found {classes}")
        colours = set()
        for cls in classes:
            sel = "." + cls.replace(" ", ".")
            rule = re.search(re.escape(sel) + r"\{([^}]*)\}", self.html)
            self.assertIsNotNone(rule, f"{sel} is emitted but never styled")
            colour = re.search(r"color:([^;}]+)", rule.group(1))
            self.assertIsNotNone(colour, f"{sel} sets no colour")
            colours.add(colour.group(1))
        self.assertEqual(len(colours), 2,
                         f"owed and done must not look alike: {colours}")

    def test_it_never_runs_unasked(self):
        self.assertIn("const OPT_IN = ['speakers']", self.html)


class TestSpeakerStateReadsTheTimeline(unittest.TestCase):
    def test_no_speakers_block_means_no_state(self):
        from xray.service import orchestrator as O
        self.assertIsNone(O._speaker_state({"contentId": "x",
                                            "provenance": {}}))

    def test_named_counts_voice_intervals_not_a_flag(self):
        """The timeline is what gets shared, so what it actually carries is
        the truthful answer to 'has anyone been named'."""
        from xray.service import orchestrator as O
        with tempfile.TemporaryDirectory() as d:
            O.STORE = Path(d)
            vp.write_clusters(Path(d), "tmdb-movie-1", turns=TURNS,
                              labels=LABELS, embeddings=EMB,
                              generated="g", version="v")
            doc = {"contentId": "tmdb-movie-1",
                   "provenance": {"speakers": {"generated": "g"}},
                   "actorIntervals": [
                       {"actorId": "tmdb:1", "source": "voice"},
                       {"actorId": "tmdb:1", "source": "voice"},
                       {"actorId": "tmdb:9", "source": "face"}]}
            st = O._speaker_state(doc)
            self.assertEqual(st["found"], 3)
            self.assertEqual(st["nameable"], 2)
            self.assertEqual(st["named"], 1, "one distinct voice actor, "
                                             "face intervals ignored")


class TestNamingRoundTrip(unittest.TestCase):
    """Name a speaker, and the timeline gains its intervals. Clear it, and
    they go away again -- which is why intervals are REBUILT from the names
    file rather than patched in place."""

    def setUp(self):
        import json
        from xray.service import orchestrator as O
        self.O = O
        self.d = tempfile.TemporaryDirectory()
        self.store = Path(self.d.name)
        O.STORE = self.store
        vp.write_clusters(self.store, "tmdb-movie-1", turns=TURNS,
                          labels=LABELS, embeddings=EMB, generated="g",
                          version="v")
        doc = {"contentId": "tmdb-movie-1", "version": 1, "generated": "g",
               "sourceRuntimeMs": 200000,
               "cast": [{"actorId": "tmdb:1", "name": "Mike Myers",
                         "character": "Shrek"}],
               "actorIntervals": [], "musicIntervals": [], "trivia": [],
               "provenance": {"speakers": {"generated": "g", "version": "v"}}}
        (self.store / "tmdb-movie-1.json").write_text(json.dumps(doc))

    def tearDown(self):
        self.d.cleanup()

    def _doc(self):
        import json
        return json.loads((self.store / "tmdb-movie-1.json").read_text())

    def _name(self, speaker, actor="tmdb:1", char="Shrek", sim=None):
        return self.O.api_name_speaker(
            "tmdb-movie-1",
            self.O.NameRequest(speaker=speaker, actor_id=actor,
                               character=char, sim=sim))

    def test_naming_writes_intervals(self):
        out = self._name("SPEAKER_00")
        self.assertGreater(out["intervals"], 0)
        ivs = self._doc()["actorIntervals"]
        self.assertTrue(ivs)
        self.assertEqual(ivs[0]["source"], "voice")
        self.assertEqual(ivs[0]["actorId"], "tmdb:1")

    def test_clearing_a_name_removes_its_intervals(self):
        self._name("SPEAKER_00")
        self.assertTrue(self._doc()["actorIntervals"])
        self.O.api_name_speaker(
            "tmdb-movie-1", self.O.NameRequest(speaker="SPEAKER_00"))
        self.assertEqual(self._doc()["actorIntervals"], [])

    def test_confidence_by_ear_is_1_and_a_suggestion_keeps_its_cosine(self):
        """Same meaning as faces: strength of the match to the claimed
        identity. A person listening outranks any cosine."""
        self._name("SPEAKER_00")
        self.assertEqual(self._doc()["actorIntervals"][0]["confidence"], 1.0)
        self._name("SPEAKER_00", sim=0.867)
        self.assertEqual(self._doc()["actorIntervals"][0]["confidence"], 0.867)

    def test_face_intervals_survive_a_naming(self):
        import json
        doc = self._doc()
        doc["actorIntervals"] = [{"actorId": "tmdb:9", "startMs": 0,
                                  "endMs": 10, "confidence": 0.7}]
        (self.store / "tmdb-movie-1.json").write_text(json.dumps(doc))
        self._name("SPEAKER_00")
        srcs = {(iv.get("source") or "face")
                for iv in self._doc()["actorIntervals"]}
        self.assertEqual(srcs, {"face", "voice"})

    def test_naming_enrols_a_voiceprint_for_other_titles(self):
        self._name("SPEAKER_00")
        self.assertIn("tmdb:1", vp.read_prints(self.store))

    def test_a_short_speaker_is_named_but_never_enrolled(self):
        """SPEAKER_02 has 10s: usable as a label, useless as a reference that
        would then be reused against every other title."""
        self._name("SPEAKER_02", actor="tmdb:2", char="Extra")
        self.assertNotIn("tmdb:2", vp.read_prints(self.store))

    def test_the_screen_ranks_by_dialogue_and_reports_the_floors(self):
        got = self.O.api_speakers("tmdb-movie-1")
        secs = [r["seconds"] for r in got["rows"]]
        self.assertEqual(secs, sorted(secs, reverse=True))
        self.assertEqual(got["enrollMin"], vp.ENROLL_MIN_S)
        self.assertTrue(got["rows"][0]["spans"], "the strip has blocks")

    def test_an_unknown_title_404s_rather_than_showing_an_empty_screen(self):
        from fastapi import HTTPException
        with self.assertRaises(HTTPException):
            self.O.api_speakers("tmdb-movie-999")
