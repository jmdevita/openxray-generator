"""engine-audio: music-scene detection as a stateless HTTP service.

Runs INSIDE the TF-pinned container (deploy/engine-audio.Dockerfile), where
inaSpeechSegmenter imports natively, no docker-in-docker. Shares the work
volume with the orchestrator, so requests pass file paths, not payloads.

  POST /segment {"audio_path": "...", "min_music_seconds": 10, "merge_gap": 15}
    → {"segments": [{"start": ..., "end": ...}, ...]}
  GET  /health
"""
from __future__ import annotations

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

app = FastAPI(title="OpenXray engine-audio", version="0.1.0")

_segmenter = None  # loaded once, first request (model load is seconds)


class SegmentRequest(BaseModel):
    audio_path: str
    min_music_seconds: float = 10.0
    merge_gap: float = 15.0


@app.get("/health")
def health():
    return {"ok": True, "engine": "audio", "loaded": _segmenter is not None}


@app.post("/segment")
def segment(req: SegmentRequest):
    global _segmenter
    from pathlib import Path
    p = Path(req.audio_path)
    if not p.exists():
        raise HTTPException(404, f"no audio at {req.audio_path}")
    if _segmenter is None:
        from inaSpeechSegmenter import Segmenter
        _segmenter = Segmenter(vad_engine="smn", detect_gender=False)
    raw = _segmenter(str(p))
    music = sorted(((float(s), float(e)) for label, s, e in raw if label == "music"))
    merged: list[list[float]] = []
    for s, e in music:
        if merged and s - merged[-1][1] <= req.merge_gap:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    out = [{"start": s, "end": e} for s, e in merged
           if e - s >= req.min_music_seconds]
    return {"segments": out}
