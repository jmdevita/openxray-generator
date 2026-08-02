"""Commons/Wikidata enrollment source (stdlib unittest, no network).

A fake session answers the three Wikidata API shapes the module uses
(haswbstatement search, wbsearchentities, wbgetentities), so these cover the
resolution chain, the name guard on the exact path, the human check on the
name fallback, URL formation, the photo cache, and the cast-shape contract
with refs.py enrollment.
"""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from xray.sources import commons


class FakeResp:
    def __init__(self, payload, status_code=200, headers=None):
        self._payload = payload
        self.status_code = status_code
        self.headers = headers or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise AssertionError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


class FakeSession:
    """Routes on the `action`/`list` params like the real API would."""

    def __init__(self, *, by_tmdb=None, by_name=None, entities=None):
        self.by_tmdb = by_tmdb or {}      # pid -> qid
        self.by_name = by_name or {}      # name -> [qids]
        self.entities = entities or {}    # qid -> full entity
        self.calls = []

    def get(self, url, params=None, timeout=None):
        params = params or {}
        self.calls.append(params)
        if params.get("list") == "search":
            want = params["srsearch"].split("=", 1)[1]
            qid = self.by_tmdb.get(want)
            return FakeResp({"query": {"search":
                                       [{"title": qid}] if qid else []}})
        if params.get("action") == "wbsearchentities":
            qids = self.by_name.get(params["search"], [])
            return FakeResp({"search": [{"id": q} for q in qids]})
        if params.get("action") == "wbgetentities":
            ents = {q: self.entities.get(q, {})
                    for q in params["ids"].split("|")}
            return FakeResp({"entities": ents})
        raise AssertionError(f"unexpected call: {params}")


def person(label, *files, aliases=(), human=True):
    """A Wikidata person item as wbgetentities returns it."""
    claims = {"P18": [{"mainsnak": {"datavalue": {"value": f}}}
                      for f in files]}
    if human:
        claims["P31"] = [{"mainsnak": {"datavalue": {"value": {"id": "Q5"}}}}]
    return {
        "labels": {"en": {"value": label}},
        "aliases": {"en": [{"value": a} for a in aliases]},
        "claims": claims,
    }


def quiet(*_a, **_k):
    pass


class ExactPath(unittest.TestCase):
    def test_tmdb_id_resolves_via_p4985_and_p18(self):
        s = FakeSession(by_tmdb={"20186": "Q170587"},
                        entities={"Q170587": person("Damian Lewis",
                                                    "Damian Lewis 2014.jpg")})
        cast = [{"actorId": "tmdb:20186", "name": "Damian Lewis",
                 "character": "Bobby Axelrod", "thumb": "t", "images": ["t"]}]
        out = commons.commons_cast(cast, session=s, log=quiet)
        self.assertEqual(out[0]["images"], [
            "https://commons.wikimedia.org/wiki/Special:FilePath/"
            "Damian_Lewis_2014.jpg?width=342"])
        self.assertEqual(out[0]["thumb"], out[0]["images"][0])
        # identity fields pass through untouched
        self.assertEqual(out[0]["actorId"], "tmdb:20186")
        self.assertEqual(out[0]["character"], "Bobby Axelrod")

    def test_filename_quoting(self):
        s = FakeSession(by_tmdb={"1": "Q1"},
                        entities={"Q1": person("X", "Ådne (actor) 2020.jpg")})
        out = commons.commons_cast([{"actorId": "tmdb:1", "name": "X",
                                     "images": []}], session=s, log=quiet)
        self.assertIn("%C3%85dne_%28actor%29_2020.jpg", out[0]["images"][0])

    def test_max_images_caps_multiple_p18(self):
        s = FakeSession(by_tmdb={"1": "Q1"},
                        entities={"Q1": person("X", "a.jpg", "b.jpg", "c.jpg")})
        out = commons.commons_cast([{"actorId": "tmdb:1", "name": "X",
                                     "images": []}],
                                   session=s, max_images=2, log=quiet)
        self.assertEqual(len(out[0]["images"]), 2)


class NameGuard(unittest.TestCase):
    """A P4985 statement is crowd-maintained. When it points at the wrong
    person, enrolling their photo would put another actor's face in the
    timeline with no error anywhere -- so the item has to name who we asked
    about."""

    def _resolve(self, entity, name):
        s = FakeSession(by_tmdb={"1": "Q1"}, entities={"Q1": entity})
        notes = []
        out = commons.commons_cast(
            [{"actorId": "tmdb:1", "name": name, "images": []}],
            session=s, log=notes.append)
        return out[0]["images"], notes

    def test_wrong_person_is_ignored_and_reported(self):
        urls, notes = self._resolve(person("Kevin Spacey", "Spacey.jpg"),
                                    "Paul Giamatti")
        self.assertEqual(urls, [])
        self.assertTrue(any("Kevin Spacey" in n and "ignored" in n
                            for n in notes))

    def test_accents_and_given_name_variants_are_accepted(self):
        # TMDb bills her "Dola Rashad"; Wikidata labels her "Condola Rashād"
        urls, _ = self._resolve(person("Condola Rashād", "Condola.jpg"),
                                "Dola Rashad")
        self.assertIn("Condola.jpg", urls[0])

    def test_a_stage_name_in_the_aliases_counts(self):
        urls, _ = self._resolve(
            person("Alphonso D'Abruzzo", "Alan.jpg", aliases=["Alan Alda"]),
            "Alan Alda")
        self.assertIn("Alan.jpg", urls[0])

    def test_names_agree_directly(self):
        self.assertTrue(commons.names_agree(person("Maggie Siff"),
                                            "Maggie Siff"))
        self.assertFalse(commons.names_agree(person("Paul Adelstein"),
                                             "Damian Lewis"))
        self.assertFalse(commons.names_agree(person("Anyone"), ""))
        self.assertFalse(commons.names_agree(None, "Anyone"))


class NameFallback(unittest.TestCase):
    def test_non_tmdb_actor_uses_name_search_with_human_check(self):
        # First candidate is a film, second the human.
        s = FakeSession(by_name={"Paul Giamatti": ["Q999", "Q317574"]},
                        entities={
                            "Q999": person("Giamatti", "Poster.jpg",
                                           human=False),
                            "Q317574": person("Paul Giamatti",
                                              "Paul Giamatti.jpg")})
        cast = [{"actorId": "plex:42", "name": "Paul Giamatti",
                 "character": "Chuck", "images": []}]
        out = commons.commons_cast(cast, session=s, log=quiet)
        self.assertIn("Paul_Giamatti.jpg?width=342", out[0]["images"][0])

    def test_no_match_yields_empty_images_and_logs(self):
        notes = []
        cast = [{"actorId": "plex:7", "name": "Nobody Findable",
                 "images": ["old"], "thumb": "old"}]
        out = commons.commons_cast(cast, session=FakeSession(),
                                   log=notes.append)
        self.assertEqual(out[0]["images"], [])
        self.assertIsNone(out[0]["thumb"])
        self.assertTrue(any("Nobody Findable" in n for n in notes))


class AmbiguousReference(unittest.TestCase):
    """Co-portrait protection in enrollment (refs._dominant): a second face
    of comparable size means the photo cannot say which person to enroll."""

    AREA = staticmethod(lambda f: f[0] * f[1])

    def test_two_similar_faces_rejected(self):
        from xray.refs import _dominant
        self.assertIsNone(_dominant([(128, 181), (101, 138)], self.AREA))

    def test_small_background_face_still_passes(self):
        from xray.refs import _dominant
        self.assertEqual(_dominant([(200, 300), (60, 80)], self.AREA),
                         (200, 300))

    def test_single_and_empty(self):
        from xray.refs import _dominant
        self.assertEqual(_dominant([(50, 50)], self.AREA), (50, 50))
        self.assertIsNone(_dominant([], self.AREA))


class Backpressure(unittest.TestCase):
    def test_429_retries_then_succeeds(self):
        class RateLimited:
            calls = 0

            def get(self, url, params=None, timeout=None):
                type(self).calls += 1
                if self.calls == 1:
                    return FakeResp({}, status_code=429,
                                    headers={"Retry-After": "0"})
                return FakeResp({"query": {"search": [{"title": "Q5"}]}})

        s = RateLimited()
        self.assertEqual(commons.qid_for_tmdb_person("1", s), "Q5")
        self.assertEqual(s.calls, 2)


class Batching(unittest.TestCase):
    def test_entities_fetched_in_50_chunks(self):
        qids = [f"Q{i}" for i in range(120)]
        s = FakeSession(entities={q: person(f"P{q}") for q in qids})
        commons.entities(qids, s)
        batches = [c for c in s.calls if c.get("action") == "wbgetentities"]
        self.assertEqual([len(b["ids"].split("|")) for b in batches],
                         [50, 50, 20])


class PhotoCache(unittest.TestCase):
    """Resolution is minutes per cast under Wikimedia's rate limits, and the
    same actors recur across a library, so it is paid once per actor."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cache = Path(self.tmp.name) / commons.CACHE_NAME
        self.addCleanup(self.tmp.cleanup)
        self.cast = [{"actorId": "tmdb:1", "name": "Hit", "images": ["tmdb"]},
                     {"actorId": "tmdb:2", "name": "Miss", "images": ["tmdb"]}]

    def _session(self):
        return FakeSession(by_tmdb={"1": "Q1"},
                           entities={"Q1": person("Hit", "Hit.jpg")})

    def test_cold_resolves_and_writes_cache(self):
        out = commons.cast_with_cache(self.cast, self.cache,
                                      session=self._session(), now=1000.0,
                                      log=quiet)
        self.assertIn("Hit.jpg?width=342", out[0]["images"][0])
        self.assertEqual(out[1]["images"], [])
        cached = json.loads(self.cache.read_text())
        self.assertEqual(cached["tmdb:1"]["images"], out[0]["images"])
        self.assertEqual(cached["tmdb:2"]["images"], [])

    def test_resolution_reports_progress_over_what_it_asks_about(self):
        """This loop is the "finding cast photos" bar. Counted over the STALE
        members, not the whole cast: on a second episode most are cached, and
        a bar that crawls to 20% and stops is the shape of a hang."""
        ticks = []
        commons.cast_with_cache(self.cast, self.cache, session=self._session(),
                                now=1000.0, log=quiet,
                                on_progress=lambda d, t: ticks.append((d, t)))
        self.assertEqual(ticks, [(0, 2), (1, 2)])

    def test_a_fully_cached_cast_reports_nothing_because_it_does_nothing(self):
        commons.cast_with_cache(self.cast, self.cache, session=self._session(),
                                now=1000.0, log=quiet)
        ticks = []
        commons.cast_with_cache(self.cast, self.cache, session=self._session(),
                                now=1000.0, log=quiet,
                                on_progress=lambda d, t: ticks.append((d, t)))
        self.assertEqual(ticks, [])

    def test_warm_asks_the_network_nothing(self):
        commons.cast_with_cache(self.cast, self.cache, session=self._session(),
                                now=1000.0, log=quiet)
        s2 = self._session()
        out = commons.cast_with_cache(self.cast, self.cache, session=s2,
                                      now=1000.0, log=quiet)
        self.assertEqual(s2.calls, [])
        self.assertIn("Hit.jpg?width=342", out[0]["images"][0])

    def test_miss_is_rechecked_after_the_ttl_but_a_hit_is_not(self):
        commons.cast_with_cache(self.cast, self.cache, session=self._session(),
                                now=1000.0, log=quiet)
        s2 = self._session()
        commons.cast_with_cache(self.cast, self.cache, session=s2,
                                now=1000.0 + commons.MISS_TTL_S + 1, log=quiet)
        searches = [c for c in s2.calls if c.get("list") == "search"]
        self.assertEqual(len(searches), 1)
        self.assertIn("=2", searches[0]["srsearch"])

    def test_unreadable_cache_is_treated_as_empty(self):
        self.cache.write_text("{not json")
        out = commons.cast_with_cache(self.cast, self.cache,
                                      session=self._session(), now=1.0,
                                      log=quiet)
        self.assertIn("Hit.jpg?width=342", out[0]["images"][0])


class EnrollmentSourceSetting(unittest.TestCase):
    """The knob is env/config only -- deliberately absent from the setup UI
    while TMDb permission is outstanding."""

    def _source(self, value):
        from xray import keys
        env = {} if value is None else {"XRAY_ENROLLMENT_SOURCE": value}
        with patch.dict(os.environ, env, clear=False):
            if value is None:
                os.environ.pop("XRAY_ENROLLMENT_SOURCE", None)
            return keys.enrollment_source()

    def test_defaults_to_commons(self):
        self.assertEqual(self._source(None), "commons")

    def test_tmdb_is_selectable(self):
        self.assertEqual(self._source("TMDb"), "tmdb")

    def test_nonsense_falls_back_to_commons(self):
        self.assertEqual(self._source("getty"), "commons")


class EnrollmentCastSwap(unittest.TestCase):
    """The swap moves photos and nothing else: the contract must not notice."""

    CAST = [{"actorId": "tmdb:1", "name": "A", "character": "Chuck",
             "thumb": "https://image.tmdb.org/t/p/w342/a.jpg",
             "images": ["https://image.tmdb.org/t/p/w342/a.jpg"]}]

    def test_tmdb_source_passes_the_cast_through_untouched(self):
        from xray.passes import index_title
        with patch.dict(os.environ, {"XRAY_ENROLLMENT_SOURCE": "tmdb"}):
            self.assertIs(index_title.enrollment_cast(self.CAST, Path("/x")),
                          self.CAST)

    def test_commons_source_keeps_identity_and_moves_only_images(self):
        from xray.passes import index_title
        swapped = [{**m, "thumb": "https://commons…/A.jpg",
                    "images": ["https://commons…/A.jpg"]} for m in self.CAST]
        with patch.dict(os.environ, {"XRAY_ENROLLMENT_SOURCE": "commons"}), \
                patch.object(commons, "cast_with_cache",
                             return_value=swapped):
            out = index_title.enrollment_cast(self.CAST, Path("/store"))
        self.assertEqual(out[0]["actorId"], "tmdb:1")
        self.assertEqual(out[0]["character"], "Chuck")
        self.assertIn("commons", out[0]["images"][0])
        # the DOC's cast is the original object, still carrying TMDb thumbs
        self.assertTrue(
            self.CAST[0]["thumb"].startswith("https://image.tmdb.org/"))


if __name__ == "__main__":
    unittest.main()
