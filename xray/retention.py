"""What the passes leave behind, and when it is safe to delete.

Indexing never downloads the video: ffmpeg reads the media server's URL and
writes only derivatives. But three of those derivatives are large, and none of
them are output. Measured on a three-title store, 556 MB of intermediate
against 700 KB of timeline:

    index_work/frames/          156 MB   sampled JPEGs, one title's worth
    music_work/<cid>/           208 MB   harvested MP3 + AudD probe clips
    speakers_work/<cid>/        191 MB   16 kHz mono WAV + audition clips

All three are re-derivable from the media server, so deleting one costs
another pull and nothing else. Two are load-bearing until a later step
consumes them, and that judgement is the reason this module exists: the index
pass, `xray clean` and the dashboard all have to make it the same way.
"""
from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path

#: Directory name per kind, relative to the store.
WORK_DIRS = {"frames": "index_work", "music": "music_work",
             "speakers": "speakers_work"}

KINDS = ("frames", "music", "speakers")

LABELS = {
    "frames": "Sampled frames",
    "music": "Harvested audio",
    "speakers": "Diarization audio",
    "crops": "Face crops",
    "other": "Timelines and caches",
}

NOTES = {
    "frames": "JPEGs the face pass read. Nothing reads them afterwards.",
    "music": "The audio track, pulled during indexing so the music pass "
             "does not stream the title a second time.",
    "speakers": "The audio the diarizer ran on. The labelling screen cuts "
                "its audition clips from it.",
    "crops": "Exemplar faces for the labelling screen. Small, and there is "
             "no way to rebuild them without re-indexing.",
    "other": "Timelines, faceprints, voiceprints and API caches.",
}


@dataclass(frozen=True)
class Chunk:
    """One deletable directory, and why it is or is not deletable now."""
    path: Path
    bytes: int
    kind: str
    content_id: str = ""
    #: Empty when this can go. Otherwise the reason it is being kept, phrased
    #: for someone reading it in a dashboard rather than a log.
    holding: str = ""

    @property
    def reclaimable(self) -> bool:
        return not self.holding


def human(n: int) -> str:
    """Bytes as a person would say them. Mirrored in dashboard.js."""
    if n < 1000:
        return f"{n} B"
    for unit in ("KB", "MB", "GB"):
        n /= 1000.0
        if n < 1000 or unit == "GB":
            return f"{n:.1f} {unit}".replace(".0 ", " ")


def dir_bytes(path: Path) -> int:
    """Apparent size of a directory tree. Missing files are skipped rather
    than raised: a pass writing into this tree while it is surveyed is normal,
    and a number one frame stale is fine where an exception is not."""
    total = 0
    for p in Path(path).rglob("*"):
        try:
            if p.is_file():
                total += p.stat().st_size
        except OSError:
            continue
    return total


def _stamped(store: Path, content_id: str, block: str) -> bool:
    """Whether a pass has finished for this title, per its own provenance."""
    from . import store as st
    try:
        doc = json.loads(st.canonical_path(store, content_id).read_text())
    except (OSError, ValueError):
        return False
    return block in (doc.get("provenance") or {})


def _speakers_holding(store: Path, content_id: str) -> str:
    """The WAV is needed until every nameable speaker has a name.

    /api/speakers/{cid}/clip/{speaker} stitches its audition passages out of
    this file, so deleting it mid-labelling replaces the play button with
    "the extracted audio is gone; re-run the pass" -- a full re-diarize to
    recover a click.
    """
    from . import voiceprints as vp
    doc = vp.read_clusters(store, content_id)
    if not doc:
        # Clusters gone but audio still here: the pass died after the pull, or
        # somebody deleted the title. Either way nothing can use the audio.
        return ""
    nameable = [s for s in doc.get("speakers") or [] if s.get("enrollable")]
    if not nameable:
        return ""
    names = _read_names(store / "speakers" / f"{content_id}.names.json")
    left = sum(1 for s in nameable if s["speaker"] not in names)
    if not left:
        return ""
    return f"{left} speaker{'s' if left != 1 else ''} still to name"


def _read_names(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def _music_holding(store: Path, content_id: str) -> str:
    if _stamped(store, content_id, "music"):
        return ""
    return "the music pass has not run yet"


def survey(store_dir, *, busy=()) -> dict:
    """Everything on disk, grouped by kind, with per-title hold reasons."""
    store = Path(store_dir)
    chunks = _chunks(store, busy=busy)
    total = dir_bytes(store)
    crops = dir_bytes(store / "faces" / "crops")
    counted = sum(c.bytes for c in chunks) + crops
    groups = [_group(kind, chunks) for kind in KINDS]
    groups.append({"kind": "crops", "label": LABELS["crops"],
                   "note": NOTES["crops"], "bytes": crops,
                   "reclaimable": 0, "items": []})
    groups.append({"kind": "other", "label": LABELS["other"],
                   "note": NOTES["other"], "bytes": max(total - counted, 0),
                   "reclaimable": 0, "items": []})
    return {"total": total,
            "reclaimable": sum(c.bytes for c in chunks if c.reclaimable),
            "held": sum(c.bytes for c in chunks if not c.reclaimable),
            "kinds": [g for g in groups if g["bytes"]]}


def _group(kind: str, chunks: list[Chunk]) -> dict:
    mine = [c for c in chunks if c.kind == kind]
    return {
        "kind": kind, "label": LABELS[kind], "note": NOTES[kind],
        "bytes": sum(c.bytes for c in mine),
        "reclaimable": sum(c.bytes for c in mine if c.reclaimable),
        "items": [{"contentId": c.content_id, "bytes": c.bytes,
                   "holding": c.holding} for c in mine if c.holding],
    }


@dataclass
class Result:
    freed: int = 0
    removed: list = field(default_factory=list)
    kept: list = field(default_factory=list)


def clean(store_dir, *, kinds=KINDS, busy=(), dry_run: bool = False) -> Result:
    """Delete every intermediate the survey called reclaimable.

    Refuses to leave the work directories even if a policy above ever gets a
    path wrong: this deletes whole trees, and the blast radius of a bug here
    is somebody's timelines.
    """
    store = Path(store_dir)
    roots = {(store / WORK_DIRS[k]).resolve() for k in WORK_DIRS}
    out = Result()
    for chunk in _chunks(store, busy=busy):
        if chunk.kind not in kinds:
            continue
        if not chunk.reclaimable:
            out.kept.append(chunk)
            continue
        if not any(r in chunk.path.resolve().parents or r == chunk.path.resolve()
                   for r in roots):
            raise RuntimeError(f"refusing to delete outside the work dirs: "
                               f"{chunk.path}")
        if not dry_run:
            shutil.rmtree(chunk.path, ignore_errors=True)
        out.freed += chunk.bytes
        out.removed.append(chunk)
    return out


def _chunks(store: Path, *, busy=()) -> list[Chunk]:
    """Every deletable directory, with its hold reason resolved.

    `busy` is the content ids a run currently has in flight. Frames are
    blocked by ANY of them: that directory is shared across titles rather
    than per-title, so there is no way to tell whose frames are in it.
    """
    busy = set(busy)
    chunks: list[Chunk] = []
    frames = store / "index_work" / "frames"
    if frames.is_dir():
        chunks.append(Chunk(frames, dir_bytes(frames), "frames",
                            holding="a run is using them" if busy else ""))
    for kind, holder in (("music", _music_holding),
                         ("speakers", _speakers_holding)):
        root = store / WORK_DIRS[kind]
        for d in sorted(root.glob("*")) if root.is_dir() else ():
            if d.is_dir():
                hold = ("a run is using them" if d.name in busy
                        else holder(store, d.name))
                chunks.append(Chunk(d, dir_bytes(d), kind, d.name, hold))
    return chunks


def drop_frames(work_dir) -> int:
    """Delete a finished index's frames. Returns the bytes freed.

    Called by the pass itself: `extract_frames` already wipes this directory
    at the START of the next run, so keeping them buys exactly one run's worth
    of debugging and costs a title's frames sitting there indefinitely -- 156
    MB, on the episode this was measured on.
    """
    frames = Path(work_dir) / "frames"
    if not frames.is_dir():
        return 0
    n = dir_bytes(frames)
    shutil.rmtree(frames, ignore_errors=True)
    return n
