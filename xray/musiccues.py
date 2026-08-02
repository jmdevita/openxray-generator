"""Music cues awaiting a name, and names given.

The music pass ends the way the speakers pass does: computed, correct, and
not finished. Segmentation reliably finds where music plays -- 31 cues over
2,418 seconds on the first feature it ran against -- but identification is a
different problem, and on that same feature AudD named exactly one of them.

So every cue is persisted, not just the ones a lookup happened to recognise,
and the rest are offered to a person who can hear them. `musicIntervals` in
the timeline is then rebuilt from cues + names, whichever way each was
answered. `source` records which: the contract already allowed for "audd"
(discovered) and "library" (owned); "manual" is a human saying so.

Cues carry NO audio, only times. The clip a person listens to is cut on
demand from the harvested mp3, which is why retention holds that file until
the naming is done (see xray/retention.py).
"""
from __future__ import annotations

from pathlib import Path

from . import prints

DIR = "music"

#: Cue identification sources, in descending order of how much we trust them.
SOURCE_MANUAL = "manual"
SOURCE_AUDD = "audd"


def cues_path(store_dir: Path, content_id: str) -> Path:
    return Path(store_dir) / DIR / f"{content_id}.json"


def names_path(store_dir: Path, content_id: str) -> Path:
    return Path(store_dir) / DIR / f"{content_id}.names.json"


def read_cues(store_dir: Path, content_id: str) -> dict | None:
    return prints.read_json(cues_path(store_dir, content_id))


def read_names(store_dir: Path, content_id: str) -> dict:
    return prints.read_json(names_path(store_dir, content_id), {}) or {}


def build_cues(*, content_id: str, cues, matches, generated: str,
               version: str) -> dict:
    """The cue document. `cues` are the segmenter's spans; `matches[i]`, when
    it has a title, is what the lookup made of cue i.

    Ordered longest first: a four-minute cue is a needle-drop somebody chose,
    a twelve-second one is usually a transition sting. Nobody wants to work
    through 31 rows, and this puts the ones worth naming at the top.
    """
    rows = []
    for i, cue in enumerate(cues):
        match = matches[i] if matches and i < len(matches) else None
        title = getattr(match, "title", None)
        rows.append({
            "cue": i,
            "startMs": int(cue.start * 1000),
            "endMs": int(cue.end * 1000),
            "seconds": round(cue.duration, 1),
            "matched": ({"title": title,
                         "artist": getattr(match, "artist", None),
                         "source": SOURCE_AUDD} if title else None),
        })
    rows.sort(key=lambda r: -r["seconds"])
    return {"contentId": content_id, "generated": generated,
            "version": version, "cues": rows}


def write_cues(store_dir: Path, content_id: str, doc: dict) -> Path:
    return prints.write_json(cues_path(store_dir, content_id), doc)


def name_cue(store_dir: Path, content_id: str, cue: int, *,
             title: str, artist: str = "") -> None:
    """Record what a person says cue `cue` is. An empty title clears it,
    which is the only way back from a typo."""
    names = read_names(store_dir, content_id)
    key = str(cue)
    if title.strip():
        names[key] = {"title": title.strip(), "artist": artist.strip()}
    else:
        names.pop(key, None)
    prints.write_json(names_path(store_dir, content_id), names)


def settled(cue: dict, names: dict) -> dict | None:
    """What this cue is, by any route, or None if still unknown.

    A human name outranks a lookup: somebody typed it while listening to the
    cue, which beats a probe of three ten-second windows.
    """
    given = names.get(str(cue["cue"]))
    if given and given.get("title"):
        return {"title": given["title"], "artist": given.get("artist") or None,
                "source": SOURCE_MANUAL}
    return cue.get("matched")


def unnamed(store_dir: Path, content_id: str) -> int:
    """Cues nobody has identified yet. Retention reads this: the harvested
    audio is what the previews are cut from, so it cannot be reclaimed while
    any cue is still waiting."""
    doc = read_cues(store_dir, content_id)
    if not doc:
        return 0
    names = read_names(store_dir, content_id)
    return sum(1 for c in doc.get("cues") or [] if settled(c, names) is None)


def intervals(store_dir: Path, content_id: str) -> list[dict]:
    """`musicIntervals` for the timeline, rebuilt from cues + names.

    Consecutive cues carrying the same song collapse into one interval, the
    way the AudD path already did -- a score that plays under a whole scene
    is one interval, not nine.
    """
    doc = read_cues(store_dir, content_id)
    if not doc:
        return []
    names = read_names(store_dir, content_id)
    # Back into time order: the document is sorted by length for the screen's
    # benefit, but a timeline is read against a clock.
    rows = sorted(doc.get("cues") or [], key=lambda r: r["startMs"])

    out: list[dict] = []
    for cue in rows:
        what = settled(cue, names)
        if not what:
            continue
        prev = out[-1] if out else None
        if prev and _same(prev["title"], what["title"]):
            prev["endMs"] = cue["endMs"]
            continue
        out.append({"title": what["title"], "artist": what.get("artist"),
                    "startMs": cue["startMs"], "endMs": cue["endMs"],
                    "confidence": None, "source": what["source"]})
    return out


def _same(a: str, b: str) -> bool:
    return "".join(ch for ch in (a or "").lower() if ch.isalnum()) == \
           "".join(ch for ch in (b or "").lower() if ch.isalnum())
