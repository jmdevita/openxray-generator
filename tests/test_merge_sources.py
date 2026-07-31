"""actorIntervals from different passes must not delete each other.

The original rule was "if the new doc has no faces stamp, restore the old
intervals". That is correct for a level-0 seed and wrong for every other
pass: a voice pass also writes no faces stamp, so its intervals were swapped
for the old face ones WHILE ITS OWN PROVENANCE BLOCK SURVIVED — a file
claiming a voice pass ran while carrying none of its output. Provenance that
lies is worse than data that is missing.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from xray.passes.index_title import merge_preserved  # noqa: E402


def doc(prov=None, intervals=(), **extra):
    return {"provenance": dict(prov or {}), "actorIntervals": list(intervals),
            "cast": [], "musicIntervals": [], "trivia": [], **extra}


FACE_IV = {"actorId": "tmdb:1", "startMs": 0, "endMs": 1000, "confidence": 0.9}
VOICE_IV = {"actorId": "tmdb:12073", "roleId": "tmdb:12073:shrek",
            "startMs": 5000, "endMs": 6000, "confidence": 1.0,
            "source": "voice"}
FACES_STAMP = {"faces": {"generated": "2026-07-01T00:00:00Z",
                         "version": "sface-v1"}}
VOICE_STAMP = {"voice": {"generated": "2026-07-29T00:00:00Z",
                         "version": "pyannote-3.1"}}


class SeedNeverOverwrites(unittest.TestCase):
    """The behaviour the original rule existed to protect. Unchanged."""

    def test_level0_seed_does_not_wipe_a_face_index(self):
        out = merge_preserved(doc(FACES_STAMP, [FACE_IV]), doc())
        self.assertEqual(out["actorIntervals"], [FACE_IV])
        self.assertIn("faces", out["provenance"])

    def test_level0_seed_does_not_wipe_a_voice_index(self):
        out = merge_preserved(doc(VOICE_STAMP, [VOICE_IV]), doc())
        self.assertEqual(out["actorIntervals"], [VOICE_IV])
        self.assertIn("voice", out["provenance"])

    def test_seed_over_an_empty_store_stays_empty(self):
        out = merge_preserved(doc(), doc())
        self.assertEqual(out["actorIntervals"], [])


class PassesReplaceOnlyTheirOwnSource(unittest.TestCase):
    def test_voice_pass_keeps_existing_face_intervals(self):
        out = merge_preserved(doc(FACES_STAMP, [FACE_IV]),
                              doc(VOICE_STAMP, [VOICE_IV]))
        self.assertIn(FACE_IV, out["actorIntervals"])
        self.assertIn(VOICE_IV, out["actorIntervals"])
        self.assertEqual(len(out["actorIntervals"]), 2)

    def test_voice_pass_output_is_not_discarded(self):
        """The regression. Previously the voice intervals vanished."""
        out = merge_preserved(doc(FACES_STAMP, [FACE_IV]),
                              doc(VOICE_STAMP, [VOICE_IV]))
        voice = [iv for iv in out["actorIntervals"]
                 if iv.get("source") == "voice"]
        self.assertEqual(voice, [VOICE_IV])

    def test_provenance_never_claims_a_pass_whose_output_is_gone(self):
        """A file saying 'voice ran' with no voice intervals is a lie."""
        out = merge_preserved(doc(FACES_STAMP, [FACE_IV]),
                              doc(VOICE_STAMP, [VOICE_IV]))
        for block, source in (("voice", "voice"), ("faces", "face")):
            if block in out["provenance"]:
                got = [iv for iv in out["actorIntervals"]
                       if (iv.get("source") or "face") == source]
                self.assertTrue(got, f"{block} stamped but no {source} intervals")

    def test_face_pass_keeps_existing_voice_intervals(self):
        out = merge_preserved(doc(VOICE_STAMP, [VOICE_IV]),
                              doc(FACES_STAMP, [FACE_IV]))
        self.assertIn(VOICE_IV, out["actorIntervals"])
        self.assertIn(FACE_IV, out["actorIntervals"])

    def test_a_reindex_replaces_its_own_source_rather_than_doubling_it(self):
        older = {**FACE_IV, "endMs": 999}
        out = merge_preserved(doc(FACES_STAMP, [older]),
                              doc(FACES_STAMP, [FACE_IV]))
        self.assertEqual(out["actorIntervals"], [FACE_IV])

    def test_legacy_intervals_without_source_count_as_face(self):
        """Intervals predating the field are all face-derived, so a face
        re-index must replace them rather than accumulate duplicates."""
        legacy = {"actorId": "tmdb:9", "startMs": 10, "endMs": 20,
                  "confidence": 0.5}          # no `source` key
        out = merge_preserved(doc(FACES_STAMP, [legacy]),
                              doc(FACES_STAMP, [FACE_IV]))
        self.assertEqual(out["actorIntervals"], [FACE_IV])

    def test_merged_intervals_stay_sorted(self):
        early = {**VOICE_IV, "startMs": 1, "endMs": 2}
        out = merge_preserved(doc(FACES_STAMP, [FACE_IV]),
                              doc(VOICE_STAMP, [early]))
        starts = [iv["startMs"] for iv in out["actorIntervals"]]
        self.assertEqual(starts, sorted(starts))


class OtherBlocksAreUntouched(unittest.TestCase):
    def test_music_and_trivia_still_survive(self):
        old = doc(FACES_STAMP, [FACE_IV])
        old["musicIntervals"] = [{"title": "Holding Out for a Hero",
                                  "startMs": 1, "endMs": 2}]
        old["trivia"] = [{"text": "fact"}]
        old["provenance"]["music"] = {"generated": "x", "version": "y"}
        out = merge_preserved(old, doc(VOICE_STAMP, [VOICE_IV]))
        self.assertTrue(out["musicIntervals"])
        self.assertTrue(out["trivia"])
        self.assertIn("music", out["provenance"])


if __name__ == "__main__":
    unittest.main()
