"""Music pass: detect music cues, identify one probe per cue via AudD.

Sources: a local media file (--media) OR the title's Plex direct-play URL
(U1: same source the frames came from, with no hand-supplied paths). Audio is
extracted once to a compact mp3, cached in the work dir.

Engine access goes through the seam (engines.audio_segmenter), so this pass is
transport-agnostic when the compose stack lands.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from .. import engines, musiccues as mc, store as st
from ..music import discovery as disc

BLOCK_VERSION = "audd-v1"


def extract_audio(source: str, work: Path, stem: str) -> Path:
    """Media path/URL → compact stereo MP3 (timeline offsets preserved)."""
    out = work / f"{stem}__audio.mp3"
    src_path = Path(source)
    if out.exists() and src_path.exists() and out.stat().st_mtime >= src_path.stat().st_mtime:
        return out
    if out.exists() and not src_path.exists():   # URL source: reuse cache
        return out
    work.mkdir(parents=True, exist_ok=True)
    cp = subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-i", source,
         "-map", "0:a:0", "-vn", "-ac", "2", "-ar", "44100",
         "-c:a", "libmp3lame", "-b:a", "128k", str(out)],
        capture_output=True, text=True,
    )
    if cp.returncode != 0:
        raise RuntimeError(f"audio extract failed: {cp.stderr.strip()}")
    return out


def run(store_dir: Path, key: str, audd_token: str, *,
        media: str | None = None, source=None, min_music: float = 10.0,
        merge_gap: float = 15.0, max_cues: int = 80,
        monthly_budget: int = 300, dry_run: bool = False) -> dict:
    """`source` is a MediaSource (sources/base.py) used to stream the title's
    audio when no local --media and no harvested track are available."""
    files = st.resolve_timelines(store_dir, [key])
    tl_path = files[0]
    doc = json.loads(tl_path.read_text())

    seg = engines.audio_segmenter()
    ok, msg = seg.ready()
    if not ok:
        raise SystemExit(msg)
    if not audd_token and not dry_run:
        raise SystemExit("no AudD token (.auddtoken or AUDD_API_TOKEN): "
                         "https://dashboard.audd.io/")

    # Audio source, cheapest first: the index pass harvests the track during
    # its frame pull (music_work/<stem>/<stem>__audio.mp3): if that exists,
    # no source flags and no network are needed at all.
    work = store_dir / "music_work" / tl_path.stem
    harvested = work / f"{tl_path.stem}__audio.mp3"
    if media is None and harvested.exists():
        audio = harvested
        print(f"[audio] using harvested audio ({harvested.name})")
    else:
        src = media
        if src is None:
            if source is None:
                raise SystemExit("no harvested audio for this title; need "
                                 "--media or a media backend to stream from")
            # The server item id comes from the MANIFEST, not the doc: the
            # contract carries no server-local id, so a timeline generated
            # after the schema cleanup has nothing to read here. Prefer the
            # newest mapping when a title has more than one.
            cid = doc.get("contentId") or tl_path.stem
            ids = st.backend_ids(store_dir, cid, source.key_prefix)
            if not ids:
                raise SystemExit(
                    f"{cid} is not mapped to any {source.key_prefix} item, so "
                    f"there is no stream to pull audio from. Index this title "
                    f"on this server first, or pass --media with a local file.")
            item = source.resolve(ids[-1])
            src = item["downloadUrl"]
            print(f"[audio] streaming from {source.key_prefix} "
                  f"({source.key_prefix}:{ids[-1]})")
        audio = extract_audio(src, work, tl_path.stem)
    cues = seg.segment(audio, work, min_music_seconds=min_music, merge_gap=merge_gap)
    print(f"{len(cues)} music cue(s) ({sum(c.duration for c in cues):.0f}s of music)")
    if len(cues) > max_cues:
        print(f"capping at {max_cues} cues", file=sys.stderr)
        cues = cues[:max_cues]

    if dry_run:
        for i, c in enumerate(cues, 1):
            print(f"  cue {i:2d}: {int(c.start // 60)}:{int(c.start % 60):02d}"
                  f"-{int(c.end // 60)}:{int(c.end % 60):02d} ({c.duration:.0f}s)")
        print("(dry run: no API calls, nothing written)")
        return {"cues": len(cues), "dryRun": True}

    from ..budget import AuddBudget
    budget = AuddBudget(store_dir, monthly_budget)
    headroom = budget.headroom()
    if headroom is not None and len(cues) > headroom:
        if headroom == 0:
            raise SystemExit(f"AudD monthly budget exhausted "
                             f"({budget.used}/{budget.monthly}); retry next month "
                             f"or raise --audd-budget")
        print(f"[budget] only {headroom} AudD call(s) left this month; "
              f"processing the first {headroom} cue(s)", file=sys.stderr)
        cues = cues[:headroom]

    matches = []
    for i, cue in enumerate(cues, 1):
        m = disc.identify_cue(audio, cue, audd_token, work, tag=str(i))
        state = f"{m.title} · {m.artist}" if m.matched else (m.error or "no match")
        print(f"  cue {i:2d}/{len(cues)} "
              f"[{int(cue.start // 60)}:{int(cue.start % 60):02d}] {state}")
        matches.append(m)

    budget.spend(len(matches))

    # Persist EVERY cue, not just the recognised ones. Segmentation is the
    # reliable half of this pass and identification is not: the first feature
    # this ran against produced 31 good cues and one name. The rest are not
    # waste, they are work for a person who can hear them (see musiccues.py).
    cid = doc.get("contentId") or tl_path.stem
    cue_doc = mc.build_cues(content_id=cid, cues=cues, matches=matches,
                            generated=st.now_iso(), version=BLOCK_VERSION)
    mc.write_cues(store_dir, cid, cue_doc)

    intervals = mc.intervals(store_dir, cid)
    named = sum(1 for c in cue_doc["cues"] if c["matched"])
    print(f"\n{len(intervals)} distinct song interval(s) from {named} of "
          f"{len(cues)} cue(s)  [AudD this month: {budget.used}"
          + (f"/{budget.monthly}]" if budget.monthly > 0 else "]"))
    if named < len(cues):
        print(f"{len(cues) - named} cue(s) unidentified — name them in the "
              f"dashboard and the intervals fill in.")
    doc["musicIntervals"] = intervals
    st.stamp(doc, "music", BLOCK_VERSION)
    st.write_timeline(tl_path, doc)
    print(f"wrote {tl_path.name}")
    return {"cues": len(cues), "songs": len(intervals)}
