# OpenXray: scene-level metadata for your media library

X-Ray-style information for media you own: who's on screen, what's playing,
and what's worth knowing, moment by moment. Not affiliated with, endorsed
by, or connected to Amazon.

Generates and enriches **timeline files**: per-title JSON describing what's on
screen at every moment (cast, playing music, trivia), for rendering as an
Amazon-X-Ray-style overlay in [Plezy](https://github.com/edde746/plezy) (Plex + Jellyfin)
or a browser extension.

**The timeline files are the system** (see [SCHEMA.md](SCHEMA.md)): named by
content identity (`tmdb-movie-769.json`), validated against
[`schema/timeline.schema.json`](schema/timeline.schema.json), carrying
per-block provenance. Generators and clients are fully decoupled through them.

**What it costs:** everything is free except naming songs (~$0.03–0.20/title
via AudD, one-time, budget-capped, amortized to zero by the hub's shared
timelines).

This repo is the **Generator**: it makes timelines from media you own.
Distribution is the companion **OpenXray Hub** (separate `openxray-hub`
repo): a deliberately boring catalog/review service the Generator only ever
talks to over HTTP. They deploy apart on purpose — the Generator holds your
media-server token and belongs on your LAN; the Hub is public-facing and
holds no credentials at all. Most people only ever run the Generator; you
can just point Plezy at a public hub.

## Quick start: the Generator (Docker)

```bash
cd generator && docker compose up -d
docker compose logs orchestrator | grep "web UI token"
open http://localhost:8080        # paste the token once
```

Then everything happens in the browser:

1. **Sign in with Plex**: a plex.tv tab opens, you sign in there (your
   password never touches this app), pick your server from the discovered
   list. Or connect **Jellyfin** via Quick Connect (enter a code in your
   Jellyfin UI) or username/password.
2. Enter a **TMDb key** (free at themoviedb.org) and optionally an **AudD
   token** (music naming, paid) in Settings.
3. **Run**: search a title or queue a whole library. Check *level-0 seed*
   for the video-free fast tier (cast + bios + trivia, seconds per title);
   uncheck it for the full face-interval index.

Timelines land in the `timelines` volume; point Plezy's *X-Ray → Timelines
folder/URL* at it (or serve it with a hub). Export, hub-upload, import,
and validation are all buttons on the dashboard.

The app registers as a named device on your Plex account and is individually
revocable; tokens live in `settings.json` inside the volume, mode 0600,
redacted in every API response. **Don't expose port 8080 to the internet raw**:
reverse-proxy with TLS, or set `AUTH_METHOD=external` behind an
authenticating proxy.

Env vars in `generator/.env` still work; they seed `settings.json` on first
boot, after which the web UI owns the values.

## Development / CLI

A developer CLI (`python -m xray.cli`) drives the same pipeline library the
orchestrator uses. It is a dev tool, not the product surface.

## Layout

```
xray/               the package: store (contract), passes/, engines seam,
                    sources/ (base seam, plex, jellyfin, wiki), faces/, music/,
                    service/ (orchestrator + engines + auth), settings_store, cli
schema/             timeline.schema.json, the machine-checkable contract
generator/          OpenXray Generator: compose stack (orchestrator + engines)
engines/audio/      Dockerfile for the music segmenter (inaSpeechSegmenter)
scripts/            fetch_models.py
SCHEMA.md           the human contract: shapes, rules, size, future levers
                    (MIRROR: canonical copy lives in the openxray-hub repo;
                    both copies must stay byte-identical, change them together)
tests/              stdlib unittest, no network: PYTHONPATH=. python tests/<f>.py
```

## License

[Apache License 2.0](LICENSE). The license covers this software; it grants no
rights in the media you index, nor in any third-party data the passes fetch,
and per Apache 2.0 section 6 it grants no trademark rights.

Note what the license cannot do for you: running this tool requires **your
own TMDb API key**, and TMDb's API Terms of Use permit **non-commercial use
only**. That obligation follows the key, not the code, so it binds you as the
operator regardless of what this license permits.

## Attribution

This product uses the TMDB API but is not endorsed or certified by TMDB.

Full attribution for every upstream source (TMDb, Wikidata, Wikipedia, AudD)
is in [NOTICE](NOTICE), which Apache 2.0 carries into derivative works.
