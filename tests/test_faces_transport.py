"""Engine-faces service transport (stdlib unittest, no network, no cv2).

Covers the U2b wiring: the HTTP client's parsing/URL construction, env-based
transport selection, the /analyze → FaceHit mapping (incl. --max-frames
trimming), and reference-embedding aggregation via the service path.
"""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

from xray import engines
from xray.engines import HttpFaceEngine, face_transport


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class TestHttpFaceEngine(unittest.TestCase):
    def test_analyze_parses_and_hits_endpoint(self):
        eng = HttpFaceEngine("http://faces:8081/")
        payload = {"model_version": "sface-v1",
                   "faces": [{"frame_index": 3, "bbox": [1, 2, 3, 4],
                              "embedding": [0.1] * 4}]}
        with mock.patch("requests.post", return_value=FakeResponse(payload)) as post:
            mv, faces = eng.analyze(Path("/work/frames"))
        self.assertEqual(mv, "sface-v1")
        self.assertEqual(faces[0]["frame_index"], 3)
        url, kwargs = post.call_args[0][0], post.call_args[1]
        self.assertEqual(url, "http://faces:8081/analyze")  # trailing / stripped
        self.assertEqual(kwargs["json"], {"frames_dir": "/work/frames"})

    def test_embed_image_file_returns_faces(self):
        eng = HttpFaceEngine("http://faces:8081")
        payload = {"faces": [{"bbox": [0, 0, 10, 10], "embedding": [1.0, 0.0]}]}
        with mock.patch("requests.post", return_value=FakeResponse(payload)):
            faces = eng.embed_image_file(Path("/work/refs/ref_photo.jpg"))
        self.assertEqual(len(faces), 1)
        self.assertEqual(faces[0]["bbox"], [0, 0, 10, 10])

    def test_face_transport_env_selection(self):
        with mock.patch.dict(os.environ, {"XRAY_ENGINE_FACES_URL": ""}):
            self.assertIsNone(face_transport())
        with mock.patch.dict(os.environ,
                             {"XRAY_ENGINE_FACES_URL": "http://faces:8081"}):
            t = face_transport()
            self.assertIsInstance(t, HttpFaceEngine)
            self.assertEqual(t.base, "http://faces:8081")

    def test_ready_reports_unreachable(self):
        import requests
        eng = HttpFaceEngine("http://nope:1")
        with mock.patch("requests.get",
                        side_effect=requests.ConnectionError("boom")):
            ok, msg = eng.ready()
        self.assertFalse(ok)
        self.assertIn("engine-faces unreachable", msg)


class TestFacesToHits(unittest.TestCase):
    def test_maps_timestamps_and_drops_unknown_frames(self):
        # Import here: index_title pulls in cv2, present in the dev venv.
        from types import SimpleNamespace
        from xray.passes.index_title import faces_to_hits

        frames = [SimpleNamespace(index=1, timestamp_ms=0),
                  SimpleNamespace(index=2, timestamp_ms=2000)]
        det = [
            {"frame_index": 1, "embedding": [0.5, 0.5]},
            {"frame_index": 2, "embedding": [1.0, 0.0]},
            {"frame_index": 9, "embedding": [0.0, 1.0]},  # trimmed/stray
        ]
        embeddings, hits = faces_to_hits(det, frames)
        self.assertEqual(len(embeddings), 2)
        self.assertEqual(embeddings[0].dtype, np.float32)
        self.assertEqual([h.frame_index for h in hits], [1, 2])
        self.assertEqual([h.timestamp_ms for h in hits], [0, 2000])


class TestReferenceEmbeddingsHttp(unittest.TestCase):
    def test_largest_bbox_wins_and_vectors_average(self):
        from xray.refs import build_reference_embeddings_http

        class FakeTransport:
            def embed_image_file(self, path):
                # two faces: the larger one carries the meaningful vector
                return [
                    {"bbox": [0, 0, 4, 4], "embedding": [9.0, 9.0]},
                    {"bbox": [0, 0, 100, 100], "embedding": [2.0, 0.0]},
                ]

        cast = [{"actorId": "tmdb:1", "name": "A",
                 "images": ["http://img/a1", "http://img/a2"]},
                {"actorId": "tmdb:2", "name": "B", "images": []}]

        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch("xray.refs._fetch_bytes", return_value=b"jpg"):
            logged = []
            refs = build_reference_embeddings_http(
                cast, FakeTransport(), tmp, log=logged.append)
            # spool file is cleaned up afterwards
            self.assertEqual(list(Path(tmp).iterdir()), [])

        self.assertIn("tmdb:1", refs)
        # both photos yield [2,0] (largest bbox) → mean [2,0] → normalized [1,0]
        np.testing.assert_allclose(refs["tmdb:1"], [1.0, 0.0], atol=1e-6)
        self.assertNotIn("tmdb:2", refs)  # no photos → skipped + logged
        self.assertTrue(any("B" in line for line in logged))

    def test_download_failure_is_tolerated(self):
        import requests
        from xray.refs import build_reference_embeddings_http

        class FakeTransport:
            def embed_image_file(self, path):
                return [{"bbox": [0, 0, 1, 1], "embedding": [0.0, 3.0]}]

        cast = [{"actorId": "tmdb:3", "name": "C",
                 "images": ["http://img/bad", "http://img/good"]}]

        calls = iter([requests.ConnectionError("down"), b"jpg"])

        def fetch(url, timeout=20):
            v = next(calls)
            if isinstance(v, Exception):
                raise v
            return v

        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch("xray.refs._fetch_bytes", side_effect=fetch):
            refs = build_reference_embeddings_http(cast, FakeTransport(), tmp)
        np.testing.assert_allclose(refs["tmdb:3"], [0.0, 1.0], atol=1e-6)

    def test_it_reports_progress_over_the_cast(self):
        """A download plus a detect+embed per member, and it used to run under
        a phase label with no number — minutes of a still bar."""
        from xray.refs import build_reference_embeddings_http

        class FakeTransport:
            def embed_image_file(self, path):
                return [{"bbox": [0, 0, 1, 1], "embedding": [1.0, 0.0]}]

        cast = [{"actorId": f"tmdb:{i}", "name": f"A{i}", "images": ["u"]}
                for i in range(3)]
        ticks = []
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch("xray.refs._fetch_bytes", return_value=b"jpg"):
            build_reference_embeddings_http(
                cast, FakeTransport(), tmp,
                on_progress=lambda d, t: ticks.append((d, t)))
        self.assertEqual(ticks, [(0, 3), (1, 3), (2, 3)])


if __name__ == "__main__":
    unittest.main()
