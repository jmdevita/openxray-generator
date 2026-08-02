"""Music cues awaiting a name, and what naming one does.

The pass finds WHERE music plays reliably and often fails to say WHAT it is:
the first feature it ran against produced 31 good cues and one identification.
So the interesting behaviour is all in the unnamed path — persisting cues
nobody recognised, letting a person answer, and not deleting the audio they
need to hear while they do it.
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from xray import musiccues as mc, retention  # noqa: E402


def cue(start, end):
    return SimpleNamespace(start=start, end=end, duration=end - start)


class Base(unittest.TestCase):
    def setUp(self):
        self.store = Path(tempfile.mkdtemp())
        self.cid = "tmdb-movie-212778"

    def write(self, cues, matches=None):
        doc = mc.build_cues(content_id=self.cid, cues=cues,
                            matches=matches or [None] * len(cues),
                            generated="2026-08-02T00:00:00Z", version="audd-v1")
        mc.write_cues(self.store, self.cid, doc)
        return doc

    def timeline(self, provenance=None):
        (self.store / f"{self.cid}.json").write_text(json.dumps(
            {"contentId": self.cid, "provenance": provenance or {}}))


class TestCueDocument(Base):
    def test_every_cue_is_kept_not_just_the_recognised_ones(self):
        """The whole point: an unrecognised cue is work for a person, not
        waste to be discarded."""
        doc = self.write([cue(0, 100), cue(200, 260), cue(400, 430)])
        self.assertEqual(len(doc["cues"]), 3)
        self.assertTrue(all(c["matched"] is None for c in doc["cues"]))

    def test_longest_first(self):
        """Nobody works through 31 rows. A four-minute cue is a needle-drop
        somebody chose; a twelve-second one is usually a sting."""
        doc = self.write([cue(0, 20), cue(100, 400), cue(500, 590)])
        self.assertEqual([c["seconds"] for c in doc["cues"]], [300.0, 90.0, 20.0])

    def test_a_lookups_answer_is_recorded_with_its_source(self):
        doc = self.write([cue(0, 100)],
                         [SimpleNamespace(title="Layla", artist="Derek")])
        self.assertEqual(doc["cues"][0]["matched"],
                         {"title": "Layla", "artist": "Derek", "source": "audd"})

    def test_a_match_with_no_title_is_not_a_match(self):
        # CueMatch objects exist for every cue, matched or not.
        doc = self.write([cue(0, 100)], [SimpleNamespace(title=None, artist=None)])
        self.assertIsNone(doc["cues"][0]["matched"])


class TestNaming(Base):
    def test_a_person_outranks_a_lookup(self):
        """Somebody listened to the whole cue; the lookup probed three
        ten-second windows of it."""
        self.write([cue(0, 100)], [SimpleNamespace(title="Wrong", artist="No")])
        mc.name_cue(self.store, self.cid, 0, title="Right", artist="Yes")
        got = mc.intervals(self.store, self.cid)
        self.assertEqual(got[0]["title"], "Right")
        self.assertEqual(got[0]["source"], "manual")

    def test_an_empty_title_clears_a_name(self):
        # The only way back from a typo.
        self.write([cue(0, 100)])
        mc.name_cue(self.store, self.cid, 0, title="Typo")
        self.assertEqual(len(mc.intervals(self.store, self.cid)), 1)
        mc.name_cue(self.store, self.cid, 0, title="")
        self.assertEqual(mc.intervals(self.store, self.cid), [])

    def test_unnamed_counts_what_is_still_owed(self):
        self.write([cue(0, 100), cue(200, 300)],
                   [SimpleNamespace(title="Known", artist=None), None])
        self.assertEqual(mc.unnamed(self.store, self.cid), 1)
        mc.name_cue(self.store, self.cid, 1, title="Also known")
        self.assertEqual(mc.unnamed(self.store, self.cid), 0)


class TestIntervals(Base):
    def test_consecutive_cues_of_one_song_become_one_interval(self):
        """A score under a whole scene is one interval, not nine."""
        self.write([cue(0, 100), cue(100, 200), cue(300, 400)])
        for i in (0, 1):
            mc.name_cue(self.store, self.cid, i, title="Theme")
        mc.name_cue(self.store, self.cid, 2, title="Other")
        got = mc.intervals(self.store, self.cid)
        self.assertEqual(len(got), 2)
        self.assertEqual((got[0]["startMs"], got[0]["endMs"]), (0, 200_000))

    def test_merging_ignores_case_and_punctuation(self):
        self.write([cue(0, 100), cue(100, 200)])
        mc.name_cue(self.store, self.cid, 0, title="Ain't Nobody's Business")
        mc.name_cue(self.store, self.cid, 1, title="aint nobodys business")
        self.assertEqual(len(mc.intervals(self.store, self.cid)), 1)

    def test_intervals_come_out_in_time_order(self):
        """The document is sorted longest-first for the screen; a timeline is
        read against a clock."""
        self.write([cue(0, 20), cue(100, 400)])
        for i in (0, 1):
            mc.name_cue(self.store, self.cid, i, title=f"Song {i}")
        got = mc.intervals(self.store, self.cid)
        self.assertEqual([g["startMs"] for g in got], [0, 100_000])

    def test_unnamed_cues_are_simply_absent(self):
        self.write([cue(0, 100), cue(200, 300)])
        mc.name_cue(self.store, self.cid, 0, title="Only this one")
        self.assertEqual(len(mc.intervals(self.store, self.cid)), 1)

    def test_no_cue_document_means_no_intervals(self):
        self.assertEqual(mc.intervals(self.store, "nothing-here"), [])


class TestRetentionInteraction(Base):
    """The rule that would have deleted the previews out from under the
    labelling screen."""

    def setUp(self):
        super().setUp()
        d = self.store / "music_work" / self.cid
        d.mkdir(parents=True)
        (d / f"{self.cid}__audio.mp3").write_bytes(b"x" * 500)

    def group(self):
        for g in retention.survey(self.store)["kinds"]:
            if g["kind"] == "music":
                return g
        return None

    def test_held_before_the_pass_runs(self):
        self.timeline()
        self.assertEqual(self.group()["items"][0]["holding"],
                         "the music pass has not run yet")

    def test_still_held_while_cues_await_a_name(self):
        # The bug this test exists for: stamping provenance used to release
        # the audio, which is exactly the file the cue previews are cut from.
        self.timeline(provenance={"music": {}})
        self.write([cue(0, 100), cue(200, 300)])
        self.assertEqual(self.group()["reclaimable"], 0)
        self.assertEqual(self.group()["items"][0]["holding"],
                         "2 music cues still to name")

    def test_released_once_every_cue_is_named(self):
        self.timeline(provenance={"music": {}})
        self.write([cue(0, 100)])
        mc.name_cue(self.store, self.cid, 0, title="Done")
        self.assertEqual(self.group()["reclaimable"], 500)

    def test_singular_reads_correctly(self):
        self.timeline(provenance={"music": {}})
        self.write([cue(0, 100)])
        self.assertEqual(self.group()["items"][0]["holding"],
                         "1 music cue still to name")

    def test_a_stamped_title_with_no_cue_document_is_released(self):
        """A pass that predates cue persistence. Nothing can be named, so
        nothing is waiting, and holding the audio forever would be a leak."""
        self.timeline(provenance={"music": {}})
        self.assertEqual(self.group()["reclaimable"], 500)


if __name__ == "__main__":
    unittest.main()
