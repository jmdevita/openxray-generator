"""Settings store + sign-in helpers (stdlib unittest, mocked HTTP).

Covers the UI-complete release's foundations: settings.json seeding/perms/
redaction/precedence, the Plex PIN flow helpers, and Jellyfin Quick Connect
(incl. the GET→POST fallback for newer servers).
"""
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from xray import keys, settings_store as ss
from xray.service import media_auth as ma


class SettingsEnv(unittest.TestCase):
    """Each test gets a fresh temp store via XRAY_SETTINGS."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patch = mock.patch.dict(os.environ, {
            "XRAY_SETTINGS": str(Path(self.tmp.name) / "settings.json"),
            "XRAY_STORE": "",
        })
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()


class TestSettingsStore(SettingsEnv):
    def test_seed_generates_secrets_and_0600(self):
        with mock.patch.dict(os.environ, {"PLEX_ORIGIN": "http://p:32400",
                                          "TMDB_KEY": "abc"}):
            self.assertTrue(ss.ensure_seeded())
        self.assertFalse(ss.ensure_seeded())  # second boot: no-op
        self.assertEqual(ss.get("plex_origin"), "http://p:32400")
        self.assertTrue(ss.get("web_token"))
        self.assertTrue(ss.get("client_id"))
        mode = stat.S_IMODE(ss.settings_path().stat().st_mode)
        self.assertEqual(mode, 0o600)

    def test_update_and_delete_via_empty_string(self):
        ss.save({})
        ss.update({"hub_url": "http://hub", "audd_token": "t1"})
        self.assertEqual(ss.get("hub_url"), "http://hub")
        ss.update({"audd_token": ""})  # empty deletes
        self.assertEqual(ss.get("audd_token"), "")
        self.assertNotIn("audd_token", ss.load())

    def test_redacted_hides_secrets_and_web_token(self):
        ss.save({"web_token": "supersecret", "plex_token": "tok123456",
                 "plex_origin": "http://p"})
        red = ss.redacted()
        self.assertNotIn("web_token", red)
        self.assertEqual(red["plex_token"], "•••3456")
        self.assertEqual(red["plex_origin"], "http://p")

    def test_corrupt_file_is_treated_as_empty(self):
        ss.settings_path().write_text("{ not json")
        self.assertEqual(ss.load(), {})

    def test_no_path_configured_is_inert(self):
        with mock.patch.dict(os.environ, {"XRAY_SETTINGS": "", "XRAY_STORE": ""}):
            self.assertIsNone(ss.settings_path())
            self.assertEqual(ss.load(), {})
            self.assertFalse(ss.ensure_seeded())


class TestSearchEndpoint(SettingsEnv):
    def test_candidates_normalized_episode_labels_and_filtering(self):
        from xray.service import orchestrator as o

        class FakeSource:
            def search(self, q, limit=12):
                return [
                    {"ratingKey": "1", "type": "movie", "title": "Shrek 2",
                     "year": 2004},
                    {"ratingKey": "2", "type": "episode", "title": "Chapter One",
                     "grandparentTitle": "Stranger Things", "season": 1,
                     "episode": 1},
                    {"ratingKey": "3", "type": "artist", "title": "Smash Mouth"},
                ]

        with mock.patch.object(o, "_origin", return_value="http://p"), \
                mock.patch.object(o, "_source", return_value=FakeSource()):
            out = o.api_search("shrek")
        labels = [r["label"] for r in out["results"]]
        self.assertEqual(labels, ["Shrek 2",
                                  "Stranger Things S01E01 · Chapter One"])
        self.assertEqual(out["results"][0]["year"], 2004)  # artist filtered out


class TestWebTokenOverride(SettingsEnv):
    def test_env_override_beats_settings(self):
        ss.save({"web_token": "generated-one"})
        from xray.service import orchestrator as o
        with mock.patch.dict(os.environ, {"XRAY_WEB_TOKEN": "pinned-by-env"}):
            self.assertEqual(o._web_token(), "pinned-by-env")
        with mock.patch.dict(os.environ, {"XRAY_WEB_TOKEN": ""}):
            self.assertEqual(o._web_token(), "generated-one")


class TestKeysPrecedence(SettingsEnv):
    def test_settings_beat_env(self):
        ss.save({"tmdb_key": "from-settings"})
        with mock.patch.dict(os.environ, {"TMDB_KEY": "from-env"}):
            self.assertEqual(keys.tmdb_key(), "from-settings")

    def test_env_still_works_without_settings_or_key_files(self):
        ss.save({})
        # Point the key-file tier at an empty dir: the dev repo root has
        # real key files that would (correctly) win otherwise.
        with mock.patch.object(keys, "ROOT", Path(self.tmp.name)), \
                mock.patch.dict(os.environ, {"AUDD_API_TOKEN": "from-env"}):
            self.assertEqual(keys.audd_token(), "from-env")

    def test_key_files_beat_env(self):
        ss.save({})
        (Path(self.tmp.name) / ".auddtoken").write_text("from-file\n")
        with mock.patch.object(keys, "ROOT", Path(self.tmp.name)), \
                mock.patch.dict(os.environ, {"AUDD_API_TOKEN": "from-env"}):
            self.assertEqual(keys.audd_token(), "from-file")


class FakeResponse:
    def __init__(self, payload, status=200):
        self._payload, self.status_code = payload, status

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class TestPlexAuth(unittest.TestCase):
    def test_create_pin_builds_auth_url(self):
        with mock.patch("requests.post",
                        return_value=FakeResponse({"id": 7, "code": "abc123"})) as post:
            out = ma.plex_create_pin("cid-1")
        self.assertEqual(out["id"], 7)
        self.assertIn("clientID=cid-1", out["authUrl"])
        self.assertIn("code=abc123", out["authUrl"])
        url, kwargs = post.call_args[0][0], post.call_args[1]
        self.assertEqual(url, "https://plex.tv/api/v2/pins")
        self.assertEqual(kwargs["data"], {"strong": "true"})
        self.assertEqual(kwargs["headers"]["X-Plex-Client-Identifier"], "cid-1")

    def test_check_pin_none_until_claimed(self):
        with mock.patch("requests.get",
                        return_value=FakeResponse({"authToken": None})):
            self.assertIsNone(ma.plex_check_pin("cid", 7))
        with mock.patch("requests.get",
                        return_value=FakeResponse({"authToken": "tok"})):
            self.assertEqual(ma.plex_check_pin("cid", 7), "tok")

    def test_servers_filters_and_orders_connections(self):
        payload = [
            {"name": "NAS", "provides": "server", "product": "PMS",
             "connections": [
                 {"uri": "https://relay.example", "local": False, "relay": True},
                 {"uri": "https://wan.example", "local": False, "relay": False},
                 {"uri": "http://192.168.1.2:32400", "local": True, "relay": False},
             ]},
            {"name": "Player", "provides": "client", "connections": []},
        ]
        with mock.patch("requests.get", return_value=FakeResponse(payload)):
            servers = ma.plex_servers("cid", "tok")
        self.assertEqual(len(servers), 1)  # clients filtered out
        uris = [c["uri"] for c in servers[0]["connections"]]
        self.assertEqual(uris[0], "http://192.168.1.2:32400")  # local first
        self.assertEqual(uris[-1], "https://relay.example")    # relay last


class TestJellyfinAuth(unittest.TestCase):
    def test_quickconnect_initiate_falls_back_to_post_on_405(self):
        get405 = FakeResponse({}, status=405)
        ok = FakeResponse({"Code": "424242", "Secret": "s3cret"})
        with mock.patch("requests.get", return_value=get405), \
                mock.patch("requests.post", return_value=ok) as post:
            out = ma.jf_quickconnect_initiate("http://jf:8096/", "cid")
        self.assertEqual(out, {"code": "424242", "secret": "s3cret"})
        self.assertEqual(post.call_args[0][0], "http://jf:8096/QuickConnect/Initiate")

    def test_quickconnect_exchange_extracts_token_and_user(self):
        payload = {"AccessToken": "jf-tok", "User": {"Id": "u-9"}}
        with mock.patch("requests.post", return_value=FakeResponse(payload)):
            out = ma.jf_quickconnect_exchange("http://jf:8096", "cid", "s")
        self.assertEqual(out, {"token": "jf-tok", "user_id": "u-9"})

    def test_password_auth_sends_mediabrowser_header(self):
        payload = {"AccessToken": "t", "User": {"Id": "u"}}
        with mock.patch("requests.post", return_value=FakeResponse(payload)) as post:
            ma.jf_password_auth("http://jf:8096", "cid-9", "julian", "pw")
        headers = post.call_args[1]["headers"]
        self.assertIn('DeviceId="cid-9"', headers["Authorization"])
        self.assertEqual(headers["Authorization"], headers["X-Emby-Authorization"])
        self.assertEqual(post.call_args[1]["json"], {"Username": "julian", "Pw": "pw"})


if __name__ == "__main__":
    unittest.main()
