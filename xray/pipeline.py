"""The per-title pipeline as a library: shared by the CLI (`xray run`) and
the orchestrator service. One implementation, two frontends (plan U2/U2b).

Each pass is provenance-gated and failure-isolated: one bad title must not
sink a batch. All output goes through the injectable `log` callable so the
orchestrator can capture per-job logs.
"""
from __future__ import annotations

import contextlib
import io
import json
import sys
from pathlib import Path

from . import progress as pg, store as st
from .sources.base import MediaSource


class Cancelled(Exception):
    """Raised out of a pass when the caller asks a running job to stop.

    Distinct from a pass failing: `step` re-raises this instead of recording
    it, because a cancelled title must abandon its remaining passes rather
    than carry on into people/trivia.
    """


class _LineSink(io.TextIOBase):
    """stdout that hands over whole lines AS THEY ARE WRITTEN.

    The first cut captured into a StringIO and drained it once the pass
    returned, which meant a feature-length index delivered every log line and
    every progress marker in one burst at the end: the dashboard sat on
    "working…" for the entire run and then jumped to done. Passes print
    steadily, so the sink dispatches on each newline instead.

    Partial trailing text is held until a newline or `close_tail`, so a line
    is never split across two callbacks.
    """

    def __init__(self, emit):
        self._emit = emit
        self._buf = ""

    def write(self, s: str) -> int:  # noqa: D102 (TextIOBase)
        self._buf += s
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            self._emit(line)
        return len(s)

    def close_tail(self) -> None:
        """Flush a final line that was printed without a newline."""
        if self._buf:
            self._emit(self._buf)
            self._buf = ""


def _season_of(leaf: dict) -> int | None:
    """A leaf's season as an int. Backends report it as a number, but a
    missing or unparseable one must not compare equal to anything."""
    try:
        return int(leaf.get("season"))
    except (TypeError, ValueError):
        return None


def enumerate_targets(source: MediaSource, *, rating_key: str | None = None,
                      search: str | None = None, library: str | None = None,
                      series: str | None = None, season: int | None = None,
                      max_titles: int = 0) -> list[str]:
    from .passes import index_title
    if library:
        leaves = source.section_leaves(library)
    elif series:
        # The natural unit for TV: between one episode and a whole library.
        leaves = source.series_leaves(series)
        if season is not None:
            # A filter, not another request: series_leaves already carries the
            # season on every leaf. `is not None` and not truthiness because
            # season 0 is Specials — a real season a falsy check would drop.
            leaves = [lf for lf in leaves if _season_of(lf) == season]
            if not leaves:
                raise ValueError(
                    f"season {season} has no episodes in this show, so "
                    f"nothing would run")
    else:
        leaves = None
    if leaves is not None:
        if max_titles:
            leaves = leaves[:max_titles]
        return [leaf["ratingKey"] for leaf in leaves]
    if rating_key:
        return [rating_key]
    if search:
        return [index_title.resolve(source, rating_key=None,
                                    search=search)["ratingKey"]]
    raise ValueError("need rating_key, search, library, or series")


def run_title(store: Path, *, source: MediaSource, tmdb_key: str,
              audd_token: str, rating_key: str, skip: set[str],
              audd_budget: int = 300, hub_url: str = "",
              hub_miss: str = "index", refresh: set[str] = frozenset(),
              level: int = 1, log=print, progress=None) -> dict:
    """Index-if-missing + every enrichment pass for one title.

    `source` is the media backend (Plex/Jellyfin) via the MediaSource seam;
    `rating_key` is that backend's item id. When a hub is configured, a missing
    timeline is first looked up there; someone may already have computed it. On
    a hub miss, `hub_miss` decides: "index" (compute locally), "ask" (interactive
    prompt; CLI only), or "skip" (leave the title for another contributor).

    `level` picks the depth: 0 seeds a video-free timeline (cast/title, then the
    people+trivia passes: seconds, no streaming, no music); 1 is the full index
    (faces, music). A level-1 run over a level-0 seed upgrades it in place.

    `progress` (optional) receives sub-title events as dicts: `{"title": ...}`
    once the target resolves, then `{"phase", "done", "total"}` from whichever
    pass is running. Callers that don't pass one lose nothing: the marker lines
    are simply dropped instead of cluttering the log."""
    from .passes import index_title

    item = source.resolve(rating_key)
    cid = index_title.content_id_for(item)
    key = cid or rating_key
    title = item.get("grandparentTitle") or item["title"]
    if item["type"] == "episode":
        title += " S%02dE%02d" % (item["season"], item["episode"])
    log(f"=== {title}  ({key}) ===")
    result = {"key": key, "title": title, "steps": {}}
    if progress:
        # Emitted before any pass runs so a dashboard can name what it is
        # working on instead of echoing back the rating key the user gave it.
        progress({"title": title})

    def step(name, fn):
        if name in skip:
            result["steps"][name] = "skipped(flag)"
            return

        # Captured BEFORE the redirect below. `log` defaults to print, so
        # without this it writes to the sink, which calls this handler,
        # which logs... every CLI run died that way.
        outer = sys.stdout

        def handle(line):
            # Progress markers leave the log channel here: a feature-length
            # face pass emits ~50 of them, and the log is for humans. With no
            # callback they are dropped rather than printed, so the CLI keeps
            # the milestone lines it always had and nothing else.
            event = pg.parse(line)
            if event is not None:
                if progress:
                    # May raise Cancelled; that propagates out through print()
                    # and aborts the pass mid-frame, which is the only way to
                    # stop a title that is minutes into its face loop.
                    with contextlib.redirect_stdout(outer):
                        progress({"pass": name, **event})
                return
            with contextlib.redirect_stdout(outer):
                log(f"  [{name}] {line}")

        sink = _LineSink(handle)
        try:
            with contextlib.redirect_stdout(sink):
                fn()
            result["steps"][name] = "ok"
        except Cancelled:
            result["steps"][name] = "cancelled"
            raise           # abandon the title; do not run its later passes
        except index_title.Unsupported as e:
            # Not a failure: the input is outside what the pass can do and a
            # rerun would do the same thing. Recorded distinctly so a library
            # containing cartoons doesn't read as a broken batch.
            result["steps"][name] = f"skipped: {e}"
        except (SystemExit, Exception) as e:  # noqa: BLE001 (batch survives)
            result["steps"][name] = f"failed: {e}"
        finally:
            sink.close_tail()
            state = result["steps"].get(name, "")
            if state.startswith("failed"):
                log(f"  [{name}] FAILED: {state}")
            elif state.startswith("skipped: "):
                log(f"  [{name}] skipped: {state[len('skipped: '):]}")

    if not cid:
        # No content identity means nothing can be indexed or fetched; stop
        # here with the reason instead of failing inside a pass.
        log("[skip] no TMDb id on this item, so it has no content identity")
        result["steps"]["index"] = "failed: no content identity (no TMDb id)"
        return result
    tl_path = st.canonical_path(store, cid)
    exists = tl_path.exists()

    def _has_faces() -> bool:
        try:
            prov = json.loads(tl_path.read_text()).get("provenance") or {}
            return "faces" in prov
        except Exception:  # noqa: BLE001 (unreadable ⇒ treat as no faces)
            return False

    if level == 0:
        # Video-free seed. An existing timeline (seed or full) is already at
        # least this rich, so never re-seed; the hub is skipped too (seeding
        # is local + cheap, nothing to save by fetching).
        if exists:
            log("[seed] timeline exists; skip")
            result["steps"]["index"] = "exists"
        else:
            step("index", lambda: index_title.run_level0(
                store, source=source, tmdb_key=tmdb_key, rating_key=rating_key))
    elif exists and "index" in refresh:
        log("[index] refresh forced; re-indexing (enrichment blocks preserved)")
        step("index", lambda: index_title.run(
            store, store / "index_work", source=source,
            tmdb_key=tmdb_key, rating_key=rating_key))
    elif exists and not _has_faces():
        log("[index] upgrading level-0 seed → full index (faces)")
        step("index", lambda: index_title.run(
            store, store / "index_work", source=source,
            tmdb_key=tmdb_key, rating_key=rating_key))
    elif exists:
        log("[index] timeline exists; skip")
        result["steps"]["index"] = "exists"
    else:
        fetched = None
        if hub_url and cid:
            from .share import fetch_from_hub
            try:
                fetched = fetch_from_hub(store, hub_url, cid)
            except Exception as e:  # noqa: BLE001 (hub is best-effort)
                log(f"[hub] fetch failed: {e}")
            if fetched is not None:
                st.map_lookup(store, f"{source.key_prefix}:{rating_key}", cid)
                log(f"[hub] fetched from hub "
                    f"(+ manifest {source.key_prefix}:{rating_key}); index skipped")
                result["steps"]["index"] = "hub"
        if fetched is None:
            if hub_url and cid and hub_miss == "ask":
                answer = input(f"{cid} isn't on the hub. Index locally? "
                               f"(streams the media) [y/N] ").strip().lower()
                if answer != "y":
                    log("[index] declined; title skipped")
                    result["steps"]["index"] = "declined"
                    return result
            elif hub_url and cid and hub_miss == "skip":
                log("[index] hub miss; title skipped (hub_miss=skip)")
                result["steps"]["index"] = "hub-miss-skipped"
                return result
            step("index", lambda: index_title.run(
                store, store / "index_work", source=source,
                tmdb_key=tmdb_key, rating_key=rating_key))

    # Speakers: diarization for animated titles, where faces cannot work.
    #
    # OPT-IN ONLY. It is not in the default skip set by accident -- it costs an
    # audio pull plus ~25 minutes of CPU per feature, and on live action it
    # would duplicate work faces already did better. The dashboard offers it
    # as a chip on animated rows; nothing runs it unasked.
    #
    # It also does not finish: it stores clusters and stops, because naming a
    # speaker needs a person. `needsLabelling` in the result is what the
    # dashboard turns into "16 speakers, none named".
    if "speakers" not in skip:
        from .passes import speakers as speakers_pass
        step("speakers", lambda: speakers_pass.run(
            store, store / "speakers_work", source=source,
            tmdb_key=tmdb_key, rating_key=rating_key))

    from .passes import people as people_pass
    from .passes import trivia as trivia_pass
    step("people", lambda: people_pass.run(
        store, tmdb_key, [key],
        refresh_days=0 if "people" in refresh else 180))
    step("trivia", lambda: trivia_pass.run(
        store, tmdb_key, [key],
        refresh_days=0 if "trivia" in refresh else 90))

    if level == 0:
        result["steps"]["music"] = "skipped(level0)"  # no video/audio at level 0
    elif "music" in skip or not audd_token:
        result["steps"]["music"] = ("skipped(flag)" if "music" in skip
                                    else "skipped(no token)")
    elif "music" not in refresh and tl_path.exists() \
            and "music" in (json.loads(tl_path.read_text())
                            .get("provenance") or {}):
        log("[music] block stamped; skip")
        result["steps"]["music"] = "exists"
    else:
        from .passes import music as music_pass
        step("music", lambda: music_pass.run(
            store, key, audd_token, source=source,
            monthly_budget=audd_budget))
    return result
