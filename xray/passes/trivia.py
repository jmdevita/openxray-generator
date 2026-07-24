"""Trivia pass: Wikidata + Wikipedia facts, derived from the contentId.

tmdb-movie-769 → movie facts; tmdb-tv-62852-s01e01 → episode facts topped up
with show-level facts. Skips timelines whose provenance.trivia is fresh.
"""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from .. import store as st
from ..sources.wiki import episode_trivia, title_trivia

BLOCK_VERSION = "wiki-v1"
MAX_FACTS = 12

MOVIE_RE = re.compile(r"^tmdb-movie-(\d+)$")
EPISODE_RE = re.compile(r"^tmdb-tv-(\d+)-s(\d{2})e(\d{2})$")


def fetch_trivia(content_id: str, key: str) -> list[dict] | None:
    if m := MOVIE_RE.match(content_id):
        return title_trivia("movie", int(m.group(1)), key, max_facts=MAX_FACTS)
    if m := EPISODE_RE.match(content_id):
        tv_id, season, episode = int(m.group(1)), int(m.group(2)), int(m.group(3))
        facts = episode_trivia(tv_id, season, episode, key, max_facts=MAX_FACTS)
        if len(facts) < MAX_FACTS:
            seen = {f["text"] for f in facts}
            for f in title_trivia("tv", tv_id, key, max_facts=MAX_FACTS):
                if f["text"] not in seen:
                    facts.append(f)
                    seen.add(f["text"])
                if len(facts) >= MAX_FACTS:
                    break
        return facts
    return None


def is_fresh(doc: dict, max_age_days: float) -> bool:
    stamp = (doc.get("provenance") or {}).get("trivia") or {}
    try:
        gen = datetime.fromisoformat(str(stamp.get("generated")).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return False
    return (datetime.now(timezone.utc) - gen).total_seconds() < max_age_days * 86400


def run(store_dir: Path, tmdb_key: str, keys: list[str] | None = None, *,
        refresh_days: float = 90) -> dict:
    files = st.resolve_timelines(store_dir, keys)
    n_updated = n_skipped = 0
    for f in files:
        doc = json.loads(f.read_text())
        content_id = doc.get("contentId") or f.stem
        if is_fresh(doc, refresh_days):
            print(f"{f.name}: trivia fresh; skip")
            n_skipped += 1
            continue
        facts = fetch_trivia(content_id, tmdb_key)
        if facts is None:
            print(f"{f.name}: unrecognized content id {content_id!r}; skip",
                  file=sys.stderr)
            n_skipped += 1
            continue
        doc["trivia"] = facts
        st.stamp(doc, "trivia", BLOCK_VERSION)
        st.write_timeline(f, doc)
        n_updated += 1
        print(f"{f.name}: {len(facts)} facts written")
    print(f"\ntrivia: {n_updated} updated, {n_skipped} skipped")
    return {"updated": n_updated, "skipped": n_skipped}
