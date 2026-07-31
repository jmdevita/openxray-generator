"""engine-speakers: diarization behind an HTTP seam.

Answers "how many distinct people speak, and when" for one audio file. It does
NOT know who they are: naming a speaker is a human's job (see the labelling
screen), and the whole reason this service returns per-speaker embeddings is so
that a name given once can be propagated by similarity.

Separate container because pyannote drags in torch. Keeping that out of the
orchestrator image is the same call `engine-faces` makes, and here it is not
optional: the orchestrator is the thing every user runs.

WEIGHTS AND THE GATE. The pyannote weights are openly licensed but sit behind
an accept-the-conditions gate, so getting them takes a HuggingFace token once.
Two ways, both ending in the same cache:

  BAKED -- `docker build` with the token as a BuildKit secret writes them into
  the image at /opt/hf. Nothing is fetched at runtime, which is what an
  air-gapped host wants. Optional now; it used to be the only way.

  FETCHED -- someone saves a token in the dashboard and POST /models pulls them
  onto the shared volume. The token lives in settings.json (chmod 0600,
  redacted on read-out) beside every other credential the product manages.
  This is the default path because it is the only one that can say WHICH gate
  is unaccepted; a failed `docker build` cannot.

The server process is permanently offline: _load sets HF_HUB_OFFLINE before
importing huggingface_hub, and that constant is read at import. Fetching
therefore happens in a SUBPROCESS -- the one part of this service allowed to
reach the network, and only when asked.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from pathlib import Path

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from .. import keys as k

app = FastAPI(title="OpenXray engine-speakers", version="0.1.0")

#: Baked into the IMAGE at build time. Checked first: if it holds weights the
#: operator asked for a self-contained image and nothing should reach out.
BAKED_CACHE = "/opt/hf"

#: Fetched at RUN time, onto the shared volume -- not the container filesystem,
#: so `up --force-recreate` does not discard a 100 MB download. Surviving
#: recreate for free, by being an image layer, is the one real advantage the
#: baked path keeps.
RUNTIME_CACHE = os.environ.get("XRAY_HF_CACHE") or "/timelines/hf"

_FETCH_SCRIPT = "scripts/fetch_speaker_models.py"

#: Where the conditions are accepted. Sent to the dashboard so it can link
#: each unaccepted gate directly instead of describing where to look.
HF_URL = "https://huggingface.co/"
GATED_REPOS = (
    "pyannote/speaker-diarization-3.1",
    "pyannote/segmentation-3.0",
    "pyannote/speaker-diarization-community-1",
)

_pipeline = None
_load_error = ""
_lock = threading.Lock()

#: Outcome of the last fetch, so /health can explain a failure that happened
#: minutes ago in a request the dashboard has long since forgotten.
_last_fetch: dict = {}
_fetching = False
_fetch_lock = threading.Lock()


def _has_weights(path: str) -> bool:
    """Whether a cache directory actually holds model files.

    Existence is not enough: HF_HOME gets created by anything that touches the
    library, so an empty /timelines/hf would otherwise read as ready.
    """
    p = Path(path)
    if not p.exists():
        return False
    return any(p.rglob("*.bin")) or any(p.rglob("*.safetensors"))


def _cache() -> str:
    """The cache to load from: baked wins, else the volume."""
    return BAKED_CACHE if _has_weights(BAKED_CACHE) else RUNTIME_CACHE


def _load():
    """Build the pipeline once. Slow (weights + torch import), so it happens
    on first use rather than at import, which would make the container look
    unhealthy for a minute after every restart."""
    global _pipeline, _load_error
    if _pipeline is not None or _load_error:
        return
    with _lock:
        if _pipeline is not None or _load_error:
            return
        try:
            cache = _cache()
            os.environ["HF_HOME"] = cache
            # Set BEFORE the import that reads it. Loading only ever wants
            # local files, so a process that has got this far should not be
            # able to reach HuggingFace at all -- on an air-gapped host a
            # stray lookup is a hang, not an error.
            os.environ.setdefault("HF_HUB_OFFLINE", "1")
            Path(cache).mkdir(parents=True, exist_ok=True)
            import torch
            from pyannote.audio import Pipeline

            # Docker gets no MPS and usually no CUDA, so this is CPU-bound in
            # practice. torch defaults to ONE thread, which measured ~1.2x
            # realtime against ~3.6x threaded -- a 3x difference for a line.
            torch.set_num_threads(int(os.environ.get("XRAY_VOICE_THREADS", "8")))
            pipe = Pipeline.from_pretrained("pyannote/speaker-diarization-3.1")
            if pipe is None:
                # from_pretrained returns None (not raises) when the weights
                # are absent or unreadable. /health has already reported the
                # real state by the time a request gets here, so this points
                # at that rather than guessing which cause applies.
                raise RuntimeError(
                    "no usable weights in " + cache
                    + ": fetch them from the dashboard (Setup → Speakers)")
            if torch.cuda.is_available():
                pipe.to(torch.device("cuda"))
            _pipeline = pipe
        except Exception as e:                     # noqa: BLE001 (reported)
            _load_error = f"{type(e).__name__}: {e}"


def _repo_root() -> Path:
    """/app in the container: xray/service/engine_speakers.py → parents[2]."""
    return Path(__file__).resolve().parents[2]


def _state() -> dict:
    """What this service can do right now, WITHOUT touching the network.

    /health is polled, so it never calls HuggingFace. It reports what is on
    disk and whether a credential exists; the network questions -- is the token
    valid, which gate is missing -- are answered by POST /models, which is a
    deliberate act.
    """
    cache = _cache()
    baked = _has_weights(BAKED_CACHE)
    if _has_weights(cache):
        return {"state": "ready", "cache": cache, "baked": baked}
    if _fetching:
        return {"state": "fetching", "cache": cache, "baked": baked}
    # A failed attempt outranks no-token/needs-fetch: it is the more specific
    # thing to say, and it is the one that can name the gate.
    if _last_fetch and not _last_fetch.get("ok"):
        return {"state": _last_fetch.get("state") or "load-failed",
                "cache": cache, "baked": baked,
                "gated": _last_fetch.get("gated") or [],
                "message": _last_fetch.get("message") or ""}
    if not k.hf_token():
        return {"state": "no-token", "cache": cache, "baked": baked}
    return {"state": "needs-fetch", "cache": cache, "baked": baked}


class DiarizeRequest(BaseModel):
    audio_path: str
    min_speakers: int | None = None
    max_speakers: int | None = None


@app.get("/health")
def health():
    """Readiness WITHOUT loading the model: the orchestrator polls this before
    offering the pass, and a probe that takes a minute is worse than one that
    answers honestly."""
    return {"ok": True, "loaded": _pipeline is not None, "error": _load_error,
            "tokenConfigured": bool(k.hf_token()),
            "gatedRepos": list(GATED_REPOS), "hfUrl": HF_URL, **_state()}


def _diagnose(env: dict) -> dict:
    """Ask the script which failure state applies. A subprocess for the same
    offline reason, and cheap: whoami plus three metadata requests."""
    try:
        proc = subprocess.run(
            [sys.executable, _FETCH_SCRIPT, "--diagnose"],
            cwd=str(_repo_root()), env=env, capture_output=True, text=True,
            timeout=60)
        return json.loads(proc.stdout.strip().splitlines()[-1])
    except Exception:                              # noqa: BLE001 (best effort)
        return {"state": "load-failed", "gated": []}


@app.post("/models")
def fetch_models():
    """Fetch the weights now with the configured token, and report why not.

    Runs the fetch script as a subprocess: this process is offline for life
    (see _load), so a child is the only thing that may reach the network.
    Blocking on purpose -- the dashboard is waiting on it, and a fire-and-poll
    design would need progress plumbing to say anything a spinner does not.
    """
    global _fetching, _last_fetch, _load_error, _pipeline
    with _fetch_lock:
        if _fetching:
            raise HTTPException(409, "a fetch is already running")
        _fetching = True
    try:
        env = dict(os.environ)
        env["HF_HOME"] = RUNTIME_CACHE     # never write into the baked cache
        env["HF_TOKEN"] = k.hf_token()
        env.pop("HF_HUB_OFFLINE", None)    # the child is the online one
        proc = subprocess.run(
            [sys.executable, _FETCH_SCRIPT], cwd=str(_repo_root()), env=env,
            capture_output=True, text=True, timeout=3600)
        log = (proc.stdout + proc.stderr).strip()
        ok = proc.returncode == 0 and _has_weights(RUNTIME_CACHE)

        # The script's own verdict, asked for rather than parsed out of the
        # log, so the state string has exactly one author.
        diag = {"state": "ok", "gated": []} if ok else _diagnose(env)
        _last_fetch = {"ok": ok, "state": "ok" if ok else diag.get("state"),
                       "gated": diag.get("gated") or [], "message": log}
        if ok:
            # An earlier load failed for want of weights, and that verdict is
            # now stale. Without this the pass keeps reporting the old error
            # and the only fix is restarting the container.
            with _lock:
                _load_error = ""
                _pipeline = None
        # _state() first: `message` is spread after it so this call's own log
        # wins over the stored one, which is the same string today but need
        # not stay that way.
        return {**_state(), "ok": ok, "message": log}
    except subprocess.TimeoutExpired:
        _last_fetch = {"ok": False, "state": "load-failed", "gated": [],
                       "message": "the download did not finish within an hour"}
        return {**_state(), "ok": False, "message": _last_fetch["message"]}
    finally:
        _fetching = False


def _wav_frames(path: Path):
    """((channel, time) float32 ndarray, sample_rate), or None if not a PCM
    WAV this can read.

    Split out from _read_wav and kept TORCH-FREE so it can be tested outside
    the engine image, which is the only place torch is installed. The part that
    can actually be wrong is here -- the 16-bit check and the interleaved-to-
    (channel, time) transpose -- while the tensor wrap is one line that cannot.
    """
    import wave
    try:
        with wave.open(str(path), "rb") as w:
            if w.getsampwidth() != 2:          # not 16-bit; let pyannote try
                return None
            channels, rate = w.getnchannels(), w.getframerate()
            frames = w.readframes(w.getnframes())
    except (wave.Error, OSError, EOFError):
        return None

    import numpy as np
    a = np.frombuffer(frames, dtype="<i2").astype("float32") / 32768.0
    # pyannote wants (channel, time); interleaved samples transpose to that.
    a = a.reshape(1, -1) if channels == 1 else a.reshape(-1, channels).T
    return np.ascontiguousarray(a), rate


def _read_wav(path: Path):
    """A PCM WAV as pyannote's in-memory form, or None if it is not one.

    WHY THIS EXISTS. pyannote 4.x dropped torchaudio's loaders for torchcodec,
    whose default wheel is CUDA-linked: in a CPU-only image it fails to load
    with `libnvrtc.so.13: cannot open shared object file` and every diarize
    dies with "torchcodec is not available". Handing the pipeline a waveform is
    pyannote's own documented alternative -- its warning lists it first -- and
    it takes the whole torch/torchcodec/FFmpeg ABI chain out of the hot path,
    so a future wheel resolution cannot break this again.

    Safe because we produce the file ourselves: frames.extract_audio writes
    mono 16 kHz pcm_s16le. Anything else falls through to the path, and the
    container carries a working CPU torchcodec for that case.

    No memory penalty: pyannote loads the whole waveform regardless (its
    Inference calls model.audio(file) before doing anything), so this only
    changes WHO reads the bytes. Measured 0.4s and 267 MB for 70 minutes.
    """
    got = _wav_frames(path)
    if got is None:
        return None
    samples, rate = got
    import torch
    return {"waveform": torch.from_numpy(samples), "sample_rate": rate}


@app.post("/diarize")
def diarize(req: DiarizeRequest):
    path = Path(req.audio_path)
    if not path.exists():
        raise HTTPException(400, f"no such audio file: {path}")
    _load()
    if _pipeline is None:
        raise HTTPException(503, _load_error or "pipeline unavailable")

    kw = {}
    if req.min_speakers:
        kw["min_speakers"] = req.min_speakers
    if req.max_speakers:
        kw["max_speakers"] = req.max_speakers

    source = _read_wav(path)
    print(f"[diarize] {path.name}: "
          + ("preloaded waveform" if source else "decoding via pyannote"),
          flush=True)
    if source is None:
        source = str(path)

    try:
        out = _pipeline(source, return_embeddings=True, **kw)
    except TypeError:
        # Older pyannote has no return_embeddings; degrade to turns only
        # rather than failing, since turns alone still support labelling
        # within one title (they just cannot propagate to another).
        out = _pipeline(source, **kw)

    ann = getattr(out, "speaker_diarization", out)
    turns = [[round(float(seg.start), 3), round(float(seg.end), 3), str(spk)]
             for seg, _, spk in ann.itertracks(yield_label=True)]
    labels = [str(x) for x in ann.labels()]

    emb = getattr(out, "speaker_embeddings", None)
    embeddings = None
    if emb is not None:
        # NaN rows happen for speakers with too little audio to embed. Sent
        # through as null rather than dropped: the caller must still be able
        # to line embeddings up with `labels` by index.
        import numpy as np
        embeddings = [None if np.isnan(row).any() else [round(float(v), 6)
                                                        for v in row]
                      for row in np.asarray(emb, dtype=float)]

    return {"turns": turns, "labels": labels, "embeddings": embeddings}
