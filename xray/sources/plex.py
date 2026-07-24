"""Minimal Plex adapter (raw REST): resolve a title to what the indexer needs.

For each item: the real Plex ratingKey (the key the extension resolves from
/status/sessions), a direct-play HTTP URL ffmpeg can read the frames from (media
is remote, so no local file), and the TMDb id (for cast references + trivia).
"""
from __future__ import annotations

import requests


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
            })
        return out

    def metadata(self, rating_key):
        j = self._get(f"/library/metadata/{rating_key}")
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

    def section_leaves(self, section_key):
        """Every playable item in a section, lightweight: movies directly,
        episodes for show sections. [{ratingKey, type, title, ...}]."""
        sec = next((s for s in self.sections() if s["key"] == str(section_key)
                    or s["title"] == section_key), None)
        if sec is None:
            raise ValueError(f"no library section {section_key!r}")
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
