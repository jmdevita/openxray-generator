"""Song discovery for music cues: probe sampling + AudD recognition.

Engine choice (validated in audio_matching_analysis, 2026-07-18): AudD most
reliably returns the canonical recording on mixed film audio; AcoustID never
matches (wrong algorithm family); Demucs separation doesn't help. Long cues are
sampled at several points and MERGED into one ~30s probe clip sent as a single
API call: this recovered real needle-drops a single sample missed, at no extra
cost (~$0.005/cue after AudD's 300 free requests).
"""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import requests

from .segments import MusicSegment

AUDD_URL = "https://api.audd.io/"

SNIPPET = 10.0          # seconds per snippet in a multi-window probe
MAX_SNIPPETS = 3
SHORT_CUE = 25.0        # cues up to this length get one plain window


# --- probe building --------------------------------------------------------

def probe_windows(seg: MusicSegment) -> list[tuple[float, float]]:
    """(start, length) snippets to sample from a cue. Short cue → one window;
    long cue → up to MAX_SNIPPETS spread across it (skipping the edges)."""
    dur = seg.duration
    if dur <= SHORT_CUE:
        s = seg.start + min(4.0, max(dur - 1.0, 0.0))
        return [(round(s, 2), round(min(20.0, seg.end - s), 2))]
    n = min(MAX_SNIPPETS, max(2, int(dur // 45)))
    wins = []
    for i in range(n):
        frac = (i + 1) / (n + 1)
        s = max(seg.start, min(seg.start + frac * dur - SNIPPET / 2,
                               seg.end - SNIPPET))
        wins.append((round(s, 2), SNIPPET))
    return wins


def build_probe(audio: Path, windows: list[tuple[float, float]], out: Path) -> Path:
    """Concatenate the snippets into one mono probe clip (a single API call)."""
    cmd = ["ffmpeg", "-y", "-v", "error"]
    for s, ln in windows:
        cmd += ["-ss", str(s), "-t", str(ln), "-i", str(audio)]
    n = len(windows)
    if n == 1:
        cmd += ["-ac", "1", "-ar", "44100"]
    else:
        filt = "".join(f"[{i}:a]" for i in range(n)) + f"concat=n={n}:v=0:a=1[a]"
        cmd += ["-filter_complex", filt, "-map", "[a]", "-ac", "1", "-ar", "44100"]
    cmd += ["-c:a", "libmp3lame", "-b:a", "64k", str(out)]
    cp = subprocess.run(cmd, capture_output=True, text=True)
    if cp.returncode != 0:
        raise RuntimeError(f"ffmpeg probe failed: {cp.stderr.strip()}")
    return out


# --- AudD ------------------------------------------------------------------

@dataclass
class CueMatch:
    cue: MusicSegment
    title: Optional[str] = None
    artist: Optional[str] = None
    error: Optional[str] = None

    @property
    def matched(self) -> bool:
        return bool(self.title)


def audd_identify(probe: Path, token: str, timeout: float = 60) -> dict:
    """One AudD call. Returns the raw response dict (result may be None)."""
    with open(probe, "rb") as fh:
        r = requests.post(
            AUDD_URL,
            data={"api_token": token, "return": "timecode,spotify"},
            files={"file": fh},
            timeout=timeout,
        )
    return r.json()


def identify_cue(audio: Path, seg: MusicSegment, token: str, work: Path,
                 tag: str) -> CueMatch:
    m = CueMatch(cue=seg)
    try:
        probe = build_probe(audio, probe_windows(seg), work / f"probe_{tag}.mp3")
        resp = audd_identify(probe, token)
        if resp.get("status") != "success":
            m.error = f"audd: {(resp.get('error') or {}).get('error_message', resp.get('status'))}"
            return m
        result = resp.get("result")
        if result:
            m.title = result.get("title")
            m.artist = result.get("artist")
    except Exception as e:  # noqa: BLE001 (a failed cue must not sink the pass)
        m.error = f"{type(e).__name__}: {e}"
    return m


# --- consolidation ---------------------------------------------------------

_norm_re = re.compile(r"[^a-z0-9]+")


def _norm(s: Optional[str]) -> str:
    return _norm_re.sub(" ", (s or "").lower()).strip()


def _same_song(a: str, b: str) -> bool:
    na, nb = _norm(a), _norm(b)
    return bool(na) and (na in nb or nb in na)


@dataclass
class SongInterval:
    title: str
    artist: Optional[str]
    start: float
    end: float
    n_cues: int = 1
    matches: list = field(default_factory=list)


def consolidate(matches: list[CueMatch]) -> list[SongInterval]:
    """Collapse consecutive same-song cue matches into one time range."""
    out: list[SongInterval] = []
    for m in matches:
        if not m.matched:
            continue
        prev = out[-1] if out else None
        if prev and _same_song(prev.title, m.title):
            prev.end = m.cue.end
            prev.n_cues += 1
        else:
            out.append(SongInterval(title=m.title, artist=m.artist,
                                    start=m.cue.start, end=m.cue.end))
    return out


def to_music_intervals(songs: list[SongInterval]) -> list[dict]:
    """Contract shape for timeline musicIntervals (SCHEMA.md)."""
    return [
        {
            "title": s.title,
            "artist": s.artist,
            "startMs": int(s.start * 1000),
            "endMs": int(s.end * 1000),
            "confidence": None,          # AudD exposes no numeric score
            "source": "audd",
        }
        for s in songs
    ]
