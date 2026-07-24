"""MediaSource seam + backend adapters (stdlib unittest, no pytest dep).

Run: python -m unittest discover -s tests
These cover the backend-agnostic refactor: the factory, key_prefix namespacing,
Jellyfin's normalization to the common item dict, and that the pipeline drives
whatever source it's handed. No live Plex/Jellyfin server is touched.
"""
import unittest

from xray import keys, pipeline
from xray.sources.base import MediaSource, open_source
from xray.sources.jellyfin import Jellyfin, JellyfinServer
from xray.sources.plex import PlexServer


# --- canned Jellyfin payloads -------------------------------------------------

ITEMS = {
    "m1": {"Id": "m1", "Type": "Movie", "Name": "Casino",
           "ProductionYear": 1995, "RunTimeTicks": 178_000_000_000,
           "Container": "mkv", "Path": "/media/casino.mkv",
           "ProviderIds": {"Tmdb": "769"}},
    "e1": {"Id": "e1", "Type": "Episode", "Name": "Chapter One",
           "ParentIndexNumber": 1, "IndexNumber": 1,
           "SeriesName": "Stranger Things", "SeriesId": "s1",
           "RunTimeTicks": 29_000_000_000, "ProviderIds": {}},
    "s1": {"Id": "s1", "Type": "Series", "Name": "Stranger Things",
           "ProviderIds": {"Tmdb": "66732"}},
}
FOLDERS = [{"Id": "lib1", "Name": "Movies", "CollectionType": "movies"},
           {"Id": "lib2", "Name": "Shows", "CollectionType": "tvshows"}]


def fake_get(path, **params):
    if path == "/Items":
        if "ids" in params:
            return {"Items": [ITEMS[params["ids"]]]}
        if "searchTerm" in params:
            return {"Items": [ITEMS["m1"], ITEMS["e1"]]}
        if params.get("ParentId") == "lib1":
            return {"Items": [ITEMS["m1"]]}
        if params.get("ParentId") == "lib2":
            return {"Items": [ITEMS["e1"]]}
        if params.get("IncludeItemTypes") == "Movie":
            return {"Items": [ITEMS["m1"]]}
        if params.get("IncludeItemTypes") == "Series":
            return {"Items": [ITEMS["s1"]]}
        return {"Items": []}
    if path == "/Library/MediaFolders":
        return {"Items": FOLDERS}
    if path.startswith("/Shows/") and path.endswith("/Episodes"):
        return {"Items": [ITEMS["e1"]]}
    raise AssertionError(f"unexpected Jellyfin call: {path} {params}")


def make_jf():
    jf = JellyfinServer("http://jf.local", "TOK", user_id="u1")
    jf._get = fake_get  # bypass the network
    return jf


# --- a minimal in-memory source for pipeline tests ---------------------------

class FakeSource:
    key_prefix = "fake"

    def __init__(self):
        self.resolved = []

    def search(self, query, limit=12):
        return [{"ratingKey": "r9", "type": "movie", "title": query}]

    def section_leaves(self, section_key):
        return [{"ratingKey": "a"}, {"ratingKey": "b"}, {"ratingKey": "c"}]

    def resolve(self, item_id):
        self.resolved.append(item_id)
        return {"ratingKey": item_id, "type": "movie", "title": "T"}


class TestSeam(unittest.TestCase):
    def test_factory_dispatch_and_key_prefix(self):
        p = open_source("plex", "http://x", "tok")
        j = open_source("jellyfin", "http://y", "tok", user_id="u1")
        self.assertIsInstance(p, PlexServer)
        self.assertIsInstance(j, JellyfinServer)
        self.assertEqual(p.key_prefix, "plex")
        self.assertEqual(j.key_prefix, "jellyfin")

    def test_default_backend_is_plex(self):
        self.assertIsInstance(open_source("", "http://x", "t"), PlexServer)
        self.assertIsInstance(open_source(None, "http://x", "t"), PlexServer)

    def test_unknown_backend_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            open_source("emby", "http://z", "t")
        self.assertIn("emby", str(ctx.exception))

    def test_both_satisfy_protocol(self):
        self.assertIsInstance(open_source("plex", "u", "t"), MediaSource)
        self.assertIsInstance(open_source("jellyfin", "u", "t"), MediaSource)
        self.assertIsInstance(FakeSource(), MediaSource)

    def test_jellyfin_alias(self):
        self.assertIs(Jellyfin, JellyfinServer)

    def test_backend_token_routing(self):
        self.assertEqual(keys.backend_token("jellyfin"), keys.jellyfin_token())
        self.assertEqual(keys.backend_token("plex"), keys.plex_token())
        self.assertEqual(keys.backend_token(""), keys.plex_token())


class TestJellyfinAdapter(unittest.TestCase):
    def test_download_url(self):
        jf = make_jf()
        self.assertEqual(
            jf.download_url("42"),
            "http://jf.local/Videos/42/stream?static=true&api_key=TOK")

    def test_resolve_movie(self):
        info = make_jf().resolve("m1")
        self.assertEqual(info["ratingKey"], "m1")
        self.assertEqual(info["type"], "movie")
        self.assertEqual(info["title"], "Casino")
        self.assertEqual(info["tmdbId"], "769")
        self.assertEqual(info["container"], "mkv")
        self.assertEqual(info["durationMs"], 17_800_000)  # ticks / 1e4
        self.assertIn("static=true", info["downloadUrl"])

    def test_resolve_episode_pulls_series_tmdb(self):
        info = make_jf().resolve("e1")
        self.assertEqual(info["type"], "episode")
        self.assertEqual(info["season"], 1)
        self.assertEqual(info["episode"], 1)
        self.assertEqual(info["grandparentTitle"], "Stranger Things")
        self.assertEqual(info["showRatingKey"], "s1")
        self.assertEqual(info["showTmdbId"], "66732")  # from the series item

    def test_search_normalizes(self):
        rows = make_jf().search("stranger")
        self.assertEqual([r["type"] for r in rows], ["movie", "episode"])
        self.assertEqual(rows[1]["grandparentTitle"], "Stranger Things")

    def test_sections_and_leaves(self):
        jf = make_jf()
        secs = {s["title"]: s["type"] for s in jf.sections()}
        self.assertEqual(secs, {"Movies": "movie", "Shows": "show"})
        leaves = jf.section_leaves("Movies")  # by title
        self.assertEqual(leaves[0]["ratingKey"], "m1")
        eps = jf.section_leaves("lib2")       # by id
        self.assertEqual(eps[0]["type"], "episode")

    def test_map_helpers(self):
        jf = make_jf()
        self.assertEqual(jf.items_by_tmdb("Movie"), {"769": "m1"})
        self.assertEqual(jf.episode_id("s1", 1, 1), "e1")
        self.assertIsNone(jf.episode_id("s1", 9, 9))


class TestPipelineIsBackendAgnostic(unittest.TestCase):
    def test_enumerate_library_respects_max(self):
        fs = FakeSource()
        self.assertEqual(
            pipeline.enumerate_targets(fs, library="X", max_titles=2),
            ["a", "b"])

    def test_enumerate_rating_key_passthrough(self):
        self.assertEqual(
            pipeline.enumerate_targets(FakeSource(), rating_key="z"), ["z"])

    def test_enumerate_search_uses_the_source(self):
        fs = FakeSource()
        # goes through index_title.resolve → source.search + source.resolve
        self.assertEqual(pipeline.enumerate_targets(fs, search="Q"), ["r9"])
        self.assertEqual(fs.resolved, ["r9"])


if __name__ == "__main__":
    unittest.main()
