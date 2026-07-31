"""Speakers pass: find who talks and when, for titles faces cannot handle.

Animated titles have no human faces to detect, so `index_title` refuses them
(TMDb genre 16). This is the other half of that refusal: pull the audio,
diarize it, and store the clusters.

WHAT THIS PASS DELIBERATELY DOES NOT DO: write actorIntervals. Diarization
produces anonymous speakers -- "someone talks here" -- and turning those into
"Shrek talks here" needs a person. So the pass ends in a state no other pass
has: computed, correct, and not finished. The dashboard picks it up from
there, and intervals are written when a human names the clusters.

Keeping the human step OUT of the pipeline is deliberate. A pass that blocked
on input would stall a batch run and hold a worker thread for however long it
takes someone to notice.
"""
from __future__ import annotations

import time
from pathlib import Path

from .. import engines, progress, refs as refsmod, schema
from .. import voiceprints as vp
from ..frames import extract_audio
from ..sources.base import MediaSource
from .index_title import Unsupported, _write_doc, content_id_for, resolve

#: This pass's phases. progress.PHASE_WEIGHTS is the FACE pass's vocabulary
#: (frames/faces/matching/writing) and does not describe this one at all: no
#: frames, and the long pole is a single opaque call into the engine.
PHASE_WEIGHTS = (("audio", 0.25), ("diarize", 0.70), ("writing", 0.05))


def run(store_dir: Path, work_dir: Path, *, source: MediaSource,
        tmdb_key: str, rating_key: str | None = None,
        search: str | None = None, dry_run: bool = False) -> dict:
    item = resolve(source, rating_key=rating_key, search=search)
    print(f"[{source.key_prefix}] {item['type']} '{item['title']}' "
          f"ratingKey={item['ratingKey']} "
          f"(~{(item.get('durationMs') or 0) // 60000} min)")

    content_id = content_id_for(item)
    if content_id is None:
        raise Unsupported(
            "no TMDb id on this item, so it has no content identity")

    # Check the engine BEFORE the audio pull -- and this now covers the model
    # weights too, not just the container. Discovering either after streaming a
    # feature wastes exactly the expensive part, the same reasoning that puts
    # the animation gate ahead of frame extraction.
    transport = engines.speaker_transport()
    if transport is None:
        raise Unsupported(
            "speaker diarization is not available: start it with "
            "`docker compose --profile speakers up -d`")
    ok, why = transport.ready()
    if not ok:
        raise Unsupported(why)

    if item["type"] == "episode":
        bundle = refsmod.episode_bundle(item["showTmdbId"], tmdb_key,
                                        season=item["season"],
                                        episode=item["episode"])
    else:
        bundle = refsmod.movie_bundle(item["tmdbId"], tmdb_key)
    cast, labels = bundle["cast"], bundle["labels"]

    if dry_run:
        print(f"[dry-run] would diarize → {content_id} ({len(cast)} cast)")
        return {"contentId": content_id, "dryRun": True}

    # --- audio ------------------------------------------------------------
    audio_out = store_dir / "speakers_work" / content_id / f"{content_id}.wav"
    duration_ms = item.get("durationMs") or 0
    print("[audio] pulling the audio track (no video decode) …")
    progress.emit("audio")
    last = [-1]

    def on_audio(done_ms, total_ms):
        pct = int(100 * done_ms / total_ms) if total_ms else 0
        if pct != last[0]:
            last[0] = pct
            progress.emit("audio", done_ms, total_ms)

    t0 = time.time()
    # `downloadUrl` is what the seam actually exposes and what the face
    # pass streams from; there is no stream_url().
    extract_audio(item["downloadUrl"], audio_out,
                  on_progress=on_audio if duration_ms else None,
                  duration_ms=duration_ms or None)
    print(f"[audio] {audio_out.stat().st_size/1e6:.0f} MB "
          f"in {time.time()-t0:.0f}s")

    # --- diarize ----------------------------------------------------------
    # One opaque call: the engine gives no intermediate progress, so the phase
    # label carries the information instead of a bar that would sit still.
    print("[diarize] finding speakers (this is the slow part) …")
    progress.emit("diarize")
    t0 = time.time()
    out = transport.diarize(audio_out)
    turns, spk_labels = out["turns"], out["labels"]
    embeddings = out.get("embeddings")
    print(f"[diarize] {len(spk_labels)} speakers, {len(turns)} turns "
          f"in {time.time()-t0:.0f}s")

    # --- store clusters ---------------------------------------------------
    progress.emit("writing")
    generated = schema.now_iso()
    vp.write_clusters(store_dir, content_id, turns=turns, labels=spk_labels,
                      embeddings=embeddings, generated=generated,
                      version="pyannote-3.1")

    # A timeline is still written, with cast and NO intervals: the same shape
    # a level-0 seed has. It is a valid, shareable document that says who is
    # in this title, and it upgrades in place once someone names the speakers.
    doc = schema.timeline(content_id, cast, actor_intervals=[],
                          duration_ms=duration_ms or None, labels=labels)
    doc["provenance"]["speakers"] = {"generated": generated,
                                     "version": "pyannote-3.1"}
    # _write_doc already merge-preserves an existing timeline and maps the
    # backend lookup key. Hand-rolling that here duplicated it and got the
    # store API wrong twice (st.map_key does not exist; it is st.map_lookup).
    _write_doc(store_dir, item, content_id, doc, source.key_prefix)

    named = sum(1 for s in spk_labels)
    over = sum(1 for s in vp.read_clusters(store_dir, content_id)["speakers"]
               if s["enrollable"])
    print(f"[speakers] {named} speakers found, {over} with enough audio to "
          f"name. Nobody is named yet — that part needs you.")
    return {"contentId": content_id, "speakers": named, "nameable": over,
            "needsLabelling": True}
