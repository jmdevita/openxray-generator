"""Face cluster store: the floor, the scene accounting, and the shared
prints half (stdlib unittest, no network, no models).

The numbers under test were measured on the Billions pilot -- see
faceprints.py for what each one came from -- so these tests pin the
BEHAVIOUR the measurement bought, not arbitrary constants.
"""
import tempfile
import unittest
from pathlib import Path

from xray import faceprints as fp, prints, voiceprints as vp
from xray.faces.cluster import FaceHit


def hits(*ms, frame=0, bbox=(1, 2, 3, 4)):
    return [FaceHit(frame + i, t, bbox) for i, t in enumerate(ms)]


class SceneAccounting(unittest.TestCase):
    def test_samples_within_the_gap_are_one_appearance(self):
        self.assertEqual(fp.scene_count([0, 2000, 4000]), 1)

    def test_a_long_gap_starts_another(self):
        self.assertEqual(fp.scene_count([0, 2000, 60_000, 62_000]), 2)

    def test_empty(self):
        self.assertEqual(fp.scene_count([]), 0)

    def test_spans_are_runtime_fractions(self):
        got = fp.spans([0, 2000, 60_000, 62_000], runtime_ms=100_000)
        self.assertEqual(got, [(0.0, 0.02), (0.6, 0.02)])


class Floor(unittest.TestCase):
    """20s AND 2 scenes. A lamp appeared six times in one shot on the pilot;
    a child actor with 30s across seven scenes was a real, nameable role."""

    def _doc(self, times, fps=0.5, matched=None):
        return fp.build_clusters(
            content_id="tmdb-tv-1-s01e01",
            labels=[0] * len(times), hits=hits(*times),
            centroids={0: [1.0, 0.0]}, matched=matched or {},
            runtime_ms=3_600_000, sample_fps=fps,
            generated="now", version="test")

    def test_long_and_recurring_is_nameable(self):
        # 15 samples at 0.5fps = 30s, spread over three scenes
        times = [0, 2000, 4000, 6000, 8000, 100_000, 102_000, 104_000,
                 106_000, 108_000, 200_000, 202_000, 204_000, 206_000,
                 208_000]
        row = self._doc(times)["clusters"][0]
        self.assertEqual(row["screenSeconds"], 30.0)
        self.assertEqual(row["scenes"], 3)
        self.assertTrue(row["nameable"])

    def test_one_long_shot_is_not(self):
        # 20 samples = 40s of screen time, but all in a single appearance
        times = [i * 2000 for i in range(20)]
        row = self._doc(times)["clusters"][0]
        self.assertGreaterEqual(row["screenSeconds"], fp.MIN_SCREEN_S)
        self.assertEqual(row["scenes"], 1)
        self.assertFalse(row["nameable"])

    def test_brief_but_recurring_is_not(self):
        times = [0, 2000, 100_000, 102_000]      # 8s over two scenes
        row = self._doc(times)["clusters"][0]
        self.assertFalse(row["nameable"])

    def test_everything_is_persisted_regardless_of_the_floor(self):
        """The floor is a READ-time judgement: it will be retuned on other
        material, and re-indexing a library to change a number is not on."""
        doc = self._doc([0, 2000])
        self.assertEqual(len(doc["clusters"]), 1)
        self.assertFalse(doc["clusters"][0]["nameable"])
        self.assertEqual(doc["minScreenSeconds"], fp.MIN_SCREEN_S)


class ClusterDoc(unittest.TestCase):
    def test_noise_is_dropped_but_matches_are_carried(self):
        doc = fp.build_clusters(
            content_id="c", labels=[0, 0, -1, 1], hits=hits(0, 2000, 4000,
                                                            6000),
            centroids={0: [1.0, 0.0], 1: [0.0, 1.0]},
            matched={0: ("tmdb:1", 0.71)},
            runtime_ms=10_000, sample_fps=0.5, generated="now",
            version="test")
        self.assertEqual({c["cluster"] for c in doc["clusters"]}, {0, 1})
        by = {c["cluster"]: c for c in doc["clusters"]}
        self.assertEqual(by[0]["matched"], {"actorId": "tmdb:1", "sim": 0.71})
        self.assertIsNone(by[1]["matched"])

    def test_sample_boxes_are_string_keyed_for_the_crop_writer(self):
        """String keys on purpose: JSON stringifies them, and a reader that
        saw ints in memory but strings from disk breaks after a restart."""
        import json
        doc = fp.build_clusters(
            content_id="c", labels=[0], hits=hits(1234), centroids={},
            matched={}, runtime_ms=10_000, sample_fps=0.5, generated="now",
            version="test")
        self.assertEqual(doc["samples"]["0"][0]["bbox"], [1, 2, 3, 4])
        self.assertEqual(doc["samples"]["0"][0]["ms"], 1234)
        self.assertEqual(json.loads(json.dumps(doc))["samples"],
                         doc["samples"])

    def test_samples_do_not_reach_disk(self):
        """They are unreachable once the frames are gone, and they tripled
        the file. Spans -- which naming needs -- stay."""
        with tempfile.TemporaryDirectory() as d:
            doc = fp.build_clusters(
                content_id="c", labels=[0, 0], hits=hits(0, 2000),
                centroids={0: [1.0, 0.0]}, matched={}, runtime_ms=10_000,
                sample_fps=0.5, generated="now", version="test")
            fp.write_clusters(Path(d), "c", doc)
            back = fp.read_clusters(Path(d), "c")
        self.assertNotIn("samples", back)
        self.assertEqual(back["clusters"][0]["spans"], [[0.0, 0.2]])
        self.assertIn("samples", doc)      # still there for the crop writer


class SharedPrintsHalf(unittest.TestCase):
    """Faces and voices keep separate files and separate thresholds while
    sharing one implementation."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def test_the_two_stores_do_not_collide(self):
        fp.enroll(self.store, "tmdb:1", actor_id="tmdb:1", character="Chuck",
                  embedding=[1.0, 0.0], content_id="c1")
        vp.enroll(self.store, "tmdb:1", actor_id="tmdb:1", character="Shrek",
                  embedding=[0.0, 1.0], content_id="c2")
        self.assertEqual(fp.read_prints(self.store)["tmdb:1"]["character"],
                         "Chuck")
        self.assertEqual(vp.read_prints(self.store)["tmdb:1"]["character"],
                         "Shrek")
        self.assertTrue(fp.prints_path(self.store).name == "faceprints.json")
        self.assertTrue(vp.prints_path(self.store).name == "voiceprints.json")

    def test_suggest_honours_the_face_threshold(self):
        fp.enroll(self.store, "tmdb:1", actor_id="tmdb:1", character="Chuck",
                  embedding=[1.0, 0.0], content_id="c1")
        # cos = 0.6 -> above the face bar (0.55), below the voice bar (0.75)
        probe = [0.6, 0.8]
        self.assertIsNotNone(fp.suggest(self.store, probe))
        self.assertEqual(fp.suggest(self.store, probe)["character"], "Chuck")
        vp.enroll(self.store, "tmdb:9", actor_id="tmdb:9", character="X",
                  embedding=[1.0, 0.0], content_id="c1")
        self.assertIsNone(vp.suggest(self.store, probe))

    def test_a_print_cannot_suggest_against_its_own_title(self):
        fp.enroll(self.store, "tmdb:1", actor_id="tmdb:1", character="Chuck",
                  embedding=[1.0, 0.0], content_id="c1")
        self.assertIsNone(fp.suggest(self.store, [1.0, 0.0],
                                     exclude_content="c1"))

    def test_thresholds_stay_where_the_measurement_put_them(self):
        self.assertEqual(fp.MATCH_THRESHOLD, 0.55)
        self.assertEqual(vp.MATCH_THRESHOLD, 0.75)
        self.assertEqual(fp.FACE.name, "faces")
        self.assertIsInstance(fp.FACE, prints.Kind)


class Strength(unittest.TestCase):
    """How strong a match is gets said in WORDS. Cosine is not a probability:
    0.55 is the line above which every measured face match was right, and
    "55% confident" would read as a coin flip."""

    def test_the_face_bands(self):
        self.assertEqual(fp.confidence(0.774), "strong match")
        self.assertEqual(fp.confidence(0.66), "good match")
        self.assertEqual(fp.confidence(0.56), "borderline")
        self.assertEqual(fp.confidence(None), "")

    def test_each_modality_carries_its_own_bands(self):
        """Voices separate far more cleanly than faces do -- 0.88 is an
        ordinary voice match and an extraordinary face one."""
        self.assertEqual(fp.confidence(0.88), "strong match")
        self.assertEqual(vp.confidence(0.88), "good match")
        self.assertEqual(vp.confidence(0.95), "strong match")

    def test_no_band_ever_undercuts_its_own_threshold(self):
        for kind in (fp.FACE, vp.VOICE):
            self.assertLess(kind.threshold, kind.good, kind.name)
            self.assertLess(kind.good, kind.strong, kind.name)


if __name__ == "__main__":
    unittest.main()
