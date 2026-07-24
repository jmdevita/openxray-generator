"""Landmark (Shazam-style) audio fingerprinting: free, local, no DB server.

Same constellation-hash technique as Dejavu (the decision-ledger pick), but
implemented directly on numpy + scipy so the indexer needs no MySQL/Postgres
server, which is a real win for a self-hosted container. Matches only recordings we put
in the DB (the user's own library), which is exactly the plan's scope: identify
*owned* songs that appear, near-zero false positives (plan.md §6.4).

Pipeline: audio → magnitude spectrogram → constellation of spectral peaks →
pair peaks into (f1, f2, Δt) hashes → match a stream against the DB by finding a
consistent time-offset (a histogram spike = that reference plays at that spot).
"""
from __future__ import annotations

import subprocess
from collections import defaultdict

import numpy as np
from scipy import signal
from scipy.ndimage import maximum_filter

# --- parameters (Shazam/Dejavu-like, scaled to SR) ------------------------
SR = 11025            # fingerprinting sample rate
N_FFT = 1024
HOP = 512             # ~46 ms/frame
PEAK_NEIGHBORHOOD = 15
AMP_MIN_DB = -50      # keep peaks within this many dB of the spectrogram max
FAN_VALUE = 15        # pairs per anchor peak
DT_MIN, DT_MAX = 1, 100   # target-zone Δt in frames (~0.05–4.6 s)
MS_PER_FRAME = HOP / SR * 1000.0


def load_audio(path, sr=SR):
    """Decode any audio/video file to mono float32 at `sr` via ffmpeg."""
    cmd = ["ffmpeg", "-nostdin", "-v", "error", "-i", str(path),
           "-vn", "-ac", "1", "-ar", str(sr), "-f", "s16le", "-"]
    raw = subprocess.run(cmd, capture_output=True, check=True).stdout
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0


def _spectrogram_db(x):
    _, _, Sxx = signal.spectrogram(x, fs=SR, window="hann", nperseg=N_FFT,
                                   noverlap=N_FFT - HOP, mode="magnitude")
    return 20.0 * np.log10(Sxx + 1e-6)   # (freq_bins, time_frames)


def _peaks(S):
    # square neighborhood → separable max-filter → fast on hour-long tracks
    local_max = maximum_filter(S, size=PEAK_NEIGHBORHOOD) == S
    detected = local_max & (S > S.max() + AMP_MIN_DB)
    freqs, frames = np.where(detected)
    order = np.lexsort((freqs, frames))   # sort by time, then freq
    return list(zip(frames[order].tolist(), freqs[order].tolist()))  # (t, f)


def _hashes(peaks):
    out = []
    n = len(peaks)
    for i in range(n):
        t1, f1 = peaks[i]
        for j in range(1, FAN_VALUE + 1):
            if i + j >= n:
                break
            t2, f2 = peaks[i + j]
            dt = t2 - t1
            if dt < DT_MIN:
                continue
            if dt > DT_MAX:
                break   # peaks time-sorted → Δt only grows from here
            h = (f1 & 0x3FF) | ((f2 & 0x3FF) << 10) | ((dt & 0xFFF) << 20)
            out.append((h, t1))
    return out


def fingerprint(x):
    """(list of (hash, frame_time), n_frames) for a mono float32 signal at SR."""
    S = _spectrogram_db(x)
    return _hashes(_peaks(S)), S.shape[1]


def frames_to_ms(frames):
    return int(round(frames * MS_PER_FRAME))


class Fingerprinter:
    """In-memory reference DB + stream matcher."""

    def __init__(self):
        self.db = defaultdict(list)   # hash -> [(song_id, ref_frame)]
        self.songs = {}               # song_id -> {n_frames, n_hashes, meta}

    def add(self, song_id, x, meta=None):
        hashes, n_frames = fingerprint(x)
        for h, t in hashes:
            self.db[h].append((song_id, t))
        self.songs[song_id] = {"n_frames": n_frames, "n_hashes": len(hashes),
                               "meta": meta or {}}
        return len(hashes)

    def add_file(self, song_id, path, meta=None):
        return self.add(song_id, load_audio(path), meta)

    def match_stream(self, x, min_votes=100, merge_offset=3):
        """Find where each DB song appears in `x`.

        For a reference present at stream offset O, matching hashes satisfy
        (stream_frame - ref_frame) == O. So per song we bucket votes by that
        offset; a bucket with many votes is an occurrence. Nearby offsets
        (STFT jitter) are merged; well-separated offsets stay distinct
        occurrences.
        """
        hashes, n_frames = fingerprint(x)
        per_song = defaultdict(lambda: defaultdict(list))  # song -> offset -> [stream_frames]
        for h, st in hashes:
            for song_id, rt in self.db.get(h, ()):
                per_song[song_id][st - rt].append(st)

        occ = []
        for song_id, offmap in per_song.items():
            offsets = sorted(offmap)
            i = 0
            while i < len(offsets):
                cluster = [offsets[i]]
                j = i + 1
                while j < len(offsets) and offsets[j] - cluster[-1] <= merge_offset:
                    cluster.append(offsets[j])
                    j += 1
                sts = sorted(s for o in cluster for s in offmap[o])
                if len(sts) >= min_votes:
                    occ.append({
                        "song_id": song_id,
                        "votes": len(sts),
                        "start_ms": frames_to_ms(sts[0]),
                        "end_ms": frames_to_ms(sts[-1]),
                    })
                i = j
        occ.sort(key=lambda o: -o["votes"])
        return occ, n_frames
