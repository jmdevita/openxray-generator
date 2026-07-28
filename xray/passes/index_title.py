"""Core generation pass: birth a timeline for a Plex-resident title.

Contract-native from the first byte (unification plan U1): the TMDb id comes
from the Plex Guids the server already exposes, so the file is born
content-keyed (`tmdb-movie-769.json`), manifest-mapped ("plex:<ratingKey>"),
provenance-stamped, validated. A title with no TMDb id has no content
identity and is refused. Trivia is NOT fetched here; that's the trivia pass.

Pipeline: Plex metadata → frames over the direct-play URL → YuNet/SFace →
HDBSCAN cluster → label against TMDb reference headshots → actorIntervals.
"""
from __future__ import annotations

import sys
import time
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

from .. import engines, progress, refs as refsmod, schema, store as st
from ..faces import cluster as clu
from ..frames import extract_frames
from ..sources.base import MediaSource


@dataclass
class IndexOptions:
    fps: float = 0.5
    threshold: float = 0.363
    min_cluster_size: int = 5
    min_run: int = 2
    start_s: float = 0.0
    duration_s: float | None = None
    max_frames: int = 0


def merge_preserved(old: dict, new: dict) -> dict:
    """Carry enrichment blocks from an existing timeline into a re-indexed one.

    Re-indexing regenerates ONLY the core (cast + actorIntervals + faces
    provenance); music, trivia, and per-actor person data are other passes'
    work, because losing them on re-index would throw away paid AudD calls and
    cached enrichment. Person data merges by actorId (cast lists can shift
    between TMDb snapshots)."""
    # A seed must never overwrite an index. run_level0 writes empty intervals
    # and no faces stamp by design, so without this a level-0 pass across a
    # library silently downgrades every title already indexed — throwing away
    # the minutes of frame decoding and face embedding that produced them.
    # "New has no faces stamp" is exactly what distinguishes a seed from a
    # re-index, which does regenerate both and must be allowed to replace them.
    if "faces" not in (new.get("provenance") or {}) and "faces" in (old.get("provenance") or {}):
        new["actorIntervals"] = old.get("actorIntervals") or []
        new.setdefault("provenance", {})["faces"] = old["provenance"]["faces"]
    for block in ("musicIntervals", "trivia"):
        if old.get(block):
            new[block] = old[block]
    # Display labels are re-fetched on every index, but a bad TMDb response
    # must not silently strip a title the timeline already carried.
    for key in ("title", "year", "series"):
        if key not in new and old.get(key):
            new[key] = old[key]
    old_prov = old.get("provenance") or {}
    for block in ("music", "trivia", "people"):
        if block in old_prov:
            new.setdefault("provenance", {})[block] = old_prov[block]
    persons = {c["actorId"]: c["person"] for c in old.get("cast") or []
               if c.get("person") and c.get("actorId")}
    if persons:
        for c in new.get("cast") or []:
            if c.get("actorId") in persons:
                c["person"] = persons[c["actorId"]]
    return new


def faces_to_hits(det_faces: list[dict], frames) -> tuple[list, list]:
    """Service /analyze results → (embeddings, FaceHits), keeping only faces
    whose frame is in [frames] (drops frames trimmed by --max-frames and any
    stray files in the dir). Timestamps come from the extraction record,
    the service knows filenames, not media time."""
    ts_by_index = {fr.index: fr.timestamp_ms for fr in frames}
    embeddings, hits = [], []
    for f in det_faces:
        ts = ts_by_index.get(f["frame_index"])
        if ts is None:
            continue
        embeddings.append(np.asarray(f["embedding"], dtype=np.float32))
        hits.append(clu.FaceHit(f["frame_index"], ts))
    return embeddings, hits


def content_id_for(item: dict) -> str | None:
    """Content identity from Plex metadata (guids + episode indices)."""
    if item["type"] == "episode":
        tmdb = item.get("showTmdbId")
        if tmdb and item.get("season") is not None and item.get("episode") is not None:
            return st.episode_content_id(tmdb, item["season"], item["episode"])
        return None
    tmdb = item.get("tmdbId")
    return st.movie_content_id(tmdb) if tmdb else None


def resolve(source: MediaSource, *, rating_key: str | None, search: str | None) -> dict:
    tag = source.key_prefix
    if rating_key:
        return source.resolve(rating_key)
    if not search:
        raise SystemExit("need --rating-key or --search")
    results = source.search(search)
    playable = [r for r in results if r["type"] in ("episode", "movie")]
    if not playable:
        raise SystemExit(f"no playable items for {search!r}")
    print(f"[{tag}] candidates:")
    for r in playable[:8]:
        label = r["title"]
        if r["type"] == "episode":
            label = f"{r['grandparentTitle']} S{r['season']}E{r['episode']} · {r['title']}"
        print(f"   ratingKey {str(r['ratingKey']):>8}  [{r['type']}]  {label} ({r.get('year') or ''})")
    chosen = playable[0]
    print(f"[{tag}] using ratingKey {chosen['ratingKey']} (first match; "
          f"pass --rating-key to override)")
    return source.resolve(chosen["ratingKey"])


def run(store_dir: Path, work_dir: Path, *, source: MediaSource,
        tmdb_key: str, rating_key: str | None = None, search: str | None = None,
        opts: IndexOptions = IndexOptions(), dry_run: bool = False) -> dict:
    item = resolve(source, rating_key=rating_key, search=search)
    print(f"[{source.key_prefix}] {item['type']} '{item['title']}' "
          f"ratingKey={item['ratingKey']} "
          f"(~{(item.get('durationMs') or 0) // 60000} min)")

    content_id = content_id_for(item)
    if content_id is None:
        raise SystemExit(
            "no TMDb id on this item's guids, so it has no content identity "
            "and cannot be indexed (check the library's metadata agent)")

    if item["type"] == "episode":
        bundle = refsmod.episode_bundle(item["showTmdbId"], tmdb_key,
                                        season=item["season"],
                                        episode=item["episode"])
    else:
        bundle = refsmod.movie_bundle(item["tmdbId"], tmdb_key)
    cast, labels = bundle["cast"], bundle["labels"]

    if dry_run:
        print(f"[dry-run] would index → {content_id or item['ratingKey']} "
              f"({len(cast)} cast refs)")
        return {"contentId": content_id, "ratingKey": item["ratingKey"], "dryRun": True}

    # Face work goes through the engine seam: in-process by default, or the
    # engine-faces service when XRAY_ENGINE_FACES_URL is set (compose stack;
    # frames/refs then live on the volume shared with the service).
    transport = engines.face_transport()
    embedder = None
    if transport is None:
        embedder = engines.face_engine()
    else:
        ok, msg = transport.ready()
        if not ok:
            raise SystemExit(msg)

    # Harvest the audio in the SAME network pull, where the music pass caches
    # its extract (music_work/<stem>/<stem>__audio.mp3): the media then
    # crosses the wire exactly once per title.
    tl_stem = content_id
    audio_out = store_dir / "music_work" / tl_stem / f"{tl_stem}__audio.mp3"
    print(f"[frames] extracting @ {opts.fps} fps from the media stream "
          f"(+ audio harvest) …")
    # ffmpeg reports no usable count until it finishes, so this phase is a
    # label with no bar. progress.fraction() returns 0 for a missing total
    # rather than inventing a position.
    progress.emit("frames")
    t0 = time.time()
    frames = extract_frames(item["downloadUrl"], work_dir / "frames",
                            sample_fps=opts.fps, start_s=opts.start_s,
                            duration_s=opts.duration_s,
                            audio_out=None if audio_out.exists() else audio_out)
    if opts.max_frames:
        frames = frames[: opts.max_frames]
    print(f"[frames] {len(frames)} frames [{time.time() - t0:.0f}s]")

    if transport is None:
        embeddings, hits = [], []
        # The one phase with an exact denominator: `frames` is already a list,
        # so this is the cheapest honest percentage in the pipeline. Emitting
        # per frame would put thousands of lines on the channel, so report at
        # most ~50 times regardless of how long the film is.
        every = max(1, len(frames) // 50)
        progress.emit("faces", 0, len(frames))
        for i, fr in enumerate(frames, 1):
            img = cv2.imread(fr.path)
            if img is not None:
                for det in embedder.detect(img):
                    embeddings.append(embedder.embed(img, det))
                    hits.append(clu.FaceHit(fr.index, fr.timestamp_ms))
            # Counts frames READ, not frames with a face: an unreadable or
            # empty frame is still work done, and a bar that stalled on a
            # faceless stretch would be lying.
            if i % every == 0 or i == len(frames):
                progress.emit("faces", i, len(frames))
        model_version = embedder.model_version
    else:
        # One blocking call into the engine service; no counter to read.
        progress.emit("faces")
        model_version, det_faces = transport.analyze(work_dir / "frames")
        embeddings, hits = faces_to_hits(det_faces, frames)
    print(f"[faces]  {len(embeddings)} faces across {len(frames)} frames")
    if not embeddings:
        raise SystemExit("no faces detected")

    progress.emit("matching")
    labels = clu.cluster_embeddings(embeddings, min_cluster_size=opts.min_cluster_size)
    centroids = clu.cluster_centroids(embeddings, labels)
    print(f"[refs]   building references for {len(cast)} cast members …")
    if transport is None:
        refs = refsmod.build_reference_embeddings(cast, embedder)
    else:
        refs = refsmod.build_reference_embeddings_http(
            cast, transport, work_dir / "refs")
    cluster_to_actor = clu.label_clusters(centroids, refs, threshold=opts.threshold)
    intervals = clu.build_intervals(hits, labels, cluster_to_actor, opts.fps,
                                    min_run=opts.min_run)

    progress.emit("writing")
    doc = schema.timeline(content_id, refsmod.public_cast(cast),
                          intervals, model_version,
                          duration_ms=item.get("durationMs"), labels=labels)

    dest = _write_doc(store_dir, item, content_id, doc, source.key_prefix)

    print(f"[out] {len(intervals)} intervals → {dest.name} "
          f"(+ manifest {source.key_prefix}:{item['ratingKey']})" if content_id else
          f"[out] {len(intervals)} intervals → {dest.name}")
    return {"contentId": content_id, "ratingKey": item["ratingKey"],
            "file": str(dest), "intervals": len(intervals), "cast": len(cast)}


def _write_doc(store_dir: Path, item: dict, content_id: str, doc: dict,
               key_prefix: str) -> Path:
    """Write [doc] into the store: merge-preserve any existing enrichment and
    map the backend lookup key. Shared by the full index pass and the level-0
    seed. content_id is required; callers stop before here without one."""
    import json as _json
    dest = st.canonical_path(store_dir, content_id)
    if dest.exists():  # rewrite: keep the other passes' work
        doc = merge_preserved(_json.loads(dest.read_text()), doc)
        print("[merge] preserved music/trivia/person blocks from the "
              "existing timeline")
    st.write_timeline(dest, doc)
    st.map_lookup(store_dir, f"{key_prefix}:{item['ratingKey']}", content_id)
    return dest


def run_level0(store_dir: Path, *, source, tmdb_key: str,
               rating_key: str | None = None, search: str | None = None,
               dry_run: bool = False) -> dict:
    """Level-0 seed: birth a timeline with NO video work at all.

    Cast list and display labels from TMDb in one request per title
    (thumb-only, no per-person image calls), empty interval arrays
    (clients render the full-cast panel), runtime from server metadata.
    Seconds per title, so a whole library seeds in minutes; the people and
    trivia passes then enrich it like any other timeline. A later full index
    upgrades it in place (merge_preserved keeps everything seeded here).
    No `provenance.faces` stamp is written; its absence is what marks a
    timeline as level-0 and lets the pipeline offer the upgrade."""
    item = resolve(source, rating_key=rating_key, search=search)
    print(f"[{source.key_prefix}] {item['type']} '{item['title']}' "
          f"ratingKey={item['ratingKey']} (level-0 seed)")

    content_id = content_id_for(item)
    if content_id is None:
        raise SystemExit(
            "no TMDb id on this item's guids, so it has no content identity "
            "and cannot be seeded (check the library's metadata agent)")

    if item["type"] == "episode":
        bundle = refsmod.episode_bundle(item["showTmdbId"], tmdb_key,
                                        season=item["season"],
                                        episode=item["episode"], max_images=0)
    else:
        bundle = refsmod.movie_bundle(item["tmdbId"], tmdb_key, max_images=0)
    cast, labels = bundle["cast"], bundle["labels"]

    if dry_run:
        print(f"[dry-run] would seed → {content_id or item['ratingKey']} "
              f"({len(cast)} cast)")
        return {"contentId": content_id, "ratingKey": item["ratingKey"],
                "dryRun": True}

    doc = schema.timeline(content_id, refsmod.public_cast(cast),
                          [], None,
                          duration_ms=item.get("durationMs"), labels=labels)

    dest = _write_doc(store_dir, item, content_id, doc, source.key_prefix)
    print(f"[out] level-0 seed {dest.stem} ({len(cast)} cast) → {dest.name}")
    return {"contentId": content_id, "ratingKey": item["ratingKey"],
            "file": str(dest), "intervals": 0, "cast": len(cast)}
