"""Minimal Plex adapter (raw REST): resolve a title to what the indexer needs.

For each item: the real Plex ratingKey (the key the extension resolves from
/status/sessions), a direct-play HTTP URL ffmpeg can read the frames from (media
is remote, so no local file), and the TMDb id (for cast references + trivia).
"""
from __future__ import annotations

import requests

from .base import check_item_id


class PlexServer:
    key_prefix = "plex"  # manifest namespace (MediaSource seam)

    def __init__(self, origin, token):
        self.origin = origin.rstrip("/")
        self.token = token
        self.s = requests.Session()
        self.s.headers.update({"Accept": "application/json", "X-Plex-Token": token})

    def _get(self, path, **params):
        r = self.s.get(self.origin + path, params=params, timeout=30)
        r.raise_for_status()
        return r.json()

    def search(self, query, limit=12):
        """Search the library; returns lightweight candidates to pick from."""
        j = self._get("/search", query=query, limit=limit)
        out = []
        for m in j.get("MediaContainer", {}).get("Metadata", []):
            out.append({
                "ratingKey": str(m.get("ratingKey")),
                "type": m.get("type"),
                "title": m.get("title"),
                "year": m.get("year"),
                "grandparentTitle": m.get("grandparentTitle"),
                "season": m.get("parentIndex"),
                "episode": m.get("index"),
                # Lets a caller offer "the whole show" next to one episode.
                "seriesId": (str(m["grandparentRatingKey"])
                             if m.get("grandparentRatingKey") else None),
            })
        return out

    def metadata(self, rating_key):
        j = self._get(f"/library/metadata/{check_item_id(rating_key)}")
        return j["MediaContainer"]["Metadata"][0]

    @staticmethod
    def _tmdb_from_guids(md):
        for g in md.get("Guid", []) or []:
            gid = g.get("id", "")
            if gid.startswith("tmdb://"):
                return gid.split("tmdb://", 1)[1]
        return None

    def download_url(self, part):
        # direct-play URL ffmpeg can stream frames from
        return f"{self.origin}{part['key']}?X-Plex-Token={self.token}"

    def sections(self):
        """Library sections: [{key, title, type}] (type: movie|show)."""
        j = self._get("/library/sections")
        return [{"key": str(d.get("key")), "title": d.get("title"),
                 "type": d.get("type")}
                for d in j.get("MediaContainer", {}).get("Directory", [])]

    def _section(self, section_key):
        sec = next((s for s in self.sections() if s["key"] == str(section_key)
                    or s["title"] == section_key), None)
        if sec is None:
            raise ValueError(f"no library section {section_key!r}")
        return sec

    def section_leaves(self, section_key):
        """Every playable item in a section, lightweight: movies directly,
        episodes for show sections. [{ratingKey, type, title, ...}]."""
        sec = self._section(section_key)
        path = (f"/library/sections/{sec['key']}/all"
                if sec["type"] == "movie"
                else f"/library/sections/{sec['key']}/all?type=4")
        j = self._get(path)
        out = []
        for m in j.get("MediaContainer", {}).get("Metadata", []):
            out.append({
                "ratingKey": str(m.get("ratingKey")),
                "type": m.get("type"),
                "title": m.get("title"),
                "grandparentTitle": m.get("grandparentTitle"),
                "season": m.get("parentIndex"),
                "episode": m.get("index"),
            })
        return out

    def series_leaves(self, series_id):
        """Every episode of one show (MediaSource seam). allLeaves flattens
        the season tree, so this is one request whatever the show's shape."""
        j = self._get(
            f"/library/metadata/{check_item_id(series_id)}/allLeaves")
        return [{"ratingKey": str(m.get("ratingKey")),
                 "type": m.get("type"),
                 "title": m.get("title"),
                 "grandparentTitle": m.get("grandparentTitle"),
                 "season": m.get("parentIndex"),
                 "episode": m.get("index")}
                for m in j.get("MediaContainer", {}).get("Metadata", [])]

    def content_ids(self, section_key):
        """{ratingKey: contentId|None} for a whole section (MediaSource seam).

        `includeGuids=1` puts the external ids on the section listing itself,
        so identity for a 400-title library is one round trip rather than 400.
        Episodes are the awkward case: their own guid is the EPISODE's, while
        a content id needs the series' TMDb id plus season/episode, so show
        sections take a second pass over the shows to build that map."""
        sec = self._section(section_key)
        base = f"/library/sections/{sec['key']}/all"

        if sec["type"] == "movie":
            items = (self._get(base, includeGuids=1)
                     .get("MediaContainer", {}).get("Metadata", []))
            if not self._guids_honored(items):
                return self._content_ids_slow(items)
            return {str(m.get("ratingKey")): self._movie_cid(m) for m in items}

        shows = (self._get(base, type=2, includeGuids=1)
                 .get("MediaContainer", {}).get("Metadata", []))
        if not self._guids_honored(shows):
            # One slow pass over SHOWS still beats one per episode.
            show_tmdb = {str(s.get("ratingKey")):
                         self._tmdb_from_guids(self.metadata(s.get("ratingKey")))
                         for s in shows}
        else:
            show_tmdb = {str(s.get("ratingKey")): self._tmdb_from_guids(s)
                         for s in shows}

        from .. import store as st
        out = {}
        for m in (self._get(base, type=4)
                  .get("MediaContainer", {}).get("Metadata", [])):
            tmdb = show_tmdb.get(str(m.get("grandparentRatingKey")))
            season, episode = m.get("parentIndex"), m.get("index")
            out[str(m.get("ratingKey"))] = (
                st.episode_content_id(tmdb, season, episode)
                if tmdb and season is not None and episode is not None
                else None)
        return out

    def _guids_honored(self, items) -> bool:
        """Did the server actually act on includeGuids=1?

        A listing with no Guid anywhere is ambiguous: either the server
        ignored the parameter (old build) or nothing in the library is
        matched. Probing a single item tells them apart, which matters
        because guessing wrong either wastes N requests or reports a whole
        library as unidentifiable."""
        if not items or any(m.get("Guid") for m in items):
            return True
        try:
            probe = self.metadata(items[0].get("ratingKey"))
        except Exception:  # noqa: BLE001 (probe is advisory; assume honored)
            return True
        return not probe.get("Guid")

    def _content_ids_slow(self, items) -> dict:
        """Per-item metadata fallback for servers without includeGuids."""
        return {str(m.get("ratingKey")):
                self._movie_cid(self.metadata(m.get("ratingKey")))
                for m in items}

    def _movie_cid(self, md):
        from .. import store as st
        tmdb = self._tmdb_from_guids(md)
        return st.movie_content_id(tmdb) if tmdb else None

    def resolve(self, rating_key):
        """Everything the indexer needs for one item (movie or episode)."""
        md = self.metadata(rating_key)
        typ = md.get("type")
        part = md["Media"][0]["Part"][0]
        info = {
            "ratingKey": str(rating_key),
            "type": typ,
            "title": md.get("title"),
            "downloadUrl": self.download_url(part),
            "container": part.get("container"),
            "file": part.get("file"),
            "durationMs": md.get("duration"),
        }
        if typ == "episode":
            info["season"] = md.get("parentIndex")
            info["episode"] = md.get("index")
            info["grandparentTitle"] = md.get("grandparentTitle")
            gpk = md.get("grandparentRatingKey")
            show_md = self.metadata(gpk) if gpk else {}
            info["showTmdbId"] = self._tmdb_from_guids(show_md)
            info["showRatingKey"] = str(gpk) if gpk else None
        else:
            info["tmdbId"] = self._tmdb_from_guids(md)
        return info
