"""The voiceprint store: speaker clusters awaiting a name, and names given.

The durable half (enrol, read, suggest) lives in `prints.py`, which faces
share; this module owns the voice-specific numbers and the diarization-shaped
cluster document. The public API here is unchanged -- speakers.py, the
orchestrator, and the tests all still call it the same way.
"""
from __future__ import annotations

from pathlib import Path

from . import prints

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

VOICE = prints.Kind(name="speakers", prints_file="voiceprints.json",
                    enroll_min=ENROLL_MIN_S, match_min=MATCH_MIN_S,
                    threshold=MATCH_THRESHOLD, good=0.85, strong=0.90)


def confidence(sim: float | None) -> str:
    """Voices separate more cleanly than faces, so these bands sit higher.

    From the same measurement: true matches landed at 0.867-0.955 and the
    worst false positive at 0.621. Anything in the 0.75-0.85 gap between the
    threshold and the observed true band is honestly borderline, and 0.90 is
    comfortably inside it.
    """
    return prints.confidence(VOICE, sim)


def explain(sim: float | None, via: str = "") -> str:
    """The score, and the two numbers that make it mean anything."""
    if sim is None:
        return ""
    return (f"{sim} out of 1. Right matches scored 0.87 and up; the worst "
            f"wrong one, 0.62.")


def clusters_path(store_dir: Path, content_id: str) -> Path:
    return prints.clusters_path(store_dir, VOICE, content_id)


def prints_path(store_dir: Path) -> Path:
    return prints.prints_path(store_dir, VOICE)


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
    return prints.write_json(clusters_path(store_dir, content_id), doc)


def read_clusters(store_dir: Path, content_id: str) -> dict | None:
    return prints.read_clusters(store_dir, VOICE, content_id)


def read_prints(store_dir: Path) -> dict:
    return prints.read_prints(store_dir, VOICE)


def enroll(store_dir: Path, key: str, *, actor_id: str, character: str,
           embedding, content_id: str) -> None:
    """Record that `key` sounds like this. `key` is whatever identity the
    caller uses -- today `actorId`, since the contract has no role id."""
    prints.enroll(store_dir, VOICE, key, actor_id=actor_id,
                  character=character, embedding=embedding,
                  content_id=content_id)


def suggest(store_dir: Path, embedding, *,
            exclude_content: str = "") -> dict | None:
    """Best stored voiceprint for this embedding, or None below threshold.

    Same-title prints from OTHER speakers are kept deliberately: within-title
    different-character similarity was measured at 0.613/0.675/0.555 across
    three films, all under the threshold, so matching there is safe and
    catches a character that diarization split into two clusters.
    """
    return prints.suggest(store_dir, VOICE, embedding,
                          exclude_content=exclude_content)
