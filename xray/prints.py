"""Durable identity references — "this embedding is Wendy Rhoades".

CLUSTERS are per-title scratch, shaped differently per modality, so each
writes its own. PRINTS are the durable half and identical everywhere, which
is why enrol/suggest live here once.

Neither ever enters a timeline: embeddings are biometric and derived from
copyrighted media, so they stay on the machine that made them. Timelines
carry intervals.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Kind:
    """One modality's store: where it lives and how strict it is. Every
    threshold was measured; the modality modules carry the evidence."""
    name: str            #: subdirectory under the store, e.g. "speakers"
    prints_file: str     #: fixed once written -- renaming orphans the
                         #: identities somebody has already typed in
    enroll_min: float    #: below this a cluster is too thin to enrol
    match_min: float     #: below this a cluster must not be MATCHED
    threshold: float     #: cosine at or above which two prints are one person
    good: float          #: clear of the threshold's margin
    strong: float        #: no realistic doubt


def confidence(kind: Kind, sim: float | None) -> str:
    """What a similarity is CALLED in front of a person.

    Ordinal, never a percentage: 0.55 is the line above which every measured
    face match was right, and "55% confident" would read as a coin flip.
    """
    if sim is None:
        return ""
    if sim >= kind.strong:
        return "strong match"
    if sim >= kind.good:
        return "good match"
    return "borderline"


def clusters_path(store_dir: Path, kind: Kind, content_id: str) -> Path:
    return Path(store_dir) / kind.name / f"{content_id}.json"


def prints_path(store_dir: Path, kind: Kind) -> Path:
    return Path(store_dir) / kind.name / kind.prints_file


def write_json(path: Path, doc) -> Path:
    """Atomic: a half-written store read by the dashboard mid-pass is a crash
    for the user and a mystery for us."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(doc), encoding="utf-8")
    tmp.replace(path)
    return path


def read_json(path: Path, default=None):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def read_clusters(store_dir: Path, kind: Kind, content_id: str) -> dict | None:
    return read_json(clusters_path(store_dir, kind, content_id))


def read_prints(store_dir: Path, kind: Kind) -> dict:
    return read_json(prints_path(store_dir, kind), {}) or {}


def enroll(store_dir: Path, kind: Kind, key: str, *, actor_id: str,
           character: str, embedding, content_id: str) -> None:
    """Record that `key` looks or sounds like this.

    Overwrites rather than averaging. A later enrollment usually comes from a
    title with more material or a cleaner cluster, and averaging two
    references of unknown quality is a good way to end up with one that
    matches neither.
    """
    if embedding is None:
        return
    prints = read_prints(store_dir, kind)
    prints[key] = {"actorId": actor_id, "character": character,
                   "embedding": list(embedding), "from": content_id}
    write_json(prints_path(store_dir, kind), prints)


def suggest(store_dir: Path, kind: Kind, embedding, *,
            exclude_content: str = "") -> dict | None:
    """Best stored print for this embedding, or None below the threshold.

    `exclude_content` drops prints enrolled from the given title so a cluster
    cannot be suggested against itself. Same-title prints from OTHER clusters
    are kept deliberately: they catch a character that clustering split in
    two, which is a common and annoying failure to fix by hand.
    """
    if embedding is None:
        return None
    import numpy as np
    prints = read_prints(store_dir, kind)
    if not prints:
        return None
    v = np.asarray(embedding, dtype=float)
    n = np.linalg.norm(v)
    if not n:
        return None
    v = v / n
    best, best_sim = None, -1.0
    for key, rec in prints.items():
        if exclude_content and rec.get("from") == exclude_content:
            continue
        w = np.asarray(rec["embedding"], dtype=float)
        wn = np.linalg.norm(w)
        if not wn:
            continue
        sim = float(v @ (w / wn))
        if sim > best_sim:
            best, best_sim = (key, rec), sim
    if best is None or best_sim < kind.threshold:
        return None
    key, rec = best
    return {"key": key, "actorId": rec["actorId"],
            "character": rec["character"], "from": rec.get("from"),
            "sim": round(best_sim, 3)}
