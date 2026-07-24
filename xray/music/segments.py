"""Music-scene detection via the inaSpeechSegmenter container.

Finds WHERE music plays so recognition API calls scale with music cues, not
runtime (validated in audio_matching_analysis: a 93-min film → ~40 cues →
~$0.20 instead of hundreds of blind windows). The CNN labels audio as
speech/music/noise/silence; we keep the music spans, merge fragments, and drop
short stingers.

Requires the `music-detect:latest` Docker image (see
engines/audio/Dockerfile; TF 2.15-pinned build).
The raw segmentation is cached, so re-tuning merge/min-music is free.
"""
from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

IMAGE = "music-detect:latest"
MARKER = "INA_JSON:"


@dataclass
class MusicSegment:
    start: float
    end: float

    @property
    def duration(self) -> float:
        return self.end - self.start


def docker_ready() -> tuple[bool, str]:
    if subprocess.run(["docker", "info"], capture_output=True).returncode != 0:
        return False, "docker daemon not reachable"
    cp = subprocess.run(["docker", "images", "-q", IMAGE],
                        capture_output=True, text=True)
    if not cp.stdout.strip():
        return False, (f"{IMAGE} image missing; build it: "
                       "docker build -t music-detect:latest "
                       "engines/audio/")
    return True, ""


def _raw_segmentation(audio: Path, cache_dir: Path, timeout: int) -> list[dict]:
    """Run the segmenter (or reuse cached output). The CNN pass on a feature
    film takes minutes; the cache makes parameter re-tuning free."""
    cache = cache_dir / f"{audio.stem}__ina.json"
    if cache.exists() and cache.stat().st_mtime >= audio.stat().st_mtime:
        return json.loads(cache.read_text())

    cp = subprocess.run(
        ["docker", "run", "--rm",
         "-v", f"{audio.parent}:/data:ro",
         IMAGE, "python", "/app/segment.py", f"/data/{audio.name}"],
        capture_output=True, text=True, timeout=timeout,
    )
    if cp.returncode != 0:
        raise RuntimeError(f"segmenter failed: {cp.stderr.strip()[-500:]}")
    for line in cp.stdout.splitlines():
        if line.startswith(MARKER):
            raw = json.loads(line[len(MARKER):])
            cache_dir.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps(raw))
            return raw
    raise RuntimeError(f"no segmentation marker in output:\n{cp.stdout[-400:]}")


def detect_music(
    audio: Path,
    cache_dir: Path,
    *,
    min_music_seconds: float = 10.0,
    merge_gap: float = 15.0,
    timeout: int = 3600,
) -> list[MusicSegment]:
    """Merged, duration-filtered music cues for `audio`. Defaults are the
    whole-movie tuning validated on Shrek 2 (82 raw cues → 39 useful)."""
    raw = _raw_segmentation(audio.resolve(), cache_dir, timeout)
    music = sorted((s for s in raw if s.get("label") == "music"),
                   key=lambda s: s["start"])
    merged: list[MusicSegment] = []
    for s in music:
        seg = MusicSegment(float(s["start"]), float(s["end"]))
        if merged and seg.start - merged[-1].end <= merge_gap:
            merged[-1].end = max(merged[-1].end, seg.end)
        else:
            merged.append(seg)
    return [m for m in merged if m.duration >= min_music_seconds]
