"""Frame extraction: ffmpeg SDR sampling at a low fps, 720p (plan.md §6.3).

SDR only: HDR/Dolby-Vision handling is explicitly out of scope (decision
ledger). One decode pass, output-side fps filter (uniform sampling), no -ss
seeking.
"""
from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Frame:
    index: int          # 1-based ffmpeg output frame number
    timestamp_ms: int   # approximate media time this sample was taken from
    path: str


def probe_duration_ms(video_path) -> int | None:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "json", str(video_path)],
        capture_output=True, text=True, check=True,
    )
    dur = json.loads(out.stdout).get("format", {}).get("duration")
    return int(float(dur) * 1000) if dur else None


def extract_frames(video_path, out_dir, sample_fps=1.0, max_height=720,
                   quality=3, start_s=0.0, duration_s=None,
                   audio_out=None) -> list[Frame]:
    """Sample `sample_fps` frames/sec, scaled to fit within 1280x`max_height`.

    `start_s`/`duration_s` limit extraction to a segment (input seeking: fast,
    good for a quick trial before a full-episode run). Timestamps are absolute
    media time: frame k at ~ start_s + (k-1)/sample_fps seconds, which is what
    actorIntervals are anchored to.

    `audio_out`: also write the audio track as a compact stereo MP3 in the
    SAME decode pass: the media crosses the network once and the music pass
    finds this file instead of re-streaming. Only honored when start_s == 0 so
    audio timestamps stay aligned to media time.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    for stale in out_dir.glob("frame_*.jpg"):
        stale.unlink()

    vf = (f"fps={sample_fps},"
          f"scale=w=1280:h={max_height}:force_original_aspect_ratio=decrease")
    cmd = ["ffmpeg", "-nostdin", "-y"]
    if start_s:
        cmd += ["-ss", str(start_s)]          # input seek (before -i) = fast
        audio_out = None                       # offset audio would lie about time
    cmd += ["-i", str(video_path)]
    if duration_s:
        cmd += ["-t", str(duration_s)]
    cmd += ["-vf", vf, "-q:v", str(quality), str(out_dir / "frame_%06d.jpg")]
    if audio_out:
        audio_out = Path(audio_out)
        audio_out.parent.mkdir(parents=True, exist_ok=True)
        if duration_s:  # -t is per-output; the frames' -t doesn't cover this one
            cmd += ["-t", str(duration_s)]
        cmd += ["-map", "0:a:0", "-vn", "-ac", "2", "-ar", "44100",
                "-c:a", "libmp3lame", "-b:a", "128k", str(audio_out)]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg failed:\n{proc.stderr[-2000:]}")

    interval_ms = 1000.0 / sample_fps
    start_ms = round(start_s * 1000)
    frames: list[Frame] = []
    for p in sorted(out_dir.glob("frame_*.jpg")):
        i = int(p.stem.split("_")[1])
        frames.append(Frame(index=i,
                            timestamp_ms=start_ms + round((i - 1) * interval_ms),
                            path=str(p)))
    return frames
