"""The face labelling endpoints: rows, naming, and interval rebuild.

Handlers are called directly (see test_dashboard for why). What matters here
is the three-state row model faces need and voices do not, and that naming a
cluster produces intervals a timeline will actually validate.
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("XRAY_STORE", tempfile.mkdtemp())

from fastapi import HTTPException                       # noqa: E402

from xray import faceprints as fp, schema, store as st  # noqa: E402
from xray.service import orchestrator as O              # noqa: E402

CID = "tmdb-movie-769"


def cluster(n, seconds, scenes=3, spans=None, matched=None, emb=(1.0, 0.0)):
    return {"cluster": n, "samples": int(seconds / 2),
            "screenSeconds": seconds, "scenes": scenes,
            "spans": spans or [[0.0, 0.1], [0.5, 0.1]],
            "nameable": seconds >= fp.MIN_SCREEN_S and scenes >= fp.MIN_SCENES,
            "matched": matched, "embedding": list(emb)}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        p = mock.patch.object(O, "STORE", self.store)
        p.start()
        self.addCleanup(p.stop)

        doc = schema.timeline(
            CID, [{"actorId": "tmdb:1", "name": "Paul Giamatti",
                   "character": "Chuck", "thumb": None},
                  {"actorId": "tmdb:2", "name": "Damian Lewis",
                   "character": "Axe", "thumb": None}],
            [], "sface-v1", duration_ms=3_600_000)
        st.write_timeline(st.canonical_path(self.store, CID), doc)

    def write_clusters(self, *clusters):
        fp.write_clusters(self.store, CID, {
            "contentId": CID, "generated": "now", "version": "sface-v1",
            "sampleFps": 0.5, "runtimeMs": 3_600_000,
            "minScreenSeconds": fp.MIN_SCREEN_S, "minScenes": fp.MIN_SCENES,
            "clusters": list(clusters), "samples": {}})

    def timeline(self):
        return json.loads(st.canonical_path(self.store, CID).read_text())


class Rows(Base):
    def test_below_floor_clusters_are_counted_not_listed(self):
        self.write_clusters(cluster(0, 120.0), cluster(1, 8.0),
                            cluster(2, 40.0, scenes=1))
        body = O.api_faces(CID)
        self.assertEqual([r["cluster"] for r in body["rows"]], [0])
        self.assertEqual(body["belowFloor"], 2)

    def test_rows_are_ranked_by_screen_time(self):
        self.write_clusters(cluster(0, 30.0), cluster(1, 300.0),
                            cluster(2, 90.0))
        self.assertEqual([r["cluster"] for r in O.api_faces(CID)["rows"]],
                         [1, 2, 0])

    def test_a_weak_auto_match_is_not_presented_as_one(self):
        """0.373 was a real false match on the pilot -- 122 seconds of one
        actor claimed as another."""
        self.write_clusters(
            cluster(0, 122.0, matched={"actorId": "tmdb:2", "sim": 0.373}))
        row = O.api_faces(CID)["rows"][0]
        self.assertIsNone(row["matched"])

    def test_a_strong_auto_match_carries_the_character_name(self):
        self.write_clusters(
            cluster(0, 122.0, matched={"actorId": "tmdb:2", "sim": 0.71}))
        row = O.api_faces(CID)["rows"][0]
        self.assertEqual(row["matched"]["character"], "Axe")
        self.assertEqual(row["matched"]["sim"], 0.71)

    def test_missing_clusters_are_a_404_not_an_empty_screen(self):
        with self.assertRaises(HTTPException) as e:
            O.api_faces(CID)
        self.assertEqual(e.exception.status_code, 404)

    def test_an_unmatched_row_is_offered_a_faceprint_suggestion(self):
        fp.enroll(self.store, "tmdb:1", actor_id="tmdb:1", character="Chuck",
                  embedding=[1.0, 0.0], content_id="other-title")
        self.write_clusters(cluster(0, 60.0, emb=(1.0, 0.0)))
        row = O.api_faces(CID)["rows"][0]
        self.assertEqual(row["suggest"]["character"], "Chuck")


class Naming(Base):
    def test_naming_writes_intervals_and_enrols_a_faceprint(self):
        self.write_clusters(cluster(0, 60.0))
        out = O.api_name_face(CID, O.FaceNameRequest(
            cluster=0, actor_id="tmdb:1", character="Chuck"))
        self.assertEqual(out["intervals"], 2)          # one per span
        ivs = self.timeline()["actorIntervals"]
        self.assertEqual({iv["actorId"] for iv in ivs}, {"tmdb:1"})
        self.assertEqual(ivs[0]["source"], "face")
        # named by eye, so full confidence
        self.assertEqual(ivs[0]["confidence"], 1.0)
        self.assertIn("tmdb:1", fp.read_prints(self.store))

    def test_spans_become_real_millisecond_intervals(self):
        self.write_clusters(cluster(0, 60.0, spans=[[0.25, 0.5]]))
        O.api_name_face(CID, O.FaceNameRequest(
            cluster=0, actor_id="tmdb:1", character="Chuck"))
        iv = self.timeline()["actorIntervals"][0]
        self.assertEqual((iv["startMs"], iv["endMs"]), (900_000, 2_700_000))

    def test_an_accepted_suggestion_keeps_the_score_that_earned_it(self):
        self.write_clusters(cluster(0, 60.0))
        O.api_name_face(CID, O.FaceNameRequest(
            cluster=0, actor_id="tmdb:1", character="Chuck", sim=0.82))
        self.assertEqual(self.timeline()["actorIntervals"][0]["confidence"],
                         0.82)

    def test_clearing_a_name_removes_its_intervals(self):
        self.write_clusters(cluster(0, 60.0))
        O.api_name_face(CID, O.FaceNameRequest(
            cluster=0, actor_id="tmdb:1", character="Chuck"))
        out = O.api_name_face(CID, O.FaceNameRequest(cluster=0))
        self.assertEqual(out["intervals"], 0)
        self.assertEqual(self.timeline()["actorIntervals"], [])

    def test_strong_auto_matches_survive_a_rebuild_but_weak_ones_do_not(self):
        """The rebuild is the whole truth for faces, so it must not drop the
        pass's own work -- and it retires claims the calibration disowned."""
        self.write_clusters(
            cluster(0, 60.0),
            cluster(1, 90.0, matched={"actorId": "tmdb:2", "sim": 0.71}),
            cluster(2, 90.0, matched={"actorId": "tmdb:1", "sim": 0.40}))
        O.api_name_face(CID, O.FaceNameRequest(
            cluster=0, actor_id="tmdb:1", character="Chuck"))
        ivs = self.timeline()["actorIntervals"]
        self.assertEqual(len(ivs), 4)                  # 2 named + 2 auto
        self.assertEqual({iv["confidence"] for iv in ivs}, {1.0, 0.71})

    def test_voice_intervals_are_left_alone(self):
        path = st.canonical_path(self.store, CID)
        doc = json.loads(path.read_text())
        doc["actorIntervals"] = [{"actorId": "tmdb:9", "startMs": 0,
                                  "endMs": 1000, "confidence": 1.0,
                                  "source": "voice"}]
        st.write_timeline(path, doc)
        self.write_clusters(cluster(0, 60.0))
        O.api_name_face(CID, O.FaceNameRequest(
            cluster=0, actor_id="tmdb:1", character="Chuck"))
        srcs = [iv["source"] for iv in self.timeline()["actorIntervals"]]
        self.assertIn("voice", srcs)
        self.assertIn("face", srcs)

    def test_the_rebuilt_timeline_still_validates(self):
        self.write_clusters(cluster(0, 60.0))
        O.api_name_face(CID, O.FaceNameRequest(
            cluster=0, actor_id="tmdb:1", character="Chuck"))
        st.validate(self.timeline())

    def test_naming_an_unknown_cluster_is_a_404(self):
        self.write_clusters(cluster(0, 60.0))
        with self.assertRaises(HTTPException) as e:
            O.api_name_face(CID, O.FaceNameRequest(
                cluster=99, actor_id="tmdb:1", character="Chuck"))
        self.assertEqual(e.exception.status_code, 404)


class StoreRowState(Base):
    def test_auto_matched_clusters_count_as_settled(self):
        """A strong match already puts intervals in the timeline; the row
        should not nag about work that is done."""
        self.write_clusters(
            cluster(0, 100.0, matched={"actorId": "tmdb:2", "sim": 0.71}),
            cluster(1, 100.0))
        doc = self.timeline()
        st_ = O._face_state(doc)
        self.assertEqual((st_["nameable"], st_["named"], st_["pct"]),
                         (2, 1, 50))

    def test_a_weak_match_is_still_owed(self):
        self.write_clusters(
            cluster(0, 100.0, matched={"actorId": "tmdb:2", "sim": 0.40}))
        self.assertEqual(O._face_state(self.timeline())["named"], 0)

    def test_naming_moves_the_needle(self):
        self.write_clusters(cluster(0, 300.0), cluster(1, 100.0))
        O.api_name_face(CID, O.FaceNameRequest(
            cluster=0, actor_id="tmdb:1", character="Chuck"))
        self.assertEqual(O._face_state(self.timeline())["pct"], 75)

    def test_titles_without_clusters_have_no_state(self):
        self.assertIsNone(O._face_state(self.timeline()))


class DashboardSurface(unittest.TestCase):
    """The Store row has to advertise face work the same way it advertises
    speaker work, and open the SAME screen in a different mode."""

    def setUp(self):
        self.html = O.dashboard()

    def test_the_row_reports_face_progress(self):
        self.assertIn("function faceNote(", self.html)
        self.assertIn("function faceAction(", self.html)

    def test_the_button_opens_the_screen_in_face_mode(self):
        self.assertIn('data-kind="faces"', self.html)
        self.assertIn("openLabelling(d.cid, d.kind)", self.html)

    def test_one_screen_serves_both_kinds(self):
        self.assertIn("const LAB_KINDS", self.html)
        for kind in ("speakers:", "faces:"):
            self.assertIn(kind, self.html)
        # ...and the face rows preview with a crop, not an audio clip
        self.assertIn("api/faces/", self.html)
        self.assertIn("labcrop", self.html)

    def test_an_unconfirmed_match_is_marked_for_checking(self):
        """The whole reason matched rows are shown at all."""
        self.assertIn("check me", self.html)


class Crops(Base):
    def test_missing_crop_is_a_404(self):
        with self.assertRaises(HTTPException) as e:
            O.api_face_crop(CID, 0)
        self.assertEqual(e.exception.status_code, 404)

    def test_an_existing_crop_is_served_as_jpeg(self):
        d = fp.crops_dir(self.store, CID)
        d.mkdir(parents=True, exist_ok=True)
        (d / "0.jpg").write_bytes(b"\xff\xd8\xff")
        self.assertEqual(O.api_face_crop(CID, 0).media_type, "image/jpeg")


if __name__ == "__main__":
    unittest.main()
