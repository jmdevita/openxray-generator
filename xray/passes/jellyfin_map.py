"""Jellyfin mapping pass: add `jellyfin:<itemId>` manifest entries.

Now that Jellyfin is a native MediaSource (sources/jellyfin.py), indexing or a
hub fetch stamps `jellyfin:` keys on its own. This pass remains for the
hub-only consumer: someone who pulls finished timelines from the community hub
and just needs to point their Jellyfin itemIds at them without indexing.

Matches canonical timelines to Jellyfin items by TMDb provider id (movies
directly; episodes via the series' provider id + season/episode numbers).
Written against the Jellyfin 10.10 API; not yet run against a live server.
"""
from __future__ import annotations

import re
from pathlib import Path

from .. import store as st
from ..sources.jellyfin import JellyfinServer

MOVIE_RE = re.compile(r"^tmdb-movie-(\d+)$")
EPISODE_RE = re.compile(r"^tmdb-tv-(\d+)-s(\d{2})e(\d{2})$")


def run(store_dir: Path, *, server: str, api_key: str,
        user_id: str | None = None, dry_run: bool = False) -> dict:
    timelines = [f for f in st.timeline_files(store_dir) if f.name.startswith("tmdb-")]
    jf = JellyfinServer(server, api_key, user_id=user_id)
    movies = series = None
    mapped = missed = 0

    for f in timelines:
        content_id = f.stem
        item_id = None
        if m := MOVIE_RE.match(content_id):
            movies = movies if movies is not None else jf.items_by_tmdb("Movie")
            item_id = movies.get(m.group(1))
        elif m := EPISODE_RE.match(content_id):
            series = series if series is not None else jf.items_by_tmdb("Series")
            series_id = series.get(m.group(1))
            if series_id:
                item_id = jf.episode_id(series_id, int(m.group(2)), int(m.group(3)))
        else:
            continue

        if item_id is None:
            print(f"{content_id}: no Jellyfin match")
            missed += 1
            continue
        mapped += 1
        if dry_run:
            print(f"{content_id}: would map jellyfin:{item_id}")
        else:
            st.map_lookup(store_dir, f"jellyfin:{item_id}", content_id)
            print(f"{content_id}: mapped jellyfin:{item_id}")

    print(f"\njellyfin: {mapped} mapped, {missed} unmatched"
          + (" (dry run)" if dry_run else ""))
    return {"mapped": mapped, "missed": missed}
