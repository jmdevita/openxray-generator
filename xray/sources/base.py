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

import re
from typing import Protocol, runtime_checkable

_ITEM_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def check_item_id(item_id: str) -> str:
    """Return a backend item id, or raise ValueError.

    These ids become URL PATH segments against the media server, and requests
    normalises "../" instead of encoding it, so an unchecked id turns a
    metadata lookup into an arbitrary call against that server carrying the
    operator's token. Plex ids are numeric and Jellyfin's are hex, so an
    alphanumeric allowlist costs nothing and closes the class outright."""
    if not _ITEM_ID_RE.match(str(item_id or "")):
        raise ValueError(f"bad backend item id: {item_id!r}")
    return str(item_id)


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

    def series_leaves(self, series_id: str) -> list[dict]:
        """Every episode of ONE series, lightweight.

        Sits between a single episode and a whole library: "index this show"
        is the natural unit for TV, and without it a 60-episode run means 60
        separate queue actions."""
        ...

    def content_ids(self, section_key: str) -> dict[str, str | None]:
        """{backend item id: content id or None} for a whole section.

        Identity in bulk, and it has to stay CHEAP: this exists so the UI can
        answer "what do I already have, and what would the rest cost?" before
        the user commits to a run. Implementations get it in one or two
        requests by asking the backend for external ids inline with the
        listing; resolving 400 titles one at a time is what it avoids.

        None means the backend has no TMDb match for that item, so it has no
        content identity and run_title() will skip it."""
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
