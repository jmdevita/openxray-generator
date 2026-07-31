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


#: HTTP input options, and they belong BEFORE -i (they configure the protocol,
#: not the output). Without these, ffmpeg treats a media server closing the
#: connection mid-file as end-of-stream: it exits 0 with a partial file and
#: nothing looks wrong. Measured against a remote Plex origin, a 2 GB pull died
#: at 67% after half an hour, and the only evidence was the output duration.
#:
#: `-reconnect_streamed` is the one that matters here -- a Plex part URL is not
#: seekable to ffmpeg, and the plain `-reconnect` does not cover that case.
_HTTP_RETRY = ["-reconnect", "1", "-reconnect_streamed", "1",
               "-reconnect_on_network_error", "1",
               "-reconnect_delay_max", "10"]


def _input_opts(video_path) -> list[str]:
    """Retry options, but only for a network input: passing them for a local
    file makes ffmpeg warn about unused options on every single call."""
    return _HTTP_RETRY if str(video_path).startswith(("http://", "https://")) \
        else []


def extract_audio(video_path, audio_out, *, on_progress=None,
                  duration_ms=None) -> Path:
    """Pull ONLY the audio track. No video decode at all.

    The speakers pass needs audio and nothing else, so `-vn` before the input
    map means ffmpeg never touches a video frame. That makes this strictly
    cheaper than extract_frames' combined pull, not an extra cost: the same
    media crosses the wire, minus the decoding.

    Mono 16 kHz because that is what the diarizer wants; feeding it 44.1 kHz
    stereo just makes it downmix on the other side. WAV rather than MP3 to
    skip an encode/decode round trip whose artefacts land in the embeddings.
    """
    import subprocess
    audio_out = Path(audio_out)
    audio_out.parent.mkdir(parents=True, exist_ok=True)
    cmd = (["ffmpeg", "-nostdin", "-y"] + _input_opts(video_path)
           + ["-i", str(video_path),
              "-vn", "-map", "0:a:0", "-ac", "1", "-ar", "16000",
              "-c:a", "pcm_s16le", str(audio_out)])
    if on_progress and duration_ms:
        # Same deadlock guard as extract_frames: without -nostats the
        # per-frame chatter fills stderr while we read stdout and both block.
        cmd = cmd[:1] + ["-nostats", "-loglevel", "error",
                         "-progress", "pipe:1"] + cmd[1:]
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True)
        for line in proc.stdout:
            ms = _progress_ms(line)
            if ms is not None:
                on_progress(min(ms, duration_ms), duration_ms)
        proc.wait()
        if proc.returncode:
            raise RuntimeError(
                f"ffmpeg audio extract failed: {proc.stderr.read()[-400:]}")
    else:
        cp = subprocess.run(cmd, capture_output=True, text=True)
        if cp.returncode:
            raise RuntimeError(
                f"ffmpeg audio extract failed: {cp.stderr[-400:]}")
    if duration_ms:
        _check_full_length(audio_out, duration_ms)
    return audio_out


#: How much shorter than the expected runtime an extract may be before it is
#: treated as truncated. Container audio and video streams legitimately differ
#: by a second or two, and a trailing-silence trim is normal; a tenth of a film
#: is not.
_SHORT_AUDIO_TOLERANCE = 0.02


def _check_full_length(audio_out: Path, duration_ms: int) -> None:
    """Fail loudly when the pull came back short.

    A zero exit from ffmpeg is NOT proof the whole track arrived: on an HTTP
    input that closes early it treats the truncation as end-of-stream and exits
    0 with a partial file. Silence there is the worst outcome available -- the
    intervals that got written would all be correct, so nothing downstream
    could tell that the last third of the film was never examined, and the
    timeline would be published as complete.

    Free to check: the header of a PCM WAV carries the frame count.
    """
    import wave
    try:
        with wave.open(str(audio_out), "rb") as w:
            got_s = w.getnframes() / float(w.getframerate() or 1)
    except (wave.Error, OSError, EOFError, ZeroDivisionError):
        return                      # not a WAV we can measure; nothing to say
    want_s = duration_ms / 1000.0
    if got_s >= want_s * (1 - _SHORT_AUDIO_TOLERANCE):
        return
    raise RuntimeError(
        f"audio extract is short: got {got_s/60:.1f} min of a "
        f"{want_s/60:.1f} min title ({100*got_s/want_s:.0f}%). ffmpeg exited "
        f"cleanly, which on an HTTP source usually means the connection "
        f"closed early -- diarizing this would silently cover only part of "
        f"the title. Re-run to retry the pull.")


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
    cmd = ["ffmpeg", "-nostdin", "-y"] + _input_opts(video_path)
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
