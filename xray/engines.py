"""Engine seam: pass code reaches heavy engines only through these clients.

Two transports (unification plan U2), both wired:
- LOCAL (what the CLI uses by default): faces in-process via OpenCV; audio
  segmentation via `docker run` of the image built from engines/audio/.
- SERVICE: the same work backed by HTTP calls to the compose stack's engine
  containers: audio via XRAY_ENGINE_AUDIO_URL (HttpAudioSegmenter), faces
  via XRAY_ENGINE_FACES_URL (HttpFaceEngine, job-level API). Pass code must
  not care which is active.

Engines are stateless calculators: inputs in, JSON-able results out. All state
stays in the timeline store.
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODELS = ROOT / "models"
YUNET = MODELS / "face_detection_yunet_2023mar.onnx"
SFACE = MODELS / "face_recognition_sface_2021dec.onnx"


class FaceEngineUnavailable(RuntimeError):
    pass


def face_engine():
    """In-process face engine (YuNet detect + SFace embed)."""
    if not (YUNET.exists() and SFACE.exists()):
        raise FaceEngineUnavailable(
            "face models missing: run scripts/fetch_models.py")
    from .faces.embedder import SFaceEmbedder
    return SFaceEmbedder(YUNET, SFACE)


class HttpFaceEngine:
    """Service transport: the compose stack's engine-faces container.

    Deliberately job-level (analyze a whole frames dir, embed one reference
    image file) rather than per-frame detect/embed: one HTTP round-trip per
    job keeps wire overhead negligible. Paths must be visible to the service
    (shared volume). Selected via XRAY_ENGINE_FACES_URL."""

    def __init__(self, base_url: str):
        self.base = base_url.rstrip("/")

    def ready(self) -> tuple[bool, str]:
        import requests
        try:
            r = requests.get(f"{self.base}/health", timeout=5)
            r.raise_for_status()
            return True, ""
        except requests.RequestException as e:
            return False, f"engine-faces unreachable at {self.base}: {e}"

    def analyze(self, frames_dir: Path) -> tuple[str, list[dict]]:
        """(model_version, faces) for every frame_*.jpg in [frames_dir];
        faces are {"frame_index", "bbox", "embedding"} dicts."""
        import requests
        r = requests.post(f"{self.base}/analyze",
                          json={"frames_dir": str(frames_dir)}, timeout=3600)
        r.raise_for_status()
        j = r.json()
        return j["model_version"], j["faces"]

    def embed_image_file(self, path: Path) -> list[dict]:
        """[{"bbox", "embedding"}] per detected face in one reference image."""
        import requests
        r = requests.post(f"{self.base}/embed-image",
                          json={"image_path": str(path)}, timeout=300)
        r.raise_for_status()
        return r.json()["faces"]


def face_transport() -> HttpFaceEngine | None:
    """The faces service client when XRAY_ENGINE_FACES_URL is set, else None
    (callers fall back to the in-process face_engine())."""
    import os
    url = os.environ.get("XRAY_ENGINE_FACES_URL", "").strip()
    return HttpFaceEngine(url) if url else None


class AudioSegmenter:
    """Music-scene detection (inaSpeechSegmenter). Local transport: docker run.

    Build the image once:
      docker build -t music-detect:latest engines/audio/
    """

    def ready(self) -> tuple[bool, str]:
        from .music.segments import docker_ready
        return docker_ready()

    def segment(self, audio: Path, cache_dir: Path, *,
                min_music_seconds: float = 10.0, merge_gap: float = 15.0):
        from .music.segments import detect_music
        return detect_music(audio, cache_dir,
                            min_music_seconds=min_music_seconds,
                            merge_gap=merge_gap)


class HttpAudioSegmenter:
    """Service transport: the compose stack's engine-audio container.

    Same interface as AudioSegmenter; paths must be visible to the service
    (shared volume). Selected via XRAY_ENGINE_AUDIO_URL."""

    def __init__(self, base_url: str):
        self.base = base_url.rstrip("/")

    def ready(self) -> tuple[bool, str]:
        import requests
        try:
            r = requests.get(f"{self.base}/health", timeout=5)
            r.raise_for_status()
            return True, ""
        except requests.RequestException as e:
            return False, f"engine-audio unreachable at {self.base}: {e}"

    def segment(self, audio: Path, cache_dir: Path, *,
                min_music_seconds: float = 10.0, merge_gap: float = 15.0):
        import requests
        from .music.segments import MusicSegment
        r = requests.post(f"{self.base}/segment", json={
            "audio_path": str(audio),
            "min_music_seconds": min_music_seconds,
            "merge_gap": merge_gap,
        }, timeout=3600)
        r.raise_for_status()
        return [MusicSegment(s["start"], s["end"])
                for s in r.json()["segments"]]


def audio_segmenter():
    import os
    url = os.environ.get("XRAY_ENGINE_AUDIO_URL", "").strip()
    return HttpAudioSegmenter(url) if url else AudioSegmenter()
