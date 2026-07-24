"""Person-data pass: embed bio + known-for into `cast[].person`.

TMDb, throttled, with a cross-title cache (`people_cache.json` beside the
timelines) whose timestamps honor TMDb's ≤6-month cache rule. Touches only the
person sub-block + provenance stamp; validated writes.
"""
from __future__ import annotations

import json
import math
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

from .. import store as st

BLOCK_VERSION = "tmdb-v1"
CACHE_NAME = "people_cache.json"
TMDB = "https://api.themoviedb.org/3"
IMG = "https://image.tmdb.org/t/p/w185"
EXCLUDE_GENRES = {10767, 10763, 10764, 99}  # talk/news/reality/documentary
KNOWN_FOR_MAX = 10
BILLING_CUTOFF = 8


def is_self_credit(character: str | None) -> bool:
    c = (character or "").strip().lower()
    return c.startswith("self") or c in ("himself", "herself", "themselves")


def truncate_bio(bio: str, limit: int) -> str:
    if len(bio) <= limit:
        return bio
    cut = bio.rfind(" ", 0, limit)
    return bio[: cut if cut > 0 else limit].rstrip() + "…"


def known_for(credits: list[dict]) -> list[dict]:
    """Real roles ranked by billing-order × popularity blend."""
    def score(c):
        pop = c.get("popularity") or 0.0
        votes = c.get("vote_count") or 0
        order = c.get("order")
        top_billed = order is not None and order <= BILLING_CUTOFF
        return (1.0 if top_billed else 0.3) * pop * math.log10(votes + 10)

    seen, out = set(), []
    for c in sorted(credits, key=score, reverse=True):
        title = c.get("title") or c.get("name") or ""
        if not title or title in seen:
            continue
        if is_self_credit(c.get("character")):
            continue
        if set(c.get("genre_ids") or []) & EXCLUDE_GENRES:
            continue
        seen.add(title)
        date = c.get("release_date") or c.get("first_air_date") or ""
        out.append({
            "title": title,
            "year": date[:4] if date else "",
            "character": c.get("character") or "",
            "mediaType": c.get("media_type"),
            "posterUrl": f"{IMG}{c['poster_path']}" if c.get("poster_path") else None,
        })
        if len(out) >= KNOWN_FOR_MAX:
            break
    return out


class TmdbFetcher:
    def __init__(self, key: str, rate: float, bio_chars: int):
        self.key = key
        self.min_interval = 1.0 / rate
        self.bio_chars = bio_chars
        self._last = 0.0

    def _throttle(self):
        wait = self._last + self.min_interval - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self._last = time.monotonic()

    def fetch(self, person_id: str) -> dict | None:
        for attempt in range(4):
            self._throttle()
            try:
                r = requests.get(
                    f"{TMDB}/person/{person_id}",
                    params={"api_key": self.key,
                            "append_to_response": "combined_credits"},
                    timeout=15,
                )
            except requests.RequestException as e:
                print(f"    tmdb:{person_id} network error: {e}", file=sys.stderr)
                return None
            if r.status_code == 429:
                time.sleep(2.0 * (attempt + 1))
                continue
            if not r.ok:
                print(f"    tmdb:{person_id} HTTP {r.status_code}", file=sys.stderr)
                return None
            d = r.json()
            return {
                "bio": truncate_bio(d.get("biography") or "", self.bio_chars),
                "birthday": d.get("birthday"),
                "deathday": d.get("deathday"),
                "placeOfBirth": d.get("place_of_birth"),
                "knownFor": known_for(d.get("combined_credits", {}).get("cast", [])),
            }
        return None


def run(store_dir: Path, tmdb_key: str, keys: list[str] | None = None, *,
        refresh_days: float = 180, rate: float = 5.0, bio_chars: int = 1200,
        dry_run: bool = False) -> dict:
    files = st.resolve_timelines(store_dir, keys)
    cache_path = store_dir / CACHE_NAME
    cache = json.loads(cache_path.read_text()) if cache_path.exists() else {}
    max_age = refresh_days * 86400
    fetcher = TmdbFetcher(tmdb_key, rate, bio_chars)

    def cached_fresh(actor_id: str) -> dict | None:
        e = cache.get(actor_id)
        if not e:
            return None
        try:
            fetched = datetime.fromisoformat(e["fetched"].replace("Z", "+00:00"))
        except (KeyError, ValueError):
            return None
        if (datetime.now(timezone.utc) - fetched).total_seconds() > max_age:
            return None
        return e.get("person")

    n_fetched = n_cached = n_failed = 0
    for f in files:
        doc = json.loads(f.read_text())
        cast = doc.get("cast") or []
        changed = False
        todo = [c for c in cast if str(c.get("actorId", "")).startswith("tmdb:")]
        print(f"{f.name}: {len(cast)} cast, {len(todo)} tmdb-enrichable")
        for c in todo:
            actor_id = c["actorId"]
            person = cached_fresh(actor_id)
            if person is not None:
                n_cached += 1
            else:
                if dry_run:
                    print(f"  would fetch {actor_id} ({c.get('name')})")
                    continue
                person = fetcher.fetch(actor_id.split(":", 1)[1])
                if person is None:
                    n_failed += 1
                    continue
                cache[actor_id] = {"fetched": st.now_iso(), "person": person}
                n_fetched += 1
            if c.get("person") != person:
                c["person"] = person
                changed = True
        if changed and not dry_run:
            st.stamp(doc, "people", BLOCK_VERSION)
            st.write_timeline(f, doc)
            print(f"  wrote {f.name}")

    if not dry_run:
        st.atomic_write(cache_path, cache)
    print(f"\npeople: {n_fetched} fetched, {n_cached} cached, {n_failed} failed")
    return {"fetched": n_fetched, "cached": n_cached, "failed": n_failed}
