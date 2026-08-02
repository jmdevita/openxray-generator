"""The faceprint store: face clusters awaiting a name, and names given.

The durable half (enrol, read, suggest) is `prints.py`, shared with voices.
This module owns the face-specific numbers and the cluster document the
labelling screen reads.

Why a labelling screen exists at all: the face pass matches clusters against
cast reference photos, and Wikimedia Commons -- the licence-clean source --
simply has no photo for many working actors. Measured on the Billions pilot,
a main-cast character with four minutes of screen time across 41 scenes went
unmatched purely because nobody has ever released a free-licensed photo of
him. He is on screen; the reference is what is missing. So the video itself
becomes the reference, and a person supplies the name.
"""
from __future__ import annotations

from pathlib import Path

from . import prints

#: Screen seconds before a cluster is worth putting in front of a person, and
#: how many separate appearances it needs. Measured on the Billions pilot
#: (59 min, 2647 faces, 52 clusters) by looking at exemplar crops of every
#: unmatched cluster:
#:
#:   >=28s  every cluster inspected was a clean, nameable character --
#:          including two child actors and a supporting player, none of whom
#:          have a Commons photo and none of whom could be matched otherwise
#:   26s    a cluster that had merged THREE different people
#:   24s    an out-of-focus background face
#:   12s, one scene   a lamp (YuNet false positives are real)
#:
#: 20s errs deliberately low. Rows are sorted by screen time, so junk sinks
#: below the fold and costs a glance; a real character dropped by the floor
#: can never be named at all, and is invisible in the timeline forever.
#: The two-scene clause kills the single-shot artefacts (the lamp appears
#: six times in one shot) that seconds alone would admit.
MIN_SCREEN_S = 20.0
MIN_SCENES = 2

#: A gap longer than this starts a new appearance, for the scene count.
SCENE_GAP_S = 5.0

#: Same person above this. From the measured gap on that pilot, not taste:
#: every true match scored 0.656-0.774 and the two false ones scored 0.373
#: and 0.486, with nothing in between. 0.55 sits mid-band.
#:
#: SFace's generic 0.363 default was demonstrably too low here -- it claimed
#: 122 seconds of one actor as another, which no amount of downstream care
#: can fix because nothing reports it. Note the references behind these
#: numbers are Commons singles, the THINNEST case; richer references score
#: higher, so this errs on the safe side.
MATCH_THRESHOLD = 0.55

#: Where "borderline" becomes "good", then "strong", for the words shown
#: beside a match. From the same pilot: true matches occupied 0.656-0.774, so
#: 0.60 is just clear of the threshold's margin and 0.70 is the top of the
#: observed true band. Provisional -- eleven matches on one episode orders
#: these honestly but does not calibrate them finely, which is exactly why
#: they are words and not a percentage.
GOOD_MATCH = 0.60
STRONG_MATCH = 0.70

FACE = prints.Kind(name="faces", prints_file="faceprints.json",
                   enroll_min=MIN_SCREEN_S, match_min=MIN_SCREEN_S,
                   threshold=MATCH_THRESHOLD, good=GOOD_MATCH,
                   strong=STRONG_MATCH)


#: Cosine at which a FACEPRINT (rather than a cast photo) names a cluster
#: outright, in another episode of the same series.
#:
#: Measured 2026-08-02, Billions S01E01 against S01E02, both indexed for
#: real. Same actor across the two: 0.819-0.964. Different actor: -0.123 to
#: 0.263. A gap of 0.556 -- three times the separation cast photos manage
#: within one episode, because a faceprint is a centroid of dozens of frames
#: from the same production rather than one red-carpet portrait years old.
#:
#: 0.75 sits 0.07 under the weakest true match and 0.49 over the strongest
#: false one. Yield was flat from 0.70 to 0.80 (13 of 51 clusters, 58% of
#: the second episode's screen time) and fell only at 0.85, so this is a
#: canyon rather than a line to tune.
#:
#: Adjacent episodes share a haircut, a wardrobe and probably a shooting
#: week. Cross-SEASON is the harder, unmeasured case; that is why this
#: applies within a series and cross-title stays a suggestion.
PROPAGATE_THRESHOLD = 0.75


def confidence(sim: float | None) -> str:
    return prints.confidence(FACE, sim)


def explain(sim: float | None, via: str = "") -> str:
    """The score, and the only two numbers that make it mean anything.

    Somebody hovering wants to know whether to trust this row, which takes
    one line: where the right answers have landed, and where the wrong ones
    did. Not what the arithmetic is called.
    """
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
    """Where a cluster's exemplar faces live. Beside the clusters rather than
    in the pass's work directory, which is cleaned: the screen needs them
    long after the pass is over."""
    return Path(store_dir) / FACE.name / "crops" / content_id


def scene_count(times_ms, gap_s: float = SCENE_GAP_S) -> int:
    """Separate appearances, not samples. A character in six scenes reads
    very differently from an extra standing in one long shot, and the
    difference is invisible in a raw sample count."""
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

    Costs no media at all. Every cluster's centroid is already stored, so
    naming the recurring cast across a season is arithmetic over numbers on
    disk -- no frames, no decoding, no traffic to the media server. That is
    what makes this an action a person can press and watch finish, instead
    of a re-index they have to schedule.

    Writes into each episode's `matched` field, NOT its names file: a
    faceprint match is the machine's claim, and the screen shows those as
    "check me" so a wrong one is visible rather than indistinguishable from
    something a person typed. Human names already in a sibling are left
    exactly as they are.

    Returns {episode: named_count} for the episodes that changed.
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
