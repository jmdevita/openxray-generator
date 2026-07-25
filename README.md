# OpenXray

Scene-level metadata for media you own: who's on screen, what's playing, and
what's worth knowing, moment by moment. Rendered as an X-Ray-style overlay in
[Plezy](https://github.com/edde746/plezy) (Plex + Jellyfin).

*Not affiliated with, endorsed by, or connected to Amazon.*

![The generator planning a full index: a coverage bar splitting a library into
already-indexed, seeded, available on the hub, and not yet indexed; below it
two tiers, Quick seed and Full index, each priced over only the work that is
left.](assets/dashboard.png)

<sub>Planning a run before starting it. Figures from a demo library, not a
real one.</sub>

## Just want to use it?

Point Plezy at the public hub. No install, no API key, nothing to run:

> **Plezy → Settings → Playback → X-Ray → Timelines URL**
> `https://hub.openxray.net`

Whatever the community has already indexed shows up. Read on only if you want
to generate timelines yourself, for titles nobody has covered.

## Generate your own

```bash
cd generator && docker compose up -d
docker compose logs orchestrator | grep "web UI token"
open http://localhost:8080          # paste the token once
```

Everything after that is in the browser:

1. **Connect a media server.** Sign-in happens on plex.tv, so this app gets a
   revocable token rather than your password. Jellyfin connects via Quick
   Connect or username/password.
2. **Add a TMDb key** (free at themoviedb.org). Titles are identified by TMDb
   id, so nothing runs without one. Until both of these exist the dashboard
   shows only this checklist.
3. **Run.** Search a title, take a whole show, or take a whole library. Pick a
   library and it reports what you already have and prices the work that's
   left, before you commit to it.

Timelines land in the `timelines` volume. Point Plezy's *Timelines folder/URL*
at it.

To contribute back, use **Export bundle**: one `.xray.jsonl` covering your whole
store, one timeline per line, licensed person data stripped. Upload it on the
hub's `/contribute` page. A bundle is a single upload however many titles it
carries, which is what makes sharing a whole library practical.

## Two tiers

|  | Quick seed | Full index |
|---|---|---|
| Reads the video file | no | yes |
| Cast, biographies, trivia | ✓ | ✓ |
| Per-actor on-screen intervals | | ✓ |
| Song names | | ✓ (needs an AudD token) |
| Time per title | seconds | minutes |
| Cost | free | ~$0.005 per music cue |

Seeding first is not wasted work: a later full index **upgrades a seed in
place**, keeping everything already computed. So the sane order is to seed a
whole library cheaply, then deepen the titles you care about.

Song identification is the only thing that ever costs money, it is per *cue*
rather than per title, and it can be switched off on a run that otherwise
wants the full index.

## What a timeline looks like

The files **are the system**. One JSON document per title, named by content
identity, validated against
[`schema/timeline.schema.json`](schema/timeline.schema.json):

```jsonc
// tmdb-movie-769.json
{
  "contentId": "tmdb-movie-769",
  "title": "Goodfellas",
  "year": 1990,
  "cast": [
    { "actorId": "tmdb:380", "name": "Robert De Niro",
      "character": "James Conway" }
  ],
  "actorIntervals": [
    { "actorId": "tmdb:380", "startMs": 4320000, "endMs": 4462000,
      "confidence": 0.91 }
  ],
  "musicIntervals": [
    { "title": "Layla", "artist": "Derek & The Dominos",
      "startMs": 512000, "endMs": 641000, "source": "audd" }
  ],
  "trivia": [{ "text": "…", "source": "wikipedia" }],
  "provenance": { "faces": { "generated": "…", "version": "sface-v1" } }
}
```

Generators and clients are decoupled entirely through that shape. Every block
records which pass produced it, so enrichment is incremental and a re-index
never destroys work another pass paid for. [SCHEMA.md](SCHEMA.md) is the human
version of the contract; the rules there are worth reading before changing
anything.

## How it works

A title becomes a timeline through independent, provenance-gated passes:

- **index** — decode frames, embed faces, cluster them, label the clusters
  against TMDb reference headshots → `actorIntervals`
- **people** — biographies and known-for, cached across titles
- **trivia** — Wikidata (CC0) and paraphrased Wikipedia, each fact citing its
  origin
- **music** — a local segmenter finds where music plays (free), then one AudD
  probe identifies each cue (paid)

Face and audio work run behind an engine seam, in-process by default or as
separate containers in the compose stack.

Distribution is a companion **hub**: a deliberately boring catalog and review
service that the Generator only ever talks to over HTTP. They are separate on
purpose. The Generator holds your media-server token and belongs on your LAN;
the hub is public-facing and holds no media credentials at all. A hub gates
uploads behind review rather than publishing on arrival, and serves downloads
from a CDN so the origin only ever handles writes.

The hub at `hub.openxray.net` is the one this project operates; its source is
not currently published.

## Development

```bash
pip install -r requirements.txt
PYTHONPATH=. python tests/test_plan.py      # each file runs standalone
```

Tests are stdlib `unittest`, no pytest dependency, and touch no network. A
developer CLI (`python -m xray.cli`) drives the same pipeline library the
orchestrator uses; it is a dev tool, not the product surface.

```
xray/            the package
  store.py       the contract: naming, validation, atomic writes, manifest
  passes/        index, people, trivia, music
  sources/       MediaSource seam: base, plex, jellyfin, wiki
  faces/         embedding + clustering
  service/       orchestrator (dashboard + jobs), engine services, auth
  plan.py        coverage and cost for a run, before you start it
schema/          timeline.schema.json, the machine-checkable contract
generator/       compose stack: orchestrator + engines
tests/           stdlib unittest
```

`SCHEMA.md` and `schema/` are **mirrored** from the hub repo, which holds the
canonical copy. Both must stay byte-identical; `diff` between them is the
drift check.

## Security

Reporting and known trade-offs are in [SECURITY.md](SECURITY.md). The short
version: **don't put port 8080 anywhere untrusted.** Docker publishes ports
with NAT rules that bypass a host firewall, so `ufw` will not save you. Use an
authenticating reverse proxy or `AUTH_METHOD=external`.

## License

[Apache License 2.0](LICENSE). It covers this software. It grants no rights in
the media you index, none in the third-party data the passes fetch, and per
section 6 no trademark rights.

Note what a license cannot do for you: running this requires **your own TMDb
API key**, and TMDb's API Terms of Use permit **non-commercial use only**.
That obligation follows the key, not the code, so it binds you as the operator
regardless of what this license allows.

## Attribution

This product uses the TMDB API but is not endorsed or certified by TMDB.

Full attribution for every upstream source (TMDb, Wikidata, Wikipedia, AudD)
is in [NOTICE](NOTICE), which Apache 2.0 carries into derivative works.
