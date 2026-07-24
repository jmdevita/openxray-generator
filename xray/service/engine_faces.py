"""engine-faces: face detect + embed as a stateless HTTP service.

Job-level API (one call per frames directory, not per image) so the wire
overhead stays negligible. Shares the work volume with the orchestrator.

  POST /analyze {"frames_dir": "..."}
    → {"model_version": "sface-v1",
       "faces": [{"frame_index": 12, "bbox": [x,y,w,h], "embedding": [...128]}]}
  POST /embed-image {"image_path": "..."}   (reference headshots)
    → {"faces": [{"bbox": [x,y,w,h], "embedding": [...128]}]}
      (bbox included so the caller can pick the largest face in group shots,
       matching the local reference-building behavior)
  GET  /health
"""
from __future__ import annotations

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

app = FastAPI(title="OpenXray engine-faces", version="0.1.0")

_engine = None


def _get_engine():
    global _engine
    if _engine is None:
        from ..engines import face_engine
        _engine = face_engine()
    return _engine


class AnalyzeRequest(BaseModel):
    frames_dir: str


class EmbedImageRequest(BaseModel):
    image_path: str


@app.get("/health")
def health():
    return {"ok": True, "engine": "faces", "loaded": _engine is not None}


@app.post("/analyze")
def analyze(req: AnalyzeRequest):
    import cv2
    from pathlib import Path
    d = Path(req.frames_dir)
    if not d.is_dir():
        raise HTTPException(404, f"no frames dir at {req.frames_dir}")
    eng = _get_engine()
    faces = []
    for p in sorted(d.glob("frame_*.jpg")):
        img = cv2.imread(str(p))
        if img is None:
            continue
        idx = int(p.stem.split("_")[1])
        for det in eng.detect(img):
            faces.append({
                "frame_index": idx,
                "bbox": [float(v) for v in det.bbox],
                "embedding": [float(v) for v in eng.embed(img, det)],
            })
    return {"model_version": eng.model_version, "faces": faces}


@app.post("/embed-image")
def embed_image(req: EmbedImageRequest):
    import cv2
    from pathlib import Path
    p = Path(req.image_path)
    if not p.exists():
        raise HTTPException(404, f"no image at {req.image_path}")
    eng = _get_engine()
    img = cv2.imread(str(p))
    if img is None:
        raise HTTPException(422, "unreadable image")
    return {"faces": [{"bbox": [float(v) for v in det.bbox],
                       "embedding": [float(v) for v in eng.embed(img, det)]}
                      for det in eng.detect(img)]}
