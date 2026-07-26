"""The `xray` CLI: one entry point over the store and every pass.

  python -m xray.cli status
  python -m xray.cli validate
  python -m xray.cli index --origin https://plex... --search "The Bear"
  python -m xray.cli --backend jellyfin index --origin https://jf... --rating-key 1234
  python -m xray.cli enrich people [keys...]
  python -m xray.cli enrich trivia [keys...]
  python -m xray.cli enrich music tmdb-tv-62852-s01e01 --media file.mkv
  python -m xray.cli enrich all [keys...]        # people + trivia (music needs a source)
  python -m xray.cli map-jellyfin --server https://jf... --api-key XXXX

The media backend is chosen with `--backend plex|jellyfin` (default plex);
both are native peers via the MediaSource seam (sources/base.py). `map-jellyfin`
is now only for hub-only consumers who pull finished timelines and never index.

Local transport today (in-process faces, docker-run segmenter); the compose
stack's orchestrator will drive the same passes over HTTP engines (plan U2b).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from . import keys as k
from . import store as st

DEFAULT_DIR = Path.home() / ".plex-xray" / "timelines"


def _store(args) -> Path:
    d = Path(args.dir).expanduser().resolve()
    if not d.is_dir():
        raise SystemExit(f"no timeline store at {d}")
    return d


def _origin(args) -> str:
    backend = getattr(args, "backend", "plex")
    env = "JELLYFIN_ORIGIN" if backend == "jellyfin" else "PLEX_ORIGIN"
    origin = args.origin or os.environ.get(env, "")
    if not origin:
        raise SystemExit(f"need --origin (or {env} env)")
    return origin


def _make_source(args):
    """Construct the selected media backend (--backend plex|jellyfin)."""
    from .sources.base import open_source
    backend = getattr(args, "backend", "plex")
    token = k.backend_token(backend)
    if not token:
        want = ".jellyfintoken" if backend == "jellyfin" else ".plextoken"
        raise SystemExit(f"need a {backend} token ({want} or env)")
    return open_source(backend, _origin(args), token,
                       user_id=k.jellyfin_user() or None)


def cmd_status(args) -> int:
    store = _store(args)
    files = st.timeline_files(store)
    manifest = st.load_manifest(store)
    rev = {}
    for lk, fn in manifest.get("lookup", {}).items():
        rev.setdefault(fn, []).append(lk)
    blocks = ["faces", "people", "music", "trivia"]
    print(f"store: {store}  ({len(files)} timeline(s))\n")
    print(f"{'timeline':<28} {' '.join(f'{b:<13}' for b in blocks)} lookup keys")
    for f in files:
        doc = json.loads(f.read_text())
        prov = doc.get("provenance") or {}
        marks = " ".join(
            (f"✓ {(prov[b].get('generated') or '')[:10]}" if b in prov else "-").ljust(13)
            for b in blocks
        )
        print(f"{f.stem:<28} {marks} {', '.join(rev.get(f.name, []))}")
    return 0


def cmd_validate(args) -> int:
    store = _store(args)
    bad = 0
    for f in st.timeline_files(store):
        try:
            st.validate(json.loads(f.read_text()))
            print(f"{f.name}: VALID")
        except Exception as e:  # noqa: BLE001
            bad += 1
            print(f"{f.name}: INVALID: {str(e).splitlines()[0]}", file=sys.stderr)
    return 1 if bad else 0


def cmd_index(args) -> int:
    from .passes import index_title
    store = _store(args)
    source = _make_source(args)
    tmdb = k.tmdb_key()
    if not tmdb:
        raise SystemExit("need .tmdbkey at the repo root")
    opts = index_title.IndexOptions(
        fps=args.fps, threshold=args.threshold,
        min_cluster_size=args.min_cluster_size, min_run=args.min_run,
        start_s=args.start_s, duration_s=args.duration_s,
        max_frames=args.max_frames,
    )
    index_title.run(store, store / "index_work", source=source,
                    tmdb_key=tmdb, rating_key=args.rating_key,
                    search=args.search, opts=opts, dry_run=args.dry_run)
    return 0


def cmd_enrich(args) -> int:
    store = _store(args)
    tmdb = k.tmdb_key()
    if args.what in ("people", "all"):
        if not tmdb:
            raise SystemExit("people pass needs .tmdbkey")
        from .passes import people
        people.run(store, tmdb, args.keys or None,
                   refresh_days=args.refresh_days, dry_run=args.dry_run)
    if args.what in ("trivia", "all"):
        if not tmdb:
            raise SystemExit("trivia pass needs .tmdbkey")
        from .passes import trivia
        trivia.run(store, tmdb, args.keys or None,
                   refresh_days=args.refresh_days)
    if args.what == "music":
        if len(args.keys) != 1:
            raise SystemExit("enrich music takes exactly one key")
        from .passes import music
        backend = getattr(args, "backend", "plex")
        env = "JELLYFIN_ORIGIN" if backend == "jellyfin" else "PLEX_ORIGIN"
        # A streaming backend is only needed when there's no --media and no
        # harvested audio; build one lazily when an origin is available.
        src = _make_source(args) if (args.origin or os.environ.get(env)) else None
        music.run(store, args.keys[0], k.audd_token(),
                  media=args.media, source=src,
                  min_music=args.min_music, merge_gap=args.merge_gap,
                  max_cues=args.max_cues, dry_run=args.dry_run)
    if args.what == "all":
        print("(music skipped in 'all': it needs a media source; run "
              "`enrich music <key> --media …` or with Plex flags)")
    return 0


def resolve_series(source, value: str) -> str:
    """A show's id, from its name or from the id itself.

    The dashboard takes the id off a search result it already rendered. The CLI
    has no search subcommand, so requiring a raw id would make --series
    unusable without reading the backend's API by hand. Search returns
    episodes, each carrying the id of its show, so a name is enough."""
    shows: dict[str, str] = {}
    for r in source.search(value):
        sid = r.get("seriesId")
        if sid:
            shows[str(sid)] = r.get("grandparentTitle") or "?"
    if value in shows:          # already an id
        return value
    if not shows:
        raise SystemExit(
            f"no show matching {value!r}: search matches episodes and the show "
            f"is taken from theirs, so a query that finds only movies finds no "
            f"show here")
    if len(shows) > 1:
        listing = "\n".join(f"  {sid}  {name}" for sid, name
                            in sorted(shows.items(), key=lambda kv: kv[1]))
        raise SystemExit(f"{value!r} matches more than one show:\n{listing}\n"
                         f"pass one of these ids as --series instead")
    sid, name = next(iter(shows.items()))
    print(f"[run] --series {value!r} → {name} ({sid})")
    return sid


def cmd_run(args) -> int:
    """The pipeline (xray/pipeline.py) driven from the CLI."""
    from . import pipeline

    store = _store(args)
    source = _make_source(args)
    tmdb = k.tmdb_key()
    if not tmdb:
        raise SystemExit("need .tmdbkey at the repo root")

    # Season 0 is Specials, so this is set-vs-None; `not args.season` would
    # reject asking for the Specials as if no season had been given.
    if args.season is not None and not args.series:
        raise SystemExit("--season narrows --series; name a show too")
    series = resolve_series(source, args.series) if args.series else None

    try:
        targets = pipeline.enumerate_targets(
            source, rating_key=args.rating_key, search=args.search,
            library=args.library, series=series, season=args.season,
            max_titles=args.max_titles)
    except ValueError as e:
        # enumerate_targets raises ValueError because it is a library function
        # (the service turns it into a job-log line). On a terminal that is a
        # traceback for what is really a typo, so say it plainly instead.
        raise SystemExit(str(e))
    if args.library:
        print(f"[run] {len(targets)} title(s) from library {args.library!r}")
    elif series:
        where = ("every season" if args.season is None
                 else f"season {args.season}")
        print(f"[run] {len(targets)} episode(s), {where}")
    skip = set((args.skip or "").split(",")) - {""}

    hub = args.hub if args.hub is not None else k.hub_url()
    hub_miss = args.hub_miss or ("ask" if sys.stdin.isatty() else "index")
    refresh = set((args.refresh or "").split(",")) - {""}
    if "all" in refresh:
        refresh = {"index", "people", "trivia", "music"}
    summary = []
    for rk in targets:
        summary.append(pipeline.run_title(
            store, source=source, tmdb_key=tmdb,
            audd_token=k.audd_token(), rating_key=rk, skip=skip,
            audd_budget=args.audd_budget, hub_url=hub, hub_miss=hub_miss,
            refresh=refresh, level=args.level))
    print("\n=== run summary ===")
    for r in summary:
        steps = "  ".join(f"{n}:{v.split(':')[0]}" for n, v in r["steps"].items())
        print(f"{r['key']:<28} {steps}")
    return 0


def cmd_export(args) -> int:
    from .share import export_timeline
    export_timeline(_store(args), args.key, Path(args.out).expanduser())
    return 0


def cmd_import(args) -> int:
    from .share import import_timeline
    import_timeline(_store(args), args.src, force=args.force)
    return 0


def cmd_map_jellyfin(args) -> int:
    from .passes import jellyfin_map
    jellyfin_map.run(_store(args), server=args.server, api_key=args.api_key,
                     user_id=args.user_id, dry_run=args.dry_run)
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Split out from main() so the flags can be exercised without running a
    command — otherwise nothing about this surface is testable."""
    p = argparse.ArgumentParser(prog="xray")
    p.add_argument("--dir", default=str(DEFAULT_DIR), help="timeline store")
    p.add_argument("--backend", choices=["plex", "jellyfin"], default="plex",
                   help="media backend to index against (default: plex)")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status", help="per-title provenance table").set_defaults(func=cmd_status)
    sub.add_parser("validate", help="validate every timeline against the contract").set_defaults(func=cmd_validate)

    pi = sub.add_parser("index", help="birth a timeline from a Plex title (frames+faces)")
    pi.add_argument("--origin", help="Plex origin URL (or PLEX_ORIGIN env)")
    pi.add_argument("--rating-key")
    pi.add_argument("--search")
    pi.add_argument("--fps", type=float, default=0.5)
    pi.add_argument("--threshold", type=float, default=0.363)
    pi.add_argument("--min-cluster-size", type=int, default=5)
    pi.add_argument("--min-run", type=int, default=2)
    pi.add_argument("--start-s", type=float, default=0.0)
    pi.add_argument("--duration-s", type=float, default=None)
    pi.add_argument("--max-frames", type=int, default=0)
    pi.add_argument("--dry-run", action="store_true",
                    help="resolve + cast refs only; no frames, nothing written")
    pi.set_defaults(func=cmd_index)

    pe = sub.add_parser("enrich", help="run an enrichment pass")
    pe.add_argument("what", choices=["people", "trivia", "music", "all"])
    pe.add_argument("keys", nargs="*")
    pe.add_argument("--refresh-days", type=float, default=180)
    pe.add_argument("--media", help="music: local media file")
    pe.add_argument("--origin", help="music: Plex origin to stream from")
    pe.add_argument("--min-music", type=float, default=10.0)
    pe.add_argument("--merge-gap", type=float, default=15.0)
    pe.add_argument("--max-cues", type=int, default=80)
    pe.add_argument("--dry-run", action="store_true")
    pe.set_defaults(func=cmd_enrich)

    pr = sub.add_parser("run", help="the pipeline: index-if-missing + all passes")
    pr.add_argument("--origin", help="Plex origin URL (or PLEX_ORIGIN env)")
    pr.add_argument("--rating-key")
    pr.add_argument("--search")
    pr.add_argument("--library", help="process a whole library section by name")
    pr.add_argument("--series", help="every episode of one show, by name "
                                     "(or its id); narrow with --season")
    pr.add_argument("--season", type=int, default=None,
                    help="with --series, one season only (0 = Specials)")
    pr.add_argument("--max-titles", type=int, default=0,
                    help="cap titles per run (nightly-batch slicing)")
    pr.add_argument("--skip", help="comma list of passes to skip, e.g. music")
    pr.add_argument("--audd-budget", type=int, default=300,
                    help="monthly AudD call ceiling (0 = unlimited)")
    pr.add_argument("--hub", help="community hub URL to check before indexing "
                    "(default: .huburl / XRAY_HUB_URL; '' disables)")
    pr.add_argument("--hub-miss", choices=["ask", "index", "skip"],
                    help="on hub miss: prompt (default when interactive), "
                    "index locally, or skip the title")
    pr.add_argument("--refresh", help="force blocks to recompute: comma list "
                    "of index,people,trivia,music or 'all' (re-index "
                    "preserves the other blocks)")
    pr.add_argument("--level", type=int, choices=[0, 1], default=1,
                    help="0 = video-free seed (cast/title/bios/trivia, minutes "
                    "per library, no streaming, no music); 1 = full index "
                    "(faces + music). A level-1 run upgrades a level-0 seed.")
    pr.set_defaults(func=cmd_run)

    px = sub.add_parser("export", help="share-safe export (strips TMDb person data)")
    px.add_argument("key", help="content id to export")
    px.add_argument("--out", default="exports", help="output directory")
    px.set_defaults(func=cmd_export)

    pm = sub.add_parser("import", help="import a shared timeline (file or URL)")
    pm.add_argument("src")
    pm.add_argument("--force", action="store_true", help="replace an existing timeline")
    pm.set_defaults(func=cmd_import)

    pj = sub.add_parser("map-jellyfin", help="map hub-fetched timelines to "
                        "jellyfin:<itemId> (hub-only consumers; indexing with "
                        "--backend jellyfin stamps these itself)")
    pj.add_argument("--server", required=True)
    pj.add_argument("--api-key", required=True)
    pj.add_argument("--user-id")
    pj.add_argument("--dry-run", action="store_true")
    pj.set_defaults(func=cmd_map_jellyfin)
    return p


def main() -> int:
    args = build_parser().parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
