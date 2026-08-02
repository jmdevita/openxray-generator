"""The faceprint store: face clusters awaiting a name, and names given.

Enrol/read/suggest live in `prints.py`, shared with voices. This module owns
the face-specific numbers and the cluster document the labelling screen
reads. Every threshold below was measured; the evidence is with it, because
nothing else can recover it.
"""
from __future__ import annotations

from pathlib import Path

from . import prints

#: When a cluster is worth showing to a person. From crops of all 41
#: unmatched clusters on the Billions pilot: >=28s were real characters, 26s
#: was three people merged, 24s a blurred background face, 12s in one shot a
#: lamp. Set below the boundary on purpose -- junk sorts to the bottom and
#: costs a glance, a missed character can never be named at all.
MIN_SCREEN_S = 20.0
MIN_SCENES = 2

#: A gap longer than this starts a new appearance, for the scene count.
SCENE_GAP_S = 5.0

#: A cluster IS this cast member. True matches scored 0.656-0.774, the two
#: false ones 0.373 and 0.486, nothing between. SFace's 0.363 default put 122
#: seconds of one actor under another's name. Measured against Commons
#: singles, the thinnest references, so it errs safe.
MATCH_THRESHOLD = 0.55

#: Where the displayed word turns "good", then "strong". True matches
#: occupied 0.656-0.774. Provisional: eleven matches orders these honestly
#: but does not calibrate them, which is why they are words not percentages.
GOOD_MATCH = 0.60
STRONG_MATCH = 0.70

FACE = prints.Kind(name="faces", prints_file="faceprints.json",
                   enroll_min=MIN_SCREEN_S, match_min=MIN_SCREEN_S,
                   threshold=MATCH_THRESHOLD, good=GOOD_MATCH,
                   strong=STRONG_MATCH)


#: When a faceprint names a cluster in another episode of the same series.
#: Billions S01E01 vs S01E02: same actor 0.819-0.964, different actors
#: -0.123 to 0.263. Yield was flat from 0.70 to 0.80, so this is a canyon,
#: not a line to tune. Cross-SEASON is unmeasured -- hence series-only.
PROPAGATE_THRESHOLD = 0.75


def confidence(sim: float | None) -> str:
    return prints.confidence(FACE, sim)


def explain(sim: float | None, via: str = "") -> str:
    """The score, plus where right and wrong answers have actually landed."""
    if sim is None:
        return ""
    if via == "faceprint":
        return (f"{sim} out of 1. The same actor in another episode scores "
                f"0.82 and up; anyone else, under 0.3.")
    return (f"{sim} out of 1. Right matches scored 0.66 and up; the wrong "
            f"ones were under 0.5.")


def series_key(content_id: str) -> str | None:
    """The series a content id belongs to, or None for a film.

    `tmdb-tv-62852-s01e01` -> `tmdb-tv-62852`. Episodes of one show are the
    only place a faceprint is trusted to name a face on its own.
    """
    cid = str(content_id or "")
    if not cid.startswith("tmdb-tv-"):
        return None
    head, _, tail = cid.rpartition("-")
    return head if tail[:1] == "s" and "e" in tail else None


def siblings(store_dir: Path, content_id: str) -> list[str]:
    """Other episodes of the same series that have been through the pass."""
    key = series_key(content_id)
    if not key:
        return []
    d = Path(store_dir) / FACE.name
    return sorted(p.stem for p in d.glob(f"{key}-s*.json")
                  if p.stem != content_id and ".names" not in p.name)


def clusters_path(store_dir: Path, content_id: str) -> Path:
    return prints.clusters_path(store_dir, FACE, content_id)


def prints_path(store_dir: Path) -> Path:
    return prints.prints_path(store_dir, FACE)


def crops_dir(store_dir: Path, content_id: str) -> Path:
    """Beside the clusters, not in the pass's work directory: the screen
    needs these long after that is cleaned."""
    return Path(store_dir) / FACE.name / "crops" / content_id


def scene_count(times_ms, gap_s: float = SCENE_GAP_S) -> int:
    """Separate appearances, not samples: six scenes reads very differently
    from one long shot, and a sample count cannot tell them apart."""
    times = sorted(times_ms)
    if not times:
        return 0
    n, prev = 1, times[0]
    for t in times[1:]:
        if (t - prev) / 1000.0 > gap_s:
            n += 1
        prev = t
    return n


def spans(times_ms, runtime_ms: float, gap_s: float = SCENE_GAP_S):
    """[(start_fraction, width_fraction)] for the row's timeline strip."""
    times = sorted(times_ms)
    if not times or not runtime_ms:
        return []
    out, start, prev = [], times[0], times[0]
    for t in times[1:]:
        if (t - prev) / 1000.0 > gap_s:
            out.append((start, prev))
            start = t
        prev = t
    out.append((start, prev))
    return [(s / runtime_ms, max((e - s) / runtime_ms, 0.0)) for s, e in out]


def build_clusters(*, content_id: str, labels, hits, centroids, matched: dict,
                   runtime_ms: float, sample_fps: float, generated: str,
                   version: str) -> dict:
    """The cluster document the labelling screen reads.

    Every cluster is written, matched or not, and the floor is applied when
    the screen READS this. Persisting everything means the floor can be
    retuned -- and it will be, on other kinds of material -- without
    re-indexing a library that took hours to build.
    """
    times: dict[int, list[int]] = {}
    boxes: dict[int, list] = {}
    for hit, lab in zip(hits, labels):
        lab = int(lab)
        if lab == -1:                      # HDBSCAN noise: not a person
            continue
        times.setdefault(lab, []).append(int(hit.timestamp_ms))
        # Keyed by STRING deliberately: this document round-trips through
        # JSON, which stringifies keys, and a reader that got ints in memory
        # but strings from disk is a bug that only shows up after a restart.
        boxes.setdefault(str(lab), []).append(
            {"frame": int(hit.frame_index), "ms": int(hit.timestamp_ms),
             "bbox": list(hit.bbox) if hit.bbox else None})

    clusters = []
    for lab, ts in sorted(times.items(), key=lambda kv: -len(kv[1])):
        screen_s = len(ts) / sample_fps
        scenes = scene_count(ts)
        hit = matched.get(lab)
        clusters.append({
            "cluster": lab,
            "samples": len(ts),
            "screenSeconds": round(screen_s, 1),
            "scenes": scenes,
            "spans": [[round(a, 5), round(b, 5)]
                      for a, b in spans(ts, runtime_ms)],
            "nameable": screen_s >= MIN_SCREEN_S and scenes >= MIN_SCENES,
            "matched": ({"actorId": hit[0], "sim": round(float(hit[1]), 3)}
                        if hit else None),
            "embedding": [float(x) for x in centroids[lab]]
            if lab in centroids else None,
        })
    return {
        "contentId": content_id,
        "generated": generated,
        "version": version,
        "sampleFps": sample_fps,
        "runtimeMs": runtime_ms,
        "minScreenSeconds": MIN_SCREEN_S,
        "minScenes": MIN_SCENES,
        "clusters": clusters,
        # Bulky, and only the crop writer needs it -- see write_clusters,
        # which drops it before this reaches disk.
        "samples": boxes,
    }


def write_clusters(store_dir: Path, content_id: str, doc: dict) -> Path:
    """Persist the cluster rows, WITHOUT the per-sample boxes.

    Those exist to cut crops, which happens once, during the pass, while the
    frames are still on disk; afterwards they are unreachable data that would
    triple the file (371 KB against 60 KB on a 59-minute episode, times a
    library). Naming a cluster later needs its `spans`, which stay.
    """
    return prints.write_json(clusters_path(store_dir, content_id),
                             {k: v for k, v in doc.items() if k != "samples"})


def read_clusters(store_dir: Path, content_id: str) -> dict | None:
    return prints.read_clusters(store_dir, FACE, content_id)


def read_prints(store_dir: Path) -> dict:
    return prints.read_prints(store_dir, FACE)


def propagate(store_dir: Path, content_id: str, *,
              threshold: float = PROPAGATE_THRESHOLD) -> dict:
    """Name what the faceprints recognise in the rest of this series.

    Arithmetic over stored centroids: no media, no decoding. Writes to each
    episode's `matched` field, not its names file, so a propagated name shows
    as the machine's claim rather than something a person typed. Human names
    are left alone. Returns {episode: named_count}.
    """
    import numpy as np
    prints_by_actor = read_prints(store_dir)
    if not prints_by_actor:
        return {}
    refs = []
    for actor_id, rec in prints_by_actor.items():
        v = np.asarray(rec["embedding"], dtype=float)
        n = np.linalg.norm(v)
        if n:
            refs.append((actor_id, rec.get("character") or actor_id, v / n))
    if not refs:
        return {}

    changed: dict[str, int] = {}
    for sib in siblings(store_dir, content_id):
        doc = read_clusters(store_dir, sib)
        if not doc:
            continue
        named = prints.read_json(
            Path(store_dir) / FACE.name / f"{sib}.names.json", {}) or {}
        hits = 0
        for c in doc.get("clusters") or []:
            if not c.get("nameable") or c.get("embedding") is None:
                continue
            if str(c["cluster"]) in named:
                continue          # a person already spoke for this one
            v = np.asarray(c["embedding"], dtype=float)
            n = np.linalg.norm(v)
            if not n:
                continue
            v = v / n
            actor_id, _character, sim = max(
                ((a, ch, float(v @ w)) for a, ch, w in refs),
                key=lambda t: t[2])
            if sim < threshold:
                continue
            # Do not downgrade a cast-photo match that already agrees, and
            # do not overwrite a stronger claim with a weaker one.
            old = c.get("matched")
            if old and old.get("sim", 0) >= sim:
                continue
            c["matched"] = {"actorId": actor_id, "sim": round(sim, 3),
                            "via": "faceprint"}
            hits += 1
        if hits:
            write_clusters(store_dir, sib, doc)
            changed[sib] = hits
    return changed


def enroll(store_dir: Path, key: str, *, actor_id: str, character: str,
           embedding, content_id: str) -> None:
    """Record that `key` looks like this."""
    prints.enroll(store_dir, FACE, key, actor_id=actor_id,
                  character=character, embedding=embedding,
                  content_id=content_id)


def suggest(store_dir: Path, embedding, *,
            exclude_content: str = "") -> dict | None:
    """Best stored faceprint for this embedding, or None below threshold."""
    return prints.suggest(store_dir, FACE, embedding,
                          exclude_content=exclude_content)
