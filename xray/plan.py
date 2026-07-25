"""What a run would actually do, before anyone commits to it.

`xray run --library X` used to be a leap of faith: you learned the size of
the job by watching it happen. This module answers the two questions worth
asking first: **what is already covered**, and **what does the rest cost**.

Coverage has three sources, and they are not interchangeable:

  * the local store   already computed here, nothing to do
  * the hub           somebody else computed it; a fetch, not a render
  * nothing           real work, and the only thing that costs money

Both levels are costed from a SINGLE gather (one library listing, one hub
request), because the caller is drawing two options side by side and asking
twice would double the network for an answer that cannot differ.

The arithmetic MIRRORS pipeline.run_title rather than idealizing it: an
estimate that disagrees with the pipeline is worse than no estimate. Two
consequences are deliberate and easy to misread as bugs:

  * a level-0 run never consults the hub (seeding is local and cheap), so
    hub coverage is reported but not subtracted;
  * at level 1 the hub is only consulted for titles with NO local timeline.
    A local seed is upgraded locally even when the hub holds a full index of
    it. That is what run_title does today; `hubCouldServe` counts those so
    the gap is visible rather than silently mis-estimated.

Estimates are ranges on purpose. Per-title cost swings with runtime, codec,
hardware and how much music a film actually has, and a single confident
number would be a lie with a decimal point on it.
"""
from __future__ import annotations

import json
from pathlib import Path

from . import store as st
from .sources.base import MediaSource

# Rough per-title figures. Ranges, not point estimates: see the module note.
# Someday these could come from job history; until there IS job history,
# honest constants beat a fabricated average.
SEED_SECONDS = (3, 6)        # metadata only, network-bound
FULL_SECONDS = (240, 600)    # stream + decode + embed + cluster

# Money is billed PER CUE, not per title: the local detector finds stretches
# of music and each one costs a single AudD probe (docs/COSTS.md). A movie
# carries far more of them than a TV episode, so costing both at one flat
# per-title rate is off by roughly 3x in whichever direction you guessed.
AUDD_PER_CALL = 0.005                        # $5 / 1k requests
CUES_PER_TITLE = {"movie": (20, 40), "show": (5, 15)}
MAX_CUES = 80                                # music.run()'s own per-title cap


def _blocks(path: Path) -> set[str]:
    """Which provenance blocks a stored timeline carries (empty if unreadable)."""
    try:
        return set((json.loads(path.read_text()).get("provenance") or {}))
    except (OSError, json.JSONDecodeError):
        return set()


def library_plan(store: Path, source: MediaSource, library: str, *,
                 hub_url: str = "", audd: bool = False, kind: str = "movie",
                 audd_headroom: int | None = None) -> dict:
    """Coverage for `library`, and what each level would cost from here.

    `audd` is whether music identification is available (a token exists); it
    is the only thing that makes a run cost money. `kind` is the section type
    ("movie" or "show"), which sets the cue count and therefore the price.
    `audd_headroom` is calls left in this month's budget, or None for
    unlimited: the estimate reports the FULL cost of the work but flags when
    the budget will halt the run partway, which is the more useful warning."""
    ids = source.content_ids(library)
    total = len(ids)
    unidentified = sum(1 for cid in ids.values() if not cid)

    # Work is per CONTENT id, not per file: two copies of the same film are
    # one timeline. Counting rows here would overstate every estimate.
    wanted = {cid for cid in ids.values() if cid}

    have_full, have_seed = set(), set()
    for cid in wanted:
        path = st.canonical_path(store, cid)
        if not path.exists():
            continue
        (have_full if "faces" in _blocks(path) else have_seed).add(cid)

    missing = wanted - have_full - have_seed

    # None = could not ask. An empty hub and an unreachable one produce the
    # same estimate but mean different things, and the UI says which.
    fetched = hub_catalog_for(hub_url) if hub_url else None
    catalog = fetched or {}

    # Level 0: any existing timeline is already at least a seed, so it is
    # skipped; the hub is never consulted.
    seed_todo = missing

    # Level 1: only titles with nothing local reach the hub lookup, and local
    # seeds are upgraded locally regardless of what the hub holds.
    from_hub = {cid for cid in missing if cid in catalog}
    full_todo = (missing - from_hub) | have_seed
    hub_could_serve = {cid for cid in have_seed
                       if "faces" in (catalog.get(cid, {}).get("units") or [])}

    return {
        "library": library,
        "total": total,
        "unidentified": unidentified,
        "distinct": len(wanted),
        "haveFull": len(have_full),
        "haveSeed": len(have_seed),
        "hubChecked": fetched is not None,
        "hubCatalog": len(catalog),
        "kind": kind,
        "auddAvailable": audd,
        "auddHeadroom": audd_headroom,
        "levels": {
            "0": _level(len(seed_todo), SEED_SECONDS, audd=False, kind=kind,
                        headroom=audd_headroom, from_hub=0, could_serve=0),
            "1": _level(len(full_todo), FULL_SECONDS, audd=audd, kind=kind,
                        headroom=audd_headroom, from_hub=len(from_hub),
                        could_serve=len(hub_could_serve)),
        },
    }


def _level(todo: int, per_title: tuple[int, int], *, audd: bool, kind: str,
           headroom: int | None, from_hub: int, could_serve: int) -> dict:
    lo, hi = per_title
    out = {
        "todo": todo,
        "fromHub": from_hub,
        "hubCouldServe": could_serve,
        "seconds": [todo * lo, todo * hi],
        "dollars": [0.0, 0.0],
        "cues": [0, 0],
        "titlesBeforeCap": None,
    }
    if not audd or not todo:
        return out

    cue_lo, cue_hi = CUES_PER_TITLE.get(kind, CUES_PER_TITLE["movie"])
    cue_hi = min(cue_hi, MAX_CUES)
    calls = (todo * cue_lo, todo * cue_hi)
    out["cues"] = list(calls)
    out["dollars"] = [round(calls[0] * AUDD_PER_CALL, 2),
                      round(calls[1] * AUDD_PER_CALL, 2)]
    if headroom is not None and calls[1] > headroom:
        # Worst case is what matters here: the run stops when the budget is
        # gone, so quote the earliest title it could stop at.
        out["titlesBeforeCap"] = headroom // cue_hi
    return out


def hub_catalog_for(hub_url: str) -> dict | None:
    """Indirection so callers (and tests) can stub the network in one place."""
    from .share import hub_catalog
    return hub_catalog(hub_url)
