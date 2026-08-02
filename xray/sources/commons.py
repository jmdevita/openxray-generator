"""Reference photos from Wikimedia Commons, resolved through Wikidata (P18).

The licence-clean enrollment source: TMDb's terms §1.C bar their content
"in connection with" an ML application, which reaches headshot enrollment.

Per cast member: TMDb person id → the Wikidata item claiming it → the item
must also NAME that actor → P18 → Commons URLs. Members with no TMDb link
fall back to a name search, accepted only for humans (P31=Q5), since names
collide with films, bands and asteroids. Emits the same cast shape as the
other sources in refs.py, so actorIds are untouched.
"""
from __future__ import annotations

import json
import time
import unicodedata
from difflib import SequenceMatcher
from pathlib import Path
from urllib.parse import quote

import requests

from . import wikimedia

WD_API = "https://www.wikidata.org/w/api.php"

#: Wikidata properties: TMDb person id, image, instance-of.
P_TMDB_PERSON, P_IMAGE, P_INSTANCE_OF = "P4985", "P18", "P31"
Q_HUMAN = "Q5"

#: How closely the Wikidata item's name must match the name we asked about
#: before its photo is trusted. Loose on purpose: the two databases disagree
#: about accents and given names constantly ("Dola Rashad" for "Condola
#: Rashād"), and rejecting those would cost real coverage. Tight enough that
#: a different person cannot pass -- a wrong P4985 statement scores near
#: zero, while the messiest true pair we measured scores ~0.87.
NAME_AGREEMENT = 0.6

#: Special:FilePath redirects to the actual file; ?width= asks Commons for a
#: server-side thumbnail, mirroring TMDb's w342 profile size.
_FILEPATH = "https://commons.wikimedia.org/wiki/Special:FilePath/{}?width={}"

#: Cross-title photo cache, beside the timelines (the people_cache.json
#: pattern). Resolution is the expensive half: Wikimedia rate-limits bursts
#: hard, so a 40-member cast costs minutes the first time it is seen.
CACHE_NAME = "commons_photos.json"

#: How long a MISS stands before we ask again. A hit never expires -- a photo
#: that exists keeps existing, and a stale URL merely fails one download --
#: but "nobody has photographed this actor yet" is exactly the kind of fact
#: that changes, so absence is rechecked monthly rather than believed forever.
MISS_TTL_S = 30 * 86400


def _session():
    return wikimedia.session("commons-refs")


def _get(session, **params):
    return wikimedia.get_json(session, WD_API, **params)


# --- resolution -----------------------------------------------------------

def qid_for_tmdb_person(pid, session):
    """The Wikidata item carrying P4985=<pid>, or None.

    Precise but not infallible: the statement is crowd-maintained, so a wrong
    or stale one points at a different person entirely. Callers must still
    check the name agrees (`names_agree`) before trusting the photo.
    """
    j = _get(session, action="query", list="search",
             srsearch=f"haswbstatement:{P_TMDB_PERSON}={pid}", srlimit=1)
    hits = (j.get("query") or {}).get("search") or []
    return hits[0]["title"] if hits else None


def qid_by_name(name, session, limit=5):
    """Name-search fallback: candidate QIDs, best first. Callers must check
    P31=Q5 before trusting one."""
    j = _get(session, action="wbsearchentities", search=name,
             language="en", type="item", limit=limit)
    return [e["id"] for e in j.get("search") or []]


def entities(qids, session, chunk=50):
    """{qid: entity} for many items in few requests; wbgetentities takes up
    to 50 ids per call. Labels and aliases ride along with the claims in the
    same request, so verifying the name costs nothing extra."""
    out = {}
    qids = list(dict.fromkeys(qids))
    for i in range(0, len(qids), chunk):
        batch = qids[i:i + chunk]
        j = _get(session, action="wbgetentities", ids="|".join(batch),
                 props="claims|labels|aliases", languages="en")
        out.update(j.get("entities") or {})
    return out


def claims_of(entity: dict | None) -> dict:
    return (entity or {}).get("claims") or {}


# --- does this item name the person we asked about? -----------------------

def _fold(s: str) -> str:
    """Accent-stripped, punctuation-free, lowercase: the only differences we
    are willing to call cosmetic."""
    n = unicodedata.normalize("NFKD", s or "")
    n = "".join(c for c in n if not unicodedata.combining(c))
    return "".join(c for c in n.lower() if c.isalnum())


def item_names(entity: dict | None) -> list[str]:
    """The English label plus any English aliases. Aliases matter: they are
    where Wikidata keeps the stage name when the label is the legal one."""
    e = entity or {}
    names = []
    label = ((e.get("labels") or {}).get("en") or {}).get("value")
    if label:
        names.append(label)
    names += [a.get("value") for a in (e.get("aliases") or {}).get("en") or []
              if a.get("value")]
    return names


def names_agree(entity, name, threshold: float = NAME_AGREEMENT) -> bool:
    """Does this Wikidata item plausibly name `name`?

    The guard against a bad P4985: without it a wrong statement enrolls
    another person's face, and nothing anywhere reports an error -- the
    timeline simply says the wrong actor was on screen.
    """
    if not name:
        return False
    want = _fold(name)
    return any(SequenceMatcher(None, want, _fold(n)).ratio() >= threshold
               for n in item_names(entity))


# --- claims → photos ------------------------------------------------------

def _statement_values(claims, prop):
    vals = []
    for c in claims.get(prop) or []:
        v = (c.get("mainsnak") or {}).get("datavalue", {}).get("value")
        if v is not None:
            vals.append(v)
    return vals


def _is_human(entity):
    return any(isinstance(v, dict) and v.get("id") == Q_HUMAN
               for v in _statement_values(claims_of(entity), P_INSTANCE_OF))


def image_urls(entity, max_images, width=342):
    """P18 statements → fetchable Commons URLs (spaces are underscores in
    Commons file titles; everything else gets percent-encoded)."""
    files = [v for v in _statement_values(claims_of(entity), P_IMAGE)
             if isinstance(v, str)]
    return [_FILEPATH.format(quote(f.replace(" ", "_")), width)
            for f in files[:max_images]]


# --- the cast source ------------------------------------------------------

def commons_cast(cast, session=None, max_images=4, log=print):
    """`cast` (from any refs.py source) with photos re-pointed at Commons.

    Identity fields pass through untouched; `images`/`thumb` are replaced —
    empty when Wikidata has no linked photo, which enrollment already skips
    with a log line. Wikidata typically holds ONE portrait per person
    against TMDb's several, so expect thinner but nonzero references.
    """
    s = session or _session()

    exact = {}      # cast index -> qid
    fallback = {}   # cast index -> candidate qids awaiting the human check
    for i, m in enumerate(cast):
        if i:
            time.sleep(0.1)   # pace the per-member lookups; Wikimedia 429s bursts
        actor_id = str(m.get("actorId") or "")
        qid = None
        if actor_id.startswith("tmdb:"):
            try:
                qid = qid_for_tmdb_person(actor_id.split(":", 1)[1], s)
            except requests.RequestException:
                qid = None
        if qid:
            exact[i] = qid
        elif m.get("name"):
            try:
                fallback[i] = qid_by_name(m["name"], s)
            except requests.RequestException:
                fallback[i] = []

    need = list(exact.values()) + [q for qs in fallback.values() for q in qs]
    ents = entities(need, s) if need else {}

    qid_of = {}
    for i, qid in exact.items():
        # An external-id match is precise only if the statement is right.
        # When the item turns out to name somebody else, drop it rather than
        # enrolling their face under this actor's id.
        if names_agree(ents.get(qid), cast[i].get("name")):
            qid_of[i] = qid
        else:
            log(f"  (Wikidata {qid} claims TMDb id for "
                f"{cast[i].get('name', '?')} but names "
                f"{(item_names(ents.get(qid)) or ['nobody'])[0]!r}; ignored)")
    for i, candidates in fallback.items():
        # Searched BY name, so the name matches by construction; what needs
        # checking here is that the hit is a person and not a film.
        qid_of[i] = next((q for q in candidates
                          if _is_human(ents.get(q))), None)

    out = []
    for i, m in enumerate(cast):
        qid = qid_of.get(i)
        urls = image_urls(ents.get(qid), max_images) if qid else []
        if not urls:
            log(f"  (no Commons photo for {m.get('name', '?')})")
        out.append({**m, "thumb": urls[0] if urls else None, "images": urls})
    return out


# --- the cached front door ------------------------------------------------

def _load_cache(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _fresh(rec: dict | None, now: float, miss_ttl_s: float) -> bool:
    """No record at all is never fresh -- checked explicitly rather than left
    to a zero timestamp, which reads as "checked at the epoch" and would look
    expired only because the clock happens to be large."""
    if not rec:
        return False
    if rec.get("images"):
        return True
    return (now - float(rec.get("checked") or 0)) < miss_ttl_s


def cast_with_cache(cast, cache_path, *, session=None, max_images=4,
                    miss_ttl_s: float = MISS_TTL_S, now: float | None = None,
                    log=print):
    """`commons_cast`, asking the network only about members it has not seen.

    Actors recur across a library -- the same forty people carry a season --
    so resolution is a per-actor cost paid once, not a per-title cost paid
    forever. Keyed on actorId, which is stable across titles.
    """
    now = time.time() if now is None else now
    path = Path(cache_path)
    cache = _load_cache(path)

    stale = [m for m in cast
             if not _fresh(cache.get(str(m.get("actorId"))), now, miss_ttl_s)]
    if stale:
        log(f"  (resolving {len(stale)} of {len(cast)} cast on Wikidata; "
            f"{len(cast) - len(stale)} already cached)")
        for m in commons_cast(stale, session=session, max_images=max_images,
                              log=log):
            cache[str(m.get("actorId"))] = {"images": m.get("images") or [],
                                            "checked": now}
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(cache), encoding="utf-8")
        tmp.replace(path)

    out = []
    for m in cast:
        urls = (cache.get(str(m.get("actorId"))) or {}).get("images") or []
        out.append({**m, "thumb": urls[0] if urls else None, "images": urls})
    return out
