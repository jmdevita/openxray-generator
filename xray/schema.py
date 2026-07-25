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


#: Longest display label accepted, matching the schema's maxLength. These
#: strings render on public pages, so the cap is enforced where they are
#: written rather than trusted to whoever produced them.
LABEL_MAX = 200


def timeline(content_id, cast, actor_intervals=None, model_version=None,
             duration_ms=None, labels=None):
    """Assemble a timeline dict (plan.md §3).

    Empty actor_intervals is a valid v1 doc (full-cast panel); the whole point
    of the indexer is to supply real intervals here. `content_id` is the
    canonical content identity (xray/store.py) and is REQUIRED: a timeline
    without content identity cannot exist (schema). Server-local ids never
    enter the doc; the store manifest owns that mapping. `provenance` gains
    per-block stamps as enrichers run; model_version lands there, not at the
    top level.

    `labels` carries the optional human-facing {title, year, series}. They are
    for reading, never for matching, and are omitted entirely when unknown so
    a timeline never claims a title it does not have.
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
    doc.update(display_labels(labels))
    if model_version:
        doc["provenance"]["faces"] = {"generated": doc["generated"],
                                      "version": model_version}
    return doc


def display_labels(labels) -> dict:
    """The {title, year, series} subset worth writing, cleaned and capped.

    Absent beats null: a key that isn't there reads as "unknown", whereas a
    null invites clients to render an empty string. Blank strings and a
    `series` on a film both drop out here rather than at each call site."""
    out = {}
    for key in ("title", "series"):
        value = ((labels or {}).get(key) or "").strip()
        if value:
            out[key] = value[:LABEL_MAX]
    year = (labels or {}).get("year")
    if isinstance(year, int) and year >= 1870:
        out["year"] = year
    return out
