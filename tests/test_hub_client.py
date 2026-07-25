"""How the generator talks to the hub: default host, and where reads go.

Two things are pinned here. The hub ships configured rather than pasted in,
and downloads follow the manifest's `timelines` base instead of the hub
host, so the origin does not end up serving every byte.
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from xray import keys, share  # noqa: E402


class TestHubDefault(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = mock.patch.dict(os.environ, {
            "XRAY_SETTINGS": str(Path(self.tmp.name) / "settings.json"),
            "XRAY_HUB_URL": "",
        })
        env.start()
        self.addCleanup(env.stop)

    def test_defaults_to_the_project_hub(self):
        self.assertEqual(keys.hub_url(), "https://hub.openxray.net")

    def test_env_override_still_wins_for_development(self):
        with mock.patch.dict(os.environ, {"XRAY_HUB_URL": "http://localhost:8090/"}):
            self.assertEqual(keys.hub_url(), "http://localhost:8090")

    def test_dash_opts_out_entirely(self):
        with mock.patch.dict(os.environ, {"XRAY_HUB_URL": "-"}):
            self.assertEqual(keys.hub_url(), "")


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self.payload


class TestReadsFollowTheManifest(unittest.TestCase):
    def test_timelines_base_comes_from_index_json(self):
        with mock.patch.object(share.requests, "get",
                               return_value=FakeResponse(
                                   {"timelines": "https://cdn.example.net/t/"})):
            self.assertEqual(share.timelines_base("https://hub.example.net"),
                             "https://cdn.example.net/t")

    def test_fetch_uses_that_base_not_the_hub_host(self):
        # The whole point of the read/write split: bytes come from the CDN.
        seen = []

        def fake_get(url, **kw):
            seen.append(url)
            if url.endswith("/index.json"):
                return FakeResponse({"timelines": "https://cdn.example.net/t"})
            r = FakeResponse({"contentId": "tmdb-movie-1"})
            r.status_code = 200
            return r

        with mock.patch.object(share.requests, "get", fake_get), \
                mock.patch.object(share, "place_shared_doc",
                                  return_value=Path("/dev/null")):
            share.fetch_from_hub(Path("/tmp"), "https://hub.example.net",
                                 "tmdb-movie-1")
        self.assertIn("https://cdn.example.net/t/tmdb-movie-1.json", seen)
        self.assertNotIn("https://hub.example.net/t/tmdb-movie-1.json", seen)

    def test_falls_back_to_the_hub_when_the_manifest_is_unreachable(self):
        # A hub with no bucket serves /t itself, so this is the right default.
        with mock.patch.object(share.requests, "get",
                               side_effect=share.requests.RequestException("x")):
            self.assertEqual(share.timelines_base("https://hub.example.net"),
                             "https://hub.example.net/t")


if __name__ == "__main__":
    unittest.main()
