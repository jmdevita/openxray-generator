"""Timeline JSON contract (plan.md §3): the indexer's output format.

Mirrors extension/src/schema.js so the indexer and every client speak the same
shape. The indexer's job is to FILL the intervals the clients render as empty.
"""
from __future__ import annotations

from datetime import datetime, timezone

SCHEMA_VERSION = 1


def now_iso() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def timeline(content_id, cast, actor_intervals=None, model_version=None,
             duration_ms=None):
    """Assemble a timeline dict (plan.md §3).

    Empty actor_intervals is a valid v1 doc (full-cast panel); the whole point
    of the indexer is to supply real intervals here. `content_id` is the
    canonical content identity (xray/store.py) and is REQUIRED: a timeline
    without content identity cannot exist (schema). Server-local ids never
    enter the doc; the store manifest owns that mapping. `provenance` gains
    per-block stamps as enrichers run; model_version lands there, not at the
    top level.
    """
    from . import __version__
    if not content_id:
        raise ValueError("content_id is required: a timeline has no identity "
                         "without one (no TMDb match => cannot index)")
    doc = {
        "contentId": content_id,
        "sourceRuntimeMs": duration_ms,
        "generator": {"name": "openxray", "version": __version__},
        "version": SCHEMA_VERSION,
        "generated": now_iso(),
        "provenance": {},
        "cast": cast or [],
        "actorIntervals": actor_intervals or [],
        "musicIntervals": [],
        "trivia": [],
    }
    if model_version:
        doc["provenance"]["faces"] = {"generated": doc["generated"],
                                      "version": model_version}
    return doc
