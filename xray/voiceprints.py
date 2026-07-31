"""The voiceprint store: clusters awaiting a name, and names already given.

Two things live here, and keeping them apart matters.

CLUSTERS are per-title scratch: "this title has 38 speakers, here is when each
talks and what each sounds like". Regenerated whenever the pass re-runs.

VOICEPRINTS are the durable asset: "this embedding is Shrek". Written when a
human names a cluster, read when a later title wants a suggestion. They are
what makes naming a character once worth more than naming it once.

Deliberately NOT in the timeline. A timeline is a public artefact that gets
shared and republished; embeddings are large, derived from copyrighted audio,
and useful only to the machine that computes more of them. The timeline
carries INTERVALS -- facts about when a character speaks -- and nothing else.
"""
from __future__ import annotations

import json
from pathlib import Path

#: Minimum audio before a speaker may be ENROLLED as a voiceprint, versus
#: before it may be MATCHED against one. Measured 2026-07-29 against a
#: different-cast film, varying each side independently:
#:
#:   reference >=2min, probe unfiltered  -> max spurious 0.754  (CLEARS 0.75)
#:   reference >=2min, probe >=1min      -> max spurious 0.621
#:   probe >=2min, reference unfiltered  -> max spurious 0.512
#:
#: The dangerous short side is the PROBE, not the reference: a 20-second
#: speaker makes a serviceable reference but invents similarity when matched.
#: One symmetric floor was doing one job well and one needlessly, and the
#: needless half hid ~40% of a film's speakers from being nameable at all.
ENROLL_MIN_S = 30.0
MATCH_MIN_S = 60.0

#: Same person above this. From the measured gap, not taste: true matches
#: landed at 0.867-0.955 (confirmed by ear), the worst false positive at
#: 0.621, and nothing at all in between. 0.75 sits mid-band with ~0.12 of
#: headroom either side.
MATCH_THRESHOLD = 0.75


def clusters_path(store_dir: Path, content_id: str) -> Path:
    return Path(store_dir) / "speakers" / f"{content_id}.json"


def prints_path(store_dir: Path) -> Path:
    return Path(store_dir) / "speakers" / "voiceprints.json"


def write_clusters(store_dir: Path, content_id: str, *, turns, labels,
                   embeddings, generated: str, version: str) -> Path:
    """Diarization output for one title. No identities: that is a human's job.

    `embeddings[i]` lines up with `labels[i]` and may be null -- pyannote
    returns NaN for a speaker with too little audio to embed, and dropping
    those would silently break the index alignment.
    """
    seconds: dict[str, float] = {}
    for start, end, spk in turns:
        seconds[spk] = seconds.get(spk, 0.0) + (float(end) - float(start))
    doc = {
        "contentId": content_id,
        "generated": generated,
        "version": version,
        "speakers": [
            {"speaker": spk,
             "seconds": round(seconds.get(spk, 0.0), 2),
             "enrollable": seconds.get(spk, 0.0) >= ENROLL_MIN_S,
             "matchable": seconds.get(spk, 0.0) >= MATCH_MIN_S,
             "embedding": (embeddings[i] if embeddings else None)}
            for i, spk in enumerate(labels)],
        "turns": [[round(float(s), 3), round(float(e), 3), str(k)]
                  for s, e, k in turns],
    }
    out = clusters_path(store_dir, content_id)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".tmp")
    tmp.write_text(json.dumps(doc), encoding="utf-8")
    tmp.replace(out)
    return out


def read_clusters(store_dir: Path, content_id: str) -> dict | None:
    p = clusters_path(store_dir, content_id)
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def read_prints(store_dir: Path) -> dict:
    p = prints_path(store_dir)
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def enroll(store_dir: Path, key: str, *, actor_id: str, character: str,
           embedding, content_id: str) -> None:
    """Record that `key` sounds like this. `key` is whatever identity the
    caller uses -- today `actorId`, since the contract has no role id.

    Overwrites rather than averaging. A later enrollment usually comes from a
    title with more audio or a cleaner cluster, and averaging two references
    of unknown quality is a good way to end up with one that matches neither.
    """
    if embedding is None:
        return
    prints = read_prints(store_dir)
    prints[key] = {"actorId": actor_id, "character": character,
                   "embedding": list(embedding), "from": content_id}
    p = prints_path(store_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(prints), encoding="utf-8")
    tmp.replace(p)


def suggest(store_dir: Path, embedding, *, exclude_content: str = "") -> dict | None:
    """Best stored voiceprint for this embedding, or None below threshold.

    `exclude_content` drops prints enrolled from the given title so a speaker
    cannot be suggested against itself. Same-title prints from OTHER speakers
    are kept deliberately: within-title different-character similarity was
    measured at 0.613/0.675/0.555 across three films, all under the threshold,
    so matching there is safe and catches a character that diarization split
    into two clusters.
    """
    if embedding is None:
        return None
    import numpy as np
    prints = read_prints(store_dir)
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
    if best is None or best_sim < MATCH_THRESHOLD:
        return None
    key, rec = best
    return {"key": key, "actorId": rec["actorId"],
            "character": rec["character"], "from": rec.get("from"),
            "sim": round(best_sim, 3)}
