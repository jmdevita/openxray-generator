"""Auto-share: when a finished timeline is sent to the hub, and when not.

Uploading publishes to a public catalog, so the interesting cases here are
all the ones where it must NOT fire: off by default, never for a timeline
that came from the hub, never for a failed run, and never loudly enough to
take a job down with it.
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("XRAY_STORE", tempfile.mkdtemp())

from xray import keys  # noqa: E402
from xray.service import orchestrator as O  # noqa: E402


def result(cid="tmdb-movie-1", index="ok", **steps):
    return {"key": cid, "title": "T", "steps": {"index": index, **steps}}


class AutoshareCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = mock.patch.dict(os.environ, {
            "XRAY_SETTINGS": str(Path(self.tmp.name) / "settings.json"),
            "XRAY_HUB_URL": "https://hub.example.net",
            "XRAY_HUB_AUTOSHARE": "",
        })
        env.start()
        self.addCleanup(env.stop)
        self.lines = []

    def run_share(self, res, *, on=True, upload=None):
        env = {"XRAY_HUB_AUTOSHARE": "on"} if on else {}
        with mock.patch.dict(os.environ, env), \
                mock.patch("xray.share.upload_to_hub",
                           upload or mock.DEFAULT) as up:
            O._autoshare(res, self.lines.append)
        return up


class TestDefaultsOff(AutoshareCase):
    def test_setting_is_off_unless_asked_for(self):
        self.assertFalse(keys.hub_autoshare())

    def test_nothing_uploads_when_off(self):
        up = self.run_share(result(), on=False)
        up.assert_not_called()

    def test_truthy_spellings_all_work(self):
        for value in ("on", "1", "true", "TRUE", "yes"):
            with mock.patch.dict(os.environ, {"XRAY_HUB_AUTOSHARE": value}):
                self.assertTrue(keys.hub_autoshare(), value)

    def test_a_stored_false_does_not_read_as_on(self):
        # settings_store str()-coerces, so this is the trap being guarded.
        with mock.patch.dict(os.environ, {"XRAY_HUB_AUTOSHARE": "False"}):
            self.assertFalse(keys.hub_autoshare())


class TestWhatGetsShared(AutoshareCase):
    def test_a_freshly_indexed_title_is_uploaded(self):
        up = self.run_share(result())
        up.assert_called_once()
        self.assertEqual(up.call_args[0][1], "tmdb-movie-1")

    def test_a_hub_fetched_title_is_never_echoed_back(self):
        # It came FROM the catalog; sending it again is queue noise.
        up = self.run_share(result(index="hub"))
        up.assert_not_called()

    def test_a_failed_index_is_not_uploaded(self):
        up = self.run_share(result(index="failed: no content identity"))
        up.assert_not_called()

    def test_a_title_with_no_content_id_is_not_uploaded(self):
        up = self.run_share(result(cid="1234"))
        up.assert_not_called()

    def test_nothing_uploads_without_a_hub(self):
        with mock.patch.dict(os.environ, {"XRAY_HUB_URL": "-"}):
            up = self.run_share(result())
        up.assert_not_called()


class TestFailureIsContained(AutoshareCase):
    def test_upload_errors_are_logged_not_raised(self):
        boom = mock.Mock(side_effect=RuntimeError("hub on fire"))
        self.run_share(result(), upload=boom)   # must not raise
        self.assertTrue(any("upload failed" in ln for ln in self.lines),
                        self.lines)


if __name__ == "__main__":
    unittest.main()
