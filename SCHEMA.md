# X-Ray Timeline: Data Contract

The timeline JSON files **are the system**: the Generator's passes write
them, clients (Plezy widget, browser extension) read them, and every feature is
an additive enrichment of the same files. No servers or databases sit in the
data path. Machine-checkable version: [`schema/timeline.schema.json`](schema/timeline.schema.json).
Every pass validates before writing (`store.write_timeline`).

## The store

```
~/.plex-xray/timelines/            # canonical store (Plezy reads this)
  tmdb-movie-769.json              # one file per title, named by CONTENT id
  tmdb-tv-62852-s01e01.json
  index.json                       # lookup: "plex:288" → "tmdb-movie-769.json"
  people_cache.json                # cross-title TMDb cache (not read by clients)
  timeline_288.json → …            # legacy compat symlinks (old readers)
```

- **Content identity, never server ids.** Plex ratingKeys are unstable (change
  on re-add, differ per server/backend); TMDb ids are forever. Clients resolve
  a playing item via `index.json` (`"<backend>:<itemId>"` → filename), with the
  legacy `timeline_<id>.json` name as fallback.
- Helpers for all of this live in `xray/store.py`.

## Annotated document

```jsonc
{
  // identity & versioning
  "contentId": "tmdb-movie-769",       // REQUIRED: canonical identity == filename stem
  "version": 1,                        // schema version; additive changes only
  "generated": "2026-07-18T03:11:58Z", // when the core (frames/faces) was built

  "provenance": {                      // per-block: who wrote it, when
    "faces":  { "generated": "…", "version": "sface-v1" },
    "people": { "generated": "…", "version": "tmdb-v1" },   // enrich_people.py
    "music":  { "generated": "…", "version": "audd-v1" },   // enrich_music.py
    "trivia": { "generated": "…", "version": "wiki-v1" }    // enrich_trivia.py
  },

  // cast, owned by: generator core; `person` sub-block by: enrich_people.py
  "cast": [{
    "actorId": "tmdb:380",             // stable person id, joins to intervals
    "name": "Robert De Niro",
    "character": "James Conway",
    "thumb": "https://image.tmdb.org/t/p/w185/…",
    "person": {                        // OPTIONAL, absent until enriched
      "bio": "…truncated ~1,200 chars…",
      "birthday": "1943-08-17", "deathday": null,
      "placeOfBirth": "Greenwich Village, New York City…",
      "knownFor": [{                   // ≤10; billing-order × popularity blend
        "title": "Joker", "year": "2019", "character": "Murray Franklin",
        "mediaType": "movie", "posterUrl": "https://…"
      }]
    }
  }],

  // intervals: ALL times are ms from title start, half-open [startMs, endMs)
  "actorIntervals": [                  // owned by: generator core (faces)
    { "actorId": "tmdb:380", "startMs": 4320000, "endMs": 4462000, "confidence": 0.91 }
  ],
  "musicIntervals": [                  // owned by: enrich_music.py
    { "title": "Layla", "artist": "Derek & The Dominos",
      "startMs": 512000, "endMs": 641000, "confidence": null,
      "source": "audd" }               // "audd" (discovered) | "library" (owned)
  ],

  // trivia, owned by: enrich_trivia.py (Wikidata + Wikipedia, derived from
  // the contentId). startMs/endMs null = title-level fact, shown position-
  // independently; set (future scene-pinning pass) = surfaced while playback
  // is inside the window. Untimed and timed facts coexist in one list.
  "trivia": [{ "text": "…", "source": "wikipedia", "startMs": null, "endMs": null }]
}
```

## Contract rules

1. **Empty arrays are valid.** No intervals → clients show the full-cast panel.
   This is a *level-0 seed* (`xray run --level 0`): cast + bios + trivia, no
   video work. No `provenance.faces` stamp; its absence marks the timeline as
   seed-only, which a later `--level 1` run upgrades in place. `sourceRuntimeMs`
   is only meaningful once intervals exist (the hub requires it only then).
2. **Additive evolution only.** Consumers ignore unknown fields; `version`
   stays `1`. Never rename/remove/retype a shipped field.
3. **One pass, one block.** Each enrichment script touches only its own block
   plus its `provenance` stamp, and writes via `store.write_timeline`
   (validate → atomic write).
4. **Times are ms, intervals half-open** `[startMs, endMs)`, matching Plezy's
   marker convention; converts losslessly to Jellyfin ticks (× 10,000).
5. **TMDb person data is licensed, not owned**: refresh within 6 months
   (`people_cache.json` timestamps + `--refresh-days`), attribute in UI
   (Plezy detail footer does), and never redistribute enriched timelines.

## Size reality (2026-07-19)

~160 KB per fully-enriched title (≈60% of that is person bios/knownFor).
A 1,000-title library ≈ 160 MB of JSON. Fine.

## Future levers (documented so we don't relitigate)

- **Normalize people** into a shared store if bio duplication bites at library
  scale (trigger: person-update feature, or store size actually matters).
- **SQLite** only ever for generator-internal job state at library scale,
  never for this contract.
- **Migrations**: none needed pre-release. Post-release, ship as versioned
  idempotent passes that detect old shapes via `index.json version` +
  per-block `provenance` versions.
- **Jellyfin Media Segments export**: a possible future pass
  (musicIntervals → typed segments, ms → ticks); lossy by design (their enum
  has no music/actor types).
