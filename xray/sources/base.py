"""The MediaSource seam: the pipeline talks to a backend through this, so Plex
and Jellyfin are peers rather than one native path + one bolt-on mapping pass.

A source resolves a backend item id (Plex ratingKey, Jellyfin itemId) to the
common *item dict* the passes consume, and declares its manifest namespace via
`key_prefix` (`plex` / `jellyfin`). Adding a backend is one module implementing
this Protocol plus one line in `open_source` below; the pipeline never changes.

The item dict `resolve` returns (its `ratingKey` field is the backend item id,
kept under that name for the timeline schema):

    ratingKey    str   backend item id (Plex ratingKey / Jellyfin itemId)
    type         str   "movie" | "episode"
    title        str
    downloadUrl  str   direct-play/stream URL ffmpeg can read frames+audio from
    container    str?  media container (optional)
    file         str?  server-side path (optional, diagnostics)
    durationMs   int?  runtime in milliseconds
  episodes add:
    season          int
    episode         int
    grandparentTitle str   series title
    showTmdbId      str?   the series' TMDb id (for cast refs)
    showRatingKey   str?   the series' backend id
  movies add:
    tmdbId       str?  the movie's TMDb id (for cast refs)
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class MediaSource(Protocol):
    """What the indexer/music passes need from a media backend."""

    #: manifest namespace for this backend's ids ("plex" / "jellyfin")
    key_prefix: str

    def search(self, query: str, limit: int = 12) -> list[dict]:
        """Library search → lightweight candidates to pick a ratingKey from."""
        ...

    def section_leaves(self, section_key: str) -> list[dict]:
        """Every playable item in a section (movies / episodes), lightweight."""
        ...

    def resolve(self, item_id: str) -> dict:
        """Everything the indexer needs for one item: see the item dict above."""
        ...


def open_source(backend: str, origin: str, token: str, *,
                user_id: str | None = None) -> MediaSource:
    """Construct the backend the pipeline should index against.

    `backend` selects the adapter; `origin`/`token` are that backend's server
    URL and auth. `user_id` is Jellyfin-only (ignored by Plex)."""
    backend = (backend or "plex").lower()
    if backend == "plex":
        from .plex import PlexServer
        return PlexServer(origin, token)
    if backend == "jellyfin":
        from .jellyfin import JellyfinServer
        return JellyfinServer(origin, token, user_id=user_id)
    raise ValueError(f"unknown backend {backend!r} (want: plex, jellyfin)")
