"""The orchestrator's planning endpoints and the dashboard shell.

The dashboard is stateful: setup is all you get until a run could actually
succeed. That gate, and the plan endpoint the tier cards are priced from,
are what these cover.

Route handlers are called directly rather than through a TestClient: that
needs httpx, which is not a dependency of this project and is not worth
becoming one to assert on return values from plain functions. The auth gate
is middleware and is covered in test_setup_auth.
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("XRAY_STORE", tempfile.mkdtemp())

from fastapi import HTTPException  # noqa: E402

from xray.service import orchestrator as O  # noqa: E402


class FakeSource:
    key_prefix = "fake"

    def sections(self):
        return [{"key": "1", "title": "Movies", "type": "movie"}]

    def content_ids(self, section_key):
        if section_key != "Movies":
            raise ValueError(f"no library section {section_key!r}")
        return {"a": "tmdb-movie-1", "b": None}


class Base(unittest.TestCase):
    def setUp(self):
        self.ctx = [
            mock.patch.object(O, "_origin", lambda: "http://server"),
            mock.patch.object(O, "_source", FakeSource),
            mock.patch("xray.plan.hub_catalog_for", return_value={}),
        ]
        for c in self.ctx:
            c.start()
        self.addCleanup(lambda: [c.stop() for c in self.ctx])


class TestPlanEndpoint(Base):
    def test_plan_prices_both_levels_in_one_request(self):
        body = O.api_plan(library="Movies")
        self.assertEqual(body["total"], 2)
        self.assertEqual(body["unidentified"], 1)
        self.assertEqual(set(body["levels"]), {"0", "1"})
        # A full index must never be cheaper than a seed of the same work.
        self.assertGreater(body["levels"]["1"]["seconds"][1],
                           body["levels"]["0"]["seconds"][1])

    def test_unknown_library_is_404_not_500(self):
        with self.assertRaises(HTTPException) as ctx:
            O.api_plan(library="Nope")
        self.assertEqual(ctx.exception.status_code, 404)

    def test_plan_needs_a_configured_server(self):
        with mock.patch.object(O, "_origin", lambda: ""):
            with self.assertRaises(HTTPException) as ctx:
                O.api_plan(library="Movies")
        self.assertEqual(ctx.exception.status_code, 503)

    def test_libraries_lists_sections(self):
        self.assertEqual(O.api_libraries(),
                         {"sections": [{"key": "1", "title": "Movies",
                                        "type": "movie"}]})


class TestSetupGate(Base):
    def test_not_ready_without_a_tmdb_key(self):
        with mock.patch("xray.keys.tmdb_key", return_value=""):
            self.assertFalse(O.api_setup()["ready"])

    def test_not_ready_without_a_server(self):
        with mock.patch.object(O, "_origin", lambda: ""), \
                mock.patch("xray.keys.tmdb_key", return_value="abc"):
            self.assertFalse(O.api_setup()["ready"])

    def test_ready_with_both(self):
        with mock.patch("xray.keys.tmdb_key", return_value="abc"):
            self.assertTrue(O.api_setup()["ready"])


class TestJobShape(unittest.TestCase):
    def setUp(self):
        O._jobs.clear()
        O._queue.clear()

    def test_list_carries_progress_without_the_log(self):
        job = O._submit(O.RunRequest(library="Movies", level=0))
        job["total"] = 3
        job["summary"] = [{"key": "k", "title": "T", "steps": {}}]
        job["log"] = ["noisy"] * 500
        row = O.api_jobs()[0]
        self.assertEqual((row["total"], row["done"]), (3, 1))
        self.assertEqual(row["level"], 0)
        self.assertNotIn("log", row)

    def test_detail_can_omit_the_log_for_polling(self):
        O._submit(O.RunRequest(library="Movies"))
        full = O.api_jobs(id=1)
        light = O.api_jobs(id=1, log=0)
        self.assertIn("log", full)
        self.assertNotIn("log", light)
        self.assertIn("summary", light)  # per-title rows survive

    def test_missing_job_is_404(self):
        with self.assertRaises(HTTPException) as ctx:
            O.api_jobs(id=99)
        self.assertEqual(ctx.exception.status_code, 404)


class TestDashboardShell(unittest.TestCase):
    def setUp(self):
        self.html = O.dashboard()

    def test_carries_the_view_containers(self):
        for hook in ("setupView", "runView", "jobView", "storeView"):
            self.assertIn(f'id="{hook}"', self.html)

    def test_the_old_dead_run_form_is_gone(self):
        # The point of the rebuild: no ratingKey box, no max=3, and no bare
        # level-0 checkbox offered before setup could possibly succeed.
        self.assertNotIn('placeholder="ratingKey"', self.html)
        self.assertNotIn('value="3"', self.html)
        self.assertNotIn('name="seed"', self.html)

    def test_library_titles_are_escaped_before_they_reach_the_page(self):
        # Titles are third-party strings; the renderer must not interpolate
        # them raw.
        self.assertIn("const esc =", self.html)
        self.assertIn("esc(x.label)", self.html)

    def test_braces_survived_the_move_off_f_strings(self):
        # The page is built from plain strings; an f-string would need every
        # CSS and JS brace doubled. Nested CSS legitimately ends in "}}", so
        # this pins a single-brace JS object literal instead: it would read
        # {{'content-type': ...}} if anyone reintroduced the f-string.
        self.assertIn("headers:{'content-type':'application/json'}", self.html)
        self.assertIn("@media(prefers-color-scheme:dark)", self.html)

    def test_hidden_beats_the_flex_layout(self):
        # .sec sets display:flex at the same specificity as the UA's
        # [hidden]{display:none}, so without an explicit override the setup
        # gate renders the store and run views anyway. Silent when it breaks.
        self.assertIn("[hidden]{display:none!important}", self.html)

    def test_hub_is_its_own_setup_step_not_buried_under_audd(self):
        self.assertIn("'Hub',", self.html)
        self.assertIn("'AudD token', 'optional'", self.html)

    def test_hub_is_not_a_paste_a_url_field(self):
        # It ships configured; offering a text box invites pointing the
        # stack at somebody else's hub. Dev override stays on XRAY_HUB_URL.
        self.assertNotIn('id="hubUrl"', self.html)

    def test_no_claim_of_a_recurring_audd_free_tier(self):
        # AudD's 300 requests are a one-time signup allowance; the monthly cap
        # is ours. Copy that blurs the two invites a surprise bill.
        for wrong in ("free tier", "calls/month free", "resumes next month"):
            self.assertNotIn(wrong, self.html)
        self.assertIn("Your spend cap", self.html)

    def test_music_is_separable_from_the_full_index(self):
        # A token being present must not force paying for music.
        self.assertIn("function runSkip(", self.html)
        self.assertIn("'music' : ''", self.html)

    def test_login_page_points_at_the_logs(self):
        self.assertIn("docker compose logs orchestrator", O.login_page())


if __name__ == "__main__":
    unittest.main()


class TestFavicon(unittest.TestCase):
    """The tab icon is inlined, so it has no route and no asset to ship."""

    def test_both_pages_carry_it(self):
        """Sign-in shares _HEAD with the dashboard; an unbranded login tab is
        the first thing a new user sees."""
        for page in (O.dashboard(), O._LOGIN_PAGE):
            self.assertIn('rel="icon"', page)
            self.assertIn("data:image/svg+xml,", page)

    def test_the_data_uri_decodes_back_to_the_svg(self):
        import re
        import urllib.parse
        href = re.search(r'href="data:image/svg\+xml,([^"]+)"', O._FAVICON)
        self.assertIsNotNone(href, "favicon href not found")
        self.assertEqual(urllib.parse.unquote(href.group(1)), O._FAVICON_SVG)

    def test_it_is_the_project_mark_not_a_new_one(self):
        """Same staggered geometry as the hub's favicon, so the two read as
        one project; only the fills differ."""
        for bar in ("x='14' y='17' width='20'",
                    "x='22' y='29' width='28'",
                    "x='14' y='41' width='14'"):
            self.assertIn(bar, O._FAVICON_SVG, bar)
        self.assertIn("#2f5d55", O._FAVICON_SVG)

    def test_tracks_stay_legible_at_16px(self):
        """Below ~40% the unfilled tracks disappear in a tab strip."""
        import re
        for value in re.findall(r"opacity='([\d.]+)'", O._FAVICON_SVG):
            self.assertGreaterEqual(float(value), 0.4, value)


class TestPageTitles(unittest.TestCase):
    """The hub's tab says "OpenXray Hub". Both are open at once whenever
    someone is contributing, so the generator has to name itself too."""

    def test_the_generator_names_itself(self):
        self.assertIn("<title>OpenXray Generator</title>", O.dashboard())

    def test_sign_in_too(self):
        self.assertIn("<title>OpenXray Generator sign-in</title>", O._LOGIN_PAGE)

    def test_no_page_is_titled_just_openxray(self):
        """Ambiguous against the hub in a tab strip."""
        for page in (O.dashboard(), O._LOGIN_PAGE):
            self.assertNotIn("<title>OpenXray</title>", page)

    def test_the_api_docs_agree(self):
        """Was a third name for the same app."""
        self.assertEqual(O.app.title, "OpenXray Generator")
