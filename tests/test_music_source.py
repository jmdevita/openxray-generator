"""How the music pass finds the audio to probe.

The timeline contract carries no server-local id, so when there is no
harvested audio and no --media the pass has to get back to a server item
through the MANIFEST. Reading it off the doc (as this once did) silently
resolves None on every timeline written since the schema cleanup, which is
why the reverse lookup is pinned here.
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from xray import store as st  # noqa: E402
from xray.passes import music  # noqa: E402


class FakeSegmenter:
    def ready(self):
        return True, ""

    def segment(self, audio, work, **kw):
        return []


class FakeSource:
    key_prefix = "plex"

    def __init__(self):
        self.resolved = []

    def resolve(self, item_id):
        self.resolved.append(item_id)
        return {"ratingKey": item_id, "downloadUrl": f"http://plex/{item_id}"}


class MusicSourceCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Path(self.tmp.name)
        # A post-cleanup timeline: content id only, no ratingKey anywhere.
        self.cid = "tmdb-movie-769"
        st.canonical_path(self.store, self.cid).write_text(json.dumps({
            "contentId": self.cid, "version": 1, "generated": "2026-07-24",
            "cast": [], "actorIntervals": [], "musicIntervals": [],
            "trivia": []}))
        self.source = FakeSource()
        for c in (mock.patch.object(music.engines, "audio_segmenter",
                                    FakeSegmenter),
                  mock.patch.object(music, "extract_audio",
                                    lambda src, work, stem: Path(src))):
            c.start()
            self.addCleanup(c.stop)

    def run_pass(self):
        return music.run(self.store, self.cid, "audd-token",
                         source=self.source, dry_run=True)


class TestManifestLookup(MusicSourceCase):
    def test_resolves_through_the_manifest_without_a_ratingKey(self):
        st.map_lookup(self.store, "plex:288", self.cid)
        self.run_pass()
        self.assertEqual(self.source.resolved, ["288"])

    def test_prefers_the_newest_mapping_when_several_exist(self):
        # Real stores carry legacy synthetic keys alongside real ids.
        st.map_lookup(self.store, "plex:billions-s1e1", self.cid)
        st.map_lookup(self.store, "plex:3606", self.cid)
        self.run_pass()
        self.assertEqual(self.source.resolved, ["3606"])

    def test_unmapped_title_fails_loudly_instead_of_resolving_none(self):
        with self.assertRaises(SystemExit) as ctx:
            self.run_pass()
        msg = str(ctx.exception)
        self.assertIn(self.cid, msg)
        self.assertIn("plex", msg)
        self.assertIn("--media", msg)          # tells you the way out
        self.assertEqual(self.source.resolved, [])  # never called with None

    def test_other_backends_mappings_are_not_borrowed(self):
        # A Jellyfin id is meaningless to a Plex source.
        st.map_lookup(self.store, "jellyfin:abc123", self.cid)
        with self.assertRaises(SystemExit):
            self.run_pass()
        self.assertEqual(self.source.resolved, [])

    def test_harvested_audio_still_short_circuits_the_lookup(self):
        work = self.store / "music_work" / self.cid
        work.mkdir(parents=True)
        (work / f"{self.cid}__audio.mp3").write_bytes(b"x")
        self.run_pass()
        self.assertEqual(self.source.resolved, [])  # no server call at all


class TestBackendIds(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Path(self.tmp.name)

    def test_reverse_lookup_filters_by_prefix_and_target(self):
        st.map_lookup(self.store, "plex:1", "tmdb-movie-1")
        st.map_lookup(self.store, "plex:2", "tmdb-movie-1")
        st.map_lookup(self.store, "jellyfin:j1", "tmdb-movie-1")
        st.map_lookup(self.store, "plex:9", "tmdb-movie-2")
        self.assertEqual(st.backend_ids(self.store, "tmdb-movie-1", "plex"),
                         ["1", "2"])
        self.assertEqual(st.backend_ids(self.store, "tmdb-movie-1", "jellyfin"),
                         ["j1"])
        self.assertEqual(st.backend_ids(self.store, "tmdb-movie-3", "plex"), [])

    def test_ids_containing_colons_survive_the_split(self):
        st.map_lookup(self.store, "jellyfin:a:b:c", "tmdb-movie-1")
        self.assertEqual(st.backend_ids(self.store, "tmdb-movie-1", "jellyfin"),
                         ["a:b:c"])

    def test_empty_store_has_no_mappings(self):
        self.assertEqual(st.backend_ids(self.store, "tmdb-movie-1", "plex"), [])


if __name__ == "__main__":
    unittest.main()
