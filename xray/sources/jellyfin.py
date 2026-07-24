"""Jellyfin adapter: the MediaSource peer to Plex (see sources/base.py).

Resolves a Jellyfin itemId to the common item dict the indexer/music passes
consume, and streams direct-play frames+audio the same way the Plex source
does. Written against the Jellyfin 10.10 API; like the old map pass, not yet
run against a live server: the shape is faithful to the docs, not battle-worn.

Ticks note: Jellyfin `RunTimeTicks` are 100-nanosecond units → ms = ticks / 1e4.
"""
from __future__ import annotations

import requests


class JellyfinServer:
    key_prefix = "jellyfin"  # manifest namespace (MediaSource seam)

    def __init__(self, origin: str, token: str, user_id: str | None = None):
        self.origin = origin.rstrip("/")
        self.token = token
        self.user_id = user_id
        self.s = requests.Session()
        self.s.headers.update({"Accept": "application/json",
                               "X-Emby-Token": token})

    def _get(self, path: str, **params) -> dict:
        if self.user_id:
            params.setdefault("userId", self.user_id)
        r = self.s.get(self.origin + path, params=params, timeout=30)
        r.raise_for_status()
        return r.json()

    # --- id helpers (shared with the jellyfin-map pass) -------------------

    @staticmethod
    def _tmdb_of(item: dict) -> str | None:
        tmdb = (item.get("ProviderIds") or {}).get("Tmdb")
        return str(tmdb) if tmdb else None

    def items_by_tmdb(self, item_type: str) -> dict[str, str]:
        """{tmdbId: itemId} for every item of a type (Movie / Series)."""
        data = self._get("/Items", IncludeItemTypes=item_type, Recursive="true",
                         Fields="ProviderIds")
        out = {}
        for it in data.get("Items", []):
            tmdb = self._tmdb_of(it)
            if tmdb:
                out[tmdb] = it["Id"]
        return out

    def episode_id(self, series_id: str, season: int, episode: int) -> str | None:
        data = self._get(f"/Shows/{series_id}/Episodes")
        for ep in data.get("Items", []):
            if ep.get("ParentIndexNumber") == season and ep.get("IndexNumber") == episode:
                return ep["Id"]
        return None

    # --- MediaSource contract --------------------------------------------

    def download_url(self, item_id: str) -> str:
        """Direct-play stream ffmpeg can read frames + audio from."""
        return (f"{self.origin}/Videos/{item_id}/stream"
                f"?static=true&api_key={self.token}")

    def _item(self, item_id: str) -> dict:
        data = self._get("/Items", ids=item_id, Recursive="true",
                         Fields="ProviderIds,MediaSources,Path")
        items = data.get("Items", [])
        if not items:
            raise ValueError(f"no Jellyfin item {item_id!r}")
        return items[0]

    @staticmethod
    def _normalize(it: dict) -> dict:
        typ = "episode" if it.get("Type") == "Episode" else "movie"
        return {
            "ratingKey": str(it.get("Id")),
            "type": typ,
            "title": it.get("Name"),
            "year": it.get("ProductionYear"),
            "grandparentTitle": it.get("SeriesName"),
            "season": it.get("ParentIndexNumber"),
            "episode": it.get("IndexNumber"),
        }

    def search(self, query: str, limit: int = 12) -> list[dict]:
        data = self._get("/Items", searchTerm=query, Recursive="true",
                         IncludeItemTypes="Movie,Episode", Limit=limit,
                         Fields="ProviderIds")
        return [self._normalize(it) for it in data.get("Items", [])]

    def sections(self) -> list[dict]:
        """Top-level libraries: [{key, title, type: movie|show}]."""
        data = self._get("/Library/MediaFolders")
        out = []
        for d in data.get("Items", []):
            ct = (d.get("CollectionType") or "").lower()
            typ = "movie" if ct == "movies" else "show" if ct == "tvshows" else ct
            out.append({"key": str(d.get("Id")), "title": d.get("Name"),
                        "type": typ})
        return out

    def section_leaves(self, section_key: str) -> list[dict]:
        sec = next((s for s in self.sections()
                    if s["key"] == str(section_key) or s["title"] == section_key),
                   None)
        if sec is None:
            raise ValueError(f"no library section {section_key!r}")
        item_type = "Episode" if sec["type"] == "show" else "Movie"
        data = self._get("/Items", ParentId=sec["key"], Recursive="true",
                         IncludeItemTypes=item_type, Fields="ProviderIds")
        return [self._normalize(it) for it in data.get("Items", [])]

    def resolve(self, item_id: str) -> dict:
        """Everything the indexer needs for one item (movie or episode)."""
        it = self._item(item_id)
        ticks = it.get("RunTimeTicks") or 0
        info = {
            "ratingKey": str(it.get("Id")),
            "type": "episode" if it.get("Type") == "Episode" else "movie",
            "title": it.get("Name"),
            "downloadUrl": self.download_url(item_id),
            "container": it.get("Container"),
            "file": it.get("Path"),
            "durationMs": (ticks // 10_000) or None,
        }
        if info["type"] == "episode":
            info["season"] = it.get("ParentIndexNumber")
            info["episode"] = it.get("IndexNumber")
            info["grandparentTitle"] = it.get("SeriesName")
            series_id = it.get("SeriesId")
            info["showRatingKey"] = str(series_id) if series_id else None
            info["showTmdbId"] = (self._tmdb_of(self._item(series_id))
                                  if series_id else None)
        else:
            info["tmdbId"] = self._tmdb_of(it)
        return info


# Back-compat: the old map pass imported `Jellyfin`; keep the name as an alias.
Jellyfin = JellyfinServer
