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


def _progress_ms(line: str) -> int | None:
    """Media position from one ffmpeg `-progress` line, in ms.

    `out_time_us` is preferred and unambiguous. `out_time_ms` is NOT
    milliseconds in most ffmpeg builds (it carries microseconds), so it is
    deliberately ignored rather than trusted, and `out_time=HH:MM:SS.ss` is
    the fallback because it cannot be misread.
    """
    key, sep, value = line.strip().partition("=")
    if not sep:
        return None
    if key == "out_time_us" and value.lstrip("-").isdigit():
        return max(0, int(value) // 1000)
    if key == "out_time":
        try:
            hh, mm, ss = value.split(":")
            return max(0, round(
                (int(hh) * 3600 + int(mm) * 60 + float(ss)) * 1000))
        except (ValueError, TypeError):
            return None
    return None


def extract_frames(video_path, out_dir, sample_fps=1.0, max_height=720,
                   quality=3, start_s=0.0, duration_s=None,
                   audio_out=None, on_progress=None,
                   duration_ms=None) -> list[Frame]:
    """Sample `sample_fps` frames/sec, scaled to fit within 1280x`max_height`.

    `start_s`/`duration_s` limit extraction to a segment (input seeking: fast,
    good for a quick trial before a full-episode run). Timestamps are absolute
    media time: frame k at ~ start_s + (k-1)/sample_fps seconds, which is what
    actorIntervals are anchored to.

    `audio_out`: also write the audio track as a compact stereo MP3 in the
    SAME decode pass: the media crosses the network once and the music pass
    finds this file instead of re-streaming. Only honored when start_s == 0 so
    audio timestamps stay aligned to media time.

    `on_progress(done_ms, total_ms)` is called as ffmpeg advances. This is the
    longest phase of an index for a feature — the whole file is streamed and
    decoded — and without it the dashboard has nothing to show for minutes.
    Needs `duration_ms` (the caller already knows it from the media server, so
    no extra probe); given neither, extraction runs exactly as before.
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
    total_ms = round(duration_s * 1000) if duration_s else duration_ms
    if on_progress and total_ms:
        # -progress writes key=value lines to stdout as it goes. -nostats and
        # -loglevel error keep stderr down to real errors: ffmpeg's usual
        # per-frame chatter would fill the stderr pipe while we are busy
        # reading stdout, and both sides would block forever.
        cmd = cmd[:1] + ["-nostats", "-loglevel", "error",
                         "-progress", "pipe:1"] + cmd[1:]
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True)
        for line in proc.stdout:
            done = _progress_ms(line)
            if done is not None:
                on_progress(min(done, total_ms), total_ms)
        proc.stdout.close()
        stderr = proc.stderr.read()
        proc.stderr.close()
        if proc.wait() != 0:
            raise RuntimeError(f"ffmpeg failed:\n{stderr[-2000:]}")
    else:
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
