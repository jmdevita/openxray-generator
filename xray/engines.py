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


class HttpSpeakerEngine:
    """Service transport: the compose stack's engine-speakers container.

    Diarization only -- turns plus one embedding per speaker. It never names
    anyone: identity comes from a human, and the embeddings exist so that a
    name given once can be carried to the next title by similarity.

    No local fallback, unlike faces. pyannote means torch, and torch has no
    business in the orchestrator image for a pass most libraries never run.
    Without the container this pass is simply unavailable, and `ready()` says
    so rather than failing halfway through a title.
    """

    def __init__(self, base_url: str):
        self.base = base_url.rstrip("/")

    #: What each engine state means for someone trying to run the pass. The
    #: engine reports the state; the wording lives here because this is the
    #: layer that knows there is a dashboard to send people to.
    _WHY = {
        "no-token": ("speaker diarization needs its model weights. Add a "
                     "HuggingFace token in Setup → Speakers and the "
                     "dashboard will fetch them (about 100 MB, once)."),
        "needs-fetch": ("the model weights have not been downloaded yet. "
                        "Open Setup → Speakers and choose Download."),
        "fetching": ("the model weights are downloading now. Try again in a "
                     "minute."),
        "bad-token": ("HuggingFace rejected the configured token. Replace it "
                      "in Setup → Speakers."),
        "gated": ("the HuggingFace token works, but the pyannote conditions "
                  "have not been accepted yet. Setup → Speakers lists the "
                  "pages to accept."),
        "load-failed": ("engine-speakers could not prepare its model. Setup → "
                        "Speakers has the details."),
    }

    def ready(self) -> tuple[bool, str]:
        j = self.model_state()
        if not j.get("reachable", True):
            return False, j["message"]
        if j.get("error"):
            return False, f"engine-speakers cannot load its model: {j['error']}"
        state = j.get("state")
        if state != "ready":
            # Surfaced BEFORE a run rather than after the audio pull:
            # discovering a missing model at minute 20 of a feature wastes
            # exactly the expensive part.
            return False, self._WHY.get(
                state, f"engine-speakers is not ready ({state})")
        return True, ""

    def model_state(self) -> dict:
        """The engine's /health, with unreachability folded in as a state.

        The dashboard needs the same answer `ready()` computes but in full --
        which gates are unaccepted, whether a token exists, where the cache is
        -- so both go through here and neither invents its own probe.
        """
        import requests
        try:
            r = requests.get(f"{self.base}/health", timeout=5)
            r.raise_for_status()
            return {"reachable": True, **r.json()}
        except Exception as e:                     # noqa: BLE001 (reported)
            return {"reachable": False, "state": "unreachable",
                    "message": (f"engine-speakers unreachable at {self.base} "
                                f"({type(e).__name__}). Start it with: "
                                f"docker compose --profile speakers up -d")}

    def fetch_models(self, timeout: int = 1800) -> dict:
        """Download the weights now. Long timeout: ~100 MB plus a load proof,
        on whatever connection the host has."""
        import requests
        r = requests.post(f"{self.base}/models", timeout=timeout)
        r.raise_for_status()
        return r.json()

    def diarize(self, audio_path: Path, *, min_speakers: int | None = None,
                max_speakers: int | None = None, timeout: int = 7200) -> dict:
        """{turns, labels, embeddings} for one audio file.

        The long timeout is not padding: diarization runs at roughly 3.6x
        realtime on CPU with threading, so a feature is ~25 minutes and a
        generous ceiling beats a spurious failure at minute 24.
        """
        import requests
        body = {"audio_path": str(audio_path)}
        if min_speakers:
            body["min_speakers"] = min_speakers
        if max_speakers:
            body["max_speakers"] = max_speakers
        r = requests.post(f"{self.base}/diarize", json=body, timeout=timeout)
        if not r.ok:
            # raise_for_status() reports the STATUS and drops the body, where
            # the engine put the actual reason. A bare "500 Server Error"
            # meant the only way to learn anything was `docker logs`, after a
            # run that had already spent half an hour pulling audio.
            detail = ""
            try:
                detail = (r.json() or {}).get("detail") or ""
            except ValueError:
                detail = (r.text or "").strip()[:400]
            raise RuntimeError(
                f"engine-speakers could not diarize ({r.status_code})"
                + (f": {detail}" if detail else ""))
        return r.json()


def speaker_transport() -> HttpSpeakerEngine | None:
    """The speakers service client when XRAY_ENGINE_SPEAKERS_URL is set.

    None means the pass is unavailable, NOT that it should run locally --
    there is no in-process diarizer by design.
    """
    import os
    url = os.environ.get("XRAY_ENGINE_SPEAKERS_URL", "").strip()
    return HttpSpeakerEngine(url) if url else None


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
