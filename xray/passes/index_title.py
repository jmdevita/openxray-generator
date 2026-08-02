"""Core generation pass: birth a timeline for a Plex-resident title.

Contract-native from the first byte (unification plan U1): the TMDb id comes
from the Plex Guids the server already exposes, so the file is born
content-keyed (`tmdb-movie-769.json`), manifest-mapped ("plex:<ratingKey>"),
provenance-stamped, validated. A title with no TMDb id has no content
identity and is refused. Trivia is NOT fetched here; that's the trivia pass.

Pipeline: Plex metadata → frames over the direct-play URL → YuNet/SFace →
HDBSCAN cluster → label against reference headshots → actorIntervals. Which
headshots is `enrollment_cast`'s business, not this pipeline's.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

from .. import (engines, faceprints as fpmod, keys, progress,
                refs as refsmod, retention, schema, store as st)
from ..faces import cluster as clu, crops
from ..frames import extract_frames
from ..sources import commons
from ..sources.base import MediaSource


class Unsupported(Exception):
    """This title is outside what the pass can do, and retrying won't help.

    Distinct from a failure: nothing went wrong, the input simply isn't
    something a human-face detector can process. `pipeline.step` records it as
    `skipped:` so the dashboard shows a limitation rather than an error, and a
    batch run over a mixed library doesn't look broken because it contains
    cartoons.
    """


@dataclass
class IndexOptions:
    fps: float = 0.5
    #: Was SFace's generic 0.363, which put 122 seconds of one actor under
    #: another's name; faceprints.MATCH_THRESHOLD carries the measurement.
    threshold: float = fpmod.MATCH_THRESHOLD
    min_cluster_size: int = 5
    min_run: int = 2
    start_s: float = 0.0
    duration_s: float | None = None
    max_frames: int = 0


#: Provenance blocks whose pass PRODUCES actorIntervals. A doc carrying none
#: of these is a seed (run_level0), which writes an empty interval list by
#: design and must never overwrite real work.
IDENTITY_BLOCKS = ("faces", "voice")

#: Which `source` value each block's intervals carry. Intervals predating the
#: field have no `source`, and those are all face-derived, so absent reads as
#: "face" rather than as unknown.
_BLOCK_SOURCE = {"faces": "face", "voice": "voice"}


def _interval_source(interval: dict) -> str:
    return interval.get("source") or "face"


def merge_preserved(old: dict, new: dict) -> dict:
    """Carry enrichment blocks from an existing timeline into a re-indexed one.

    Re-indexing regenerates ONLY what the pass that ran actually produces;
    music, trivia, and per-actor person data are other passes' work, because
    losing them on re-index would throw away paid AudD calls and cached
    enrichment. Person data merges by actorId (cast lists can shift between
    TMDb snapshots)."""
    old_prov = old.get("provenance") or {}
    new_prov = new.get("provenance") or {}
    old_ident = {b for b in IDENTITY_BLOCKS if b in old_prov}
    new_ident = {b for b in IDENTITY_BLOCKS if b in new_prov}

    if not new_ident and old_ident:
        # A seed. run_level0 writes empty intervals and no identity stamp by
        # design, so without this a level-0 pass across a library silently
        # downgrades every title already indexed — throwing away the minutes
        # of frame decoding and face embedding that produced them.
        new["actorIntervals"] = old.get("actorIntervals") or []
        for block in old_ident:
            new.setdefault("provenance", {})[block] = old_prov[block]
    elif new_ident:
        # A real pass. It may replace ONLY the sources it regenerated: a voice
        # pass must not delete face intervals, and vice versa. Keying on the
        # bare presence of a faces stamp (the previous rule) meant any pass
        # that wasn't faces had its intervals silently swapped for the old
        # face ones while its own provenance block survived — a file claiming
        # a pass ran while carrying none of its output.
        regenerated = {_BLOCK_SOURCE[b] for b in new_ident}
        kept = [iv for iv in (old.get("actorIntervals") or [])
                if _interval_source(iv) not in regenerated]
        merged = kept + list(new.get("actorIntervals") or [])
        merged.sort(key=lambda iv: (iv.get("startMs") or 0,
                                    iv.get("endMs") or 0))
        new["actorIntervals"] = merged
        for block in old_ident - new_ident:
            new.setdefault("provenance", {})[block] = old_prov[block]

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
        box = f.get("bbox")
        hits.append(clu.FaceHit(f["frame_index"], ts,
                                tuple(box) if box else None))
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


def enrollment_cast(cast, store_dir: Path, *, on_progress=None):
    """The same cast with its REFERENCE PHOTOS swapped for the chosen source.

    Identity is deliberately untouched -- same actorIds, names, characters --
    so the timeline, the manifest, and every downstream join are unaffected
    by which photos happened to enroll the references. Only `images` moves.

    The document keeps TMDb's `thumb` for display: that is metadata use, not
    ML use, and Commons portraits are thinner (many cast members have none),
    so swapping the display too would trade a licence question we do not have
    for a visibly emptier cast panel.
    """
    if keys.enrollment_source() != "commons":
        return cast
    out = commons.cast_with_cache(cast, Path(store_dir) / commons.CACHE_NAME,
                                  on_progress=on_progress)
    # Coverage, not a comparison. This once printed how many photos the other
    # source would have had, which invited "so why not use that one?" in a log
    # line people paste into issues -- a licence question answered badly, by
    # the wrong medium. The number that helps is how many of this cast can be
    # matched automatically; the rest are named on the labelling screen.
    have = sum(1 for m in out if m.get("images"))
    print(f"[refs]   reference photos for {have} of {len(cast)} cast; "
          f"the rest can be named by hand once the pass finishes")
    return out


def _apply_faceprints(store_dir, content_id, centroids, cluster_to_actor):
    """Let faces named in EARLIER episodes name themselves here.

    Cast photos go first: they cover the whole cast, while a print only
    exists for someone already named by hand. Mutates `cluster_to_actor`, so
    this pass's intervals carry the inherited names.
    """
    key = fpmod.series_key(content_id or "")
    if not key:
        return 0
    store = fpmod.read_prints(store_dir)
    if not store:
        return 0
    # Same series only. Cross-title propagation is plausible but unmeasured,
    # and a face that ages five years between shows is exactly where this
    # would go wrong; those stay suggestions on the labelling screen.
    refs = {}
    for actor_id, rec in store.items():
        if fpmod.series_key(rec.get("from") or "") != key:
            continue
        v = np.asarray(rec["embedding"], dtype=np.float32)
        n = np.linalg.norm(v)
        if n:
            refs[actor_id] = v / n
    if not refs:
        return 0
    unnamed = {lab: c for lab, c in centroids.items()
               if lab not in cluster_to_actor}
    got = clu.label_clusters(unnamed, refs,
                             threshold=fpmod.PROPAGATE_THRESHOLD)
    cluster_to_actor.update(got)
    if got:
        print(f"[refs]   {len(got)} cluster(s) named from faces you named in "
              f"earlier episodes")
    return len(got)


def _record_clusters(store_dir, content_id, cluster_labels, hits, centroids,
                     matched, frames, *, runtime_ms, fps, model_version):
    """Write the cluster document + exemplar crops for the labelling screen.

    Best-effort by design: this is an aid to a later, optional human step,
    and a timeline that indexed cleanly must not be failed because a crop
    could not be written.
    """
    if not content_id:
        return
    try:
        doc = fpmod.build_clusters(
            content_id=content_id, labels=cluster_labels, hits=hits,
            centroids=centroids, matched=matched, runtime_ms=runtime_ms,
            sample_fps=fps, generated=schema.now_iso(), version=model_version)
        fpmod.write_clusters(store_dir, content_id, doc)
        n = crops.write_crops(doc, frames,
                              fpmod.crops_dir(store_dir, content_id))
        offer = sum(1 for c in doc["clusters"]
                    if c["nameable"] and not c["matched"])
        print(f"[faces]  {len(doc['clusters'])} clusters kept, {offer} "
              f"unnamed and worth naming ({n} crop montages)")
    except Exception as e:                      # noqa: BLE001 - see docstring
        print(f"[faces]  (could not record clusters for naming: {e})")


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

    # Refuse animation BEFORE the extraction, not after. The face stack is
    # YuNet + SFace; both need five-point human landmarks, and most animated
    # principals are not humanoid (Donkey, Puss, Gingy). Running anyway costs
    # a full media pull and yields an empty actorIntervals with no
    # explanation, which reads as a bug rather than a limitation.
    # docs/ANIMATION.md carries the assessment and the voice alternative.
    if bundle.get("animated"):
        # Redirect, not a dead end. Until the speakers pass existed this said
        # "use level 0", which was the honest answer then and is the wrong one
        # now: there IS a way to index an animated title, it just listens
        # instead of looking.
        raise Unsupported(
            "animated title — faces need human facial landmarks and most "
            "animated characters have none. Run the speakers pass instead: "
            "it finds who talks and when, then asks you to name them")

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
    # Measured against media time, not frames written: ffmpeg reports how far
    # into the file it has decoded, and the runtime is already known from the
    # media server, so this needs no extra probe.
    progress.emit("frames")
    t0 = time.time()

    # ffmpeg reports roughly twice a second; over a feature that is hundreds
    # of markers for a bar with a hundred positions. Emit only when the whole
    # percentage point changes.
    last_pct = [-1]

    def frames_progress(done_ms, total_ms):
        pct = int(100 * done_ms / total_ms) if total_ms else 0
        if pct != last_pct[0]:
            last_pct[0] = pct
            progress.emit("frames", done_ms, total_ms)

    frames = extract_frames(item["downloadUrl"], work_dir / "frames",
                            sample_fps=opts.fps, start_s=opts.start_s,
                            duration_s=opts.duration_s,
                            audio_out=None if audio_out.exists() else audio_out,
                            duration_ms=item.get("durationMs"),
                            on_progress=frames_progress)
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
                    hits.append(clu.FaceHit(fr.index, fr.timestamp_ms,
                                            tuple(det.bbox)))
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

    # Still "faces": clustering IS face work, and holding the label steady
    # here is honest where a new one would imply the slow part had ended.
    # NOT `labels`: that name already holds the display {title, year, series}
    # from the TMDb bundle above, and rebinding it here sent an HDBSCAN array
    # into schema.timeline(labels=...), which raised on `labels or {}` and
    # killed every full index at the final write.
    cluster_labels = clu.cluster_embeddings(
        embeddings, min_cluster_size=opts.min_cluster_size)
    centroids = clu.cluster_centroids(embeddings, cluster_labels)

    # Two counted phases, not one. Both walk the cast and both are network-
    # bound, but they are minutes apart in cost and `progress.advance` is
    # monotonic within a phase -- run under one label, the second would sit at
    # 100% for as long as it took, which is what "stuck" looks like.
    def tick(phase):
        return lambda done, total: progress.emit(phase, done, total)

    progress.emit("enrolling")
    enroll_cast = enrollment_cast(cast, store_dir,
                                  on_progress=tick("enrolling"))

    progress.emit("matching")
    print(f"[refs]   building references for {len(cast)} cast members …")
    if transport is None:
        refs = refsmod.build_reference_embeddings(
            enroll_cast, embedder, on_progress=tick("matching"))
    else:
        refs = refsmod.build_reference_embeddings_http(
            enroll_cast, transport, work_dir / "refs",
            on_progress=tick("matching"))
    cluster_to_actor = clu.label_clusters(centroids, refs, threshold=opts.threshold)
    _apply_faceprints(store_dir, content_id, centroids, cluster_to_actor)
    intervals = clu.build_intervals(hits, cluster_labels, cluster_to_actor,
                                    opts.fps, min_run=opts.min_run)

    # Clusters the references could not name are not waste: on live action
    # they are mostly real, credited characters whose only problem is that
    # no free-licensed photo of them exists. Persist every cluster (plus a
    # few exemplar crops) so a person can name them afterwards, and do it
    # while the frames still exist -- they are deleted at the end of this call.
    _record_clusters(store_dir, content_id, cluster_labels, hits, centroids,
                     cluster_to_actor, frames,
                     runtime_ms=item.get("durationMs") or 0, fps=opts.fps,
                     model_version=model_version)

    progress.emit("writing")
    doc = schema.timeline(content_id, refsmod.public_cast(cast),
                          intervals, model_version,
                          duration_ms=item.get("durationMs"), labels=labels)

    dest = _write_doc(store_dir, item, content_id, doc, source.key_prefix)

    # Only now: the crops above are the last reader, and a pass that failed
    # earlier keeps its frames for a look. Harvested audio stays -- the music
    # pass has not run yet, and re-pulling it is a second trip over the wire.
    freed = retention.drop_frames(work_dir)
    if freed:
        print(f"[clean] {retention.human(freed)} of frames removed")

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
