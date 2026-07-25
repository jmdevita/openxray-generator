"""Canonical timeline store: content-keyed files + lookup manifest.

The foundation contract (framework decision, 2026-07-19):

- Timeline files are the source of truth. They are named by CONTENT identity
  (TMDb title id), not by server-local ids: `tmdb-movie-769.json`,
  `tmdb-tv-62852-s01e01.json`. Server ratingKeys are unstable (change on
  re-add, differ per server/backend); content ids are forever.
- `index.json` maps backend lookup keys ("plex:288") to canonical filenames so
  clients can resolve a playing item to its timeline. Generators update it.
- Every enrichment pass stamps `provenance[block] = {generated, version}` in
  the file itself, so staleness is determinable from the file alone.
- Writes are atomic (tmp + rename). Passes touch only their own block.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

MANIFEST = "index.json"


def now_iso() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


# --- content identity ------------------------------------------------------

def movie_content_id(tmdb_id: int | str) -> str:
    return f"tmdb-movie-{tmdb_id}"


def episode_content_id(tmdb_tv_id: int | str, season: int, episode: int) -> str:
    return f"tmdb-tv-{tmdb_tv_id}-s{season:02d}e{episode:02d}"


def canonical_path(store: Path, content_id: str) -> Path:
    return store / f"{content_id}.json"


# --- manifest --------------------------------------------------------------

def load_manifest(store: Path) -> dict:
    p = store / MANIFEST
    if p.exists():
        return json.loads(p.read_text())
    return {"version": 1, "lookup": {}}


def save_manifest(store: Path, manifest: dict) -> None:
    atomic_write(store / MANIFEST, manifest)


def map_lookup(store: Path, lookup_key: str, content_id: str) -> None:
    """Point a backend lookup key ("plex:288") at a canonical file."""
    m = load_manifest(store)
    m["lookup"][lookup_key] = f"{content_id}.json"
    save_manifest(store, m)


def backend_ids(store: Path, content_id: str, prefix: str) -> list[str]:
    """Backend item ids mapped to this timeline, in the order they were added.

    The reverse of map_lookup, and the ONLY way back to a server item: the
    contract deliberately carries no server-local id, so a content-keyed
    timeline records that association here and nowhere else.

    A timeline can have several (a re-index after the server reassigned an
    id, or a legacy synthetic key), so callers get every match and should
    prefer the last: the newest mapping is likeliest to still resolve."""
    want = f"{content_id}.json"
    tag = f"{prefix}:"
    return [lk.split(":", 1)[1]
            for lk, fn in load_manifest(store).get("lookup", {}).items()
            if fn == want and lk.startswith(tag)]


# --- validation ------------------------------------------------------------

_SCHEMA_PATH = Path(__file__).resolve().parents[1] / "schema" / "timeline.schema.json"
_schema_cache: dict | None = None


def validate(doc: dict) -> None:
    """Check a timeline doc against the contract (schema/timeline.schema.json).

    Raises jsonschema.ValidationError. Every pass MUST call this before
    writing: an enricher bug must not silently corrupt the store."""
    global _schema_cache
    import jsonschema

    if _schema_cache is None:
        _schema_cache = json.loads(_SCHEMA_PATH.read_text())
    jsonschema.validate(doc, _schema_cache)


# --- file ops --------------------------------------------------------------

def atomic_write(path: Path, data) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    tmp.replace(path)


def write_timeline(path: Path, doc: dict) -> None:
    """Validate-then-write: the only sanctioned way to write a timeline."""
    validate(doc)
    atomic_write(path, doc)


def stamp(doc: dict, block: str, version: str) -> None:
    """Record that `block` was (re)generated now by `version`."""
    doc.setdefault("provenance", {})[block] = {
        "generated": now_iso(),
        "version": version,
    }


def resolve_timelines(store: Path, keys: list[str] | None) -> list[Path]:
    """Timelines for the given content ids, or every timeline in the store
    when keys is None/empty. Raises SystemExit listing any key that resolves
    to nothing."""
    if not keys:
        return timeline_files(store)
    files, missing = [], []
    for k in keys:
        cand = store / f"{k}.json"
        if cand.exists():
            files.append(cand.resolve())
        else:
            missing.append(k)
    if missing:
        raise SystemExit(f"no timeline for: {missing}")
    return files


def timeline_files(store: Path) -> list[Path]:
    """Every timeline in the store (canonical tmdb-* names), excluding the
    manifest, caches, and non-timeline JSON."""
    out = []
    for p in sorted(store.glob("tmdb-*.json")):
        if p.name.endswith("_cache.json"):
            continue
        out.append(p)
    return out
