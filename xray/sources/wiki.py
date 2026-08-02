"""Untimed title-level trivia from Wikidata (structured) + Wikipedia (editorial).

Free and deterministic, no LLM, no paid API (plan.md §2 ledger, §6.3; research
doc §7 v1). Resolution chain: TMDb external_ids → Wikidata entity → English
Wikipedia article. Emits schema `trivia` items with startMs/endMs = null
(title-level, not scene-pinned; timed trivia is a stretch goal).
"""
from __future__ import annotations

import re

from . import wikimedia

TMDB = "https://api.themoviedb.org/3"
WD_API = "https://www.wikidata.org/w/api.php"
WP_API = "https://en.wikipedia.org/w/api.php"

# Wikidata property → (sentence template, is the value an entity to label-resolve)
WD_PROPS = {
    "P170": ("Created by {v}.", True),
    "P57":  ("Directed by {v}.", True),
    "P58":  ("Written by {v}.", True),
    "P86":  ("Music by {v}.", True),
    "P449": ("Originally aired on {v}.", True),
    "P272": ("Produced by {v}.", True),
    "P1431": ("Executive produced by {v}.", True),
    "P915": ("Filmed in {v}.", True),
    "P840": ("Set in {v}.", True),
    "P144": ("Based on {v}.", True),
    "P136": ("Genre: {v}.", True),
    "P495": ("A {v} production.", True),
}

# "Juicy" facts we surface first; "admin" (renewal/air-date boilerplate) is
# factual but dull, so it's capped rather than allowed to dominate the panel.
_JUICY = re.compile(
    r"\b(inspired by|based on|adapted|real[- ]life|first|only|record|budget|"
    r"\$|million|cameo|non-binary|controversy|lawsuit|banned|improvised|"
    r"reportedly|shot on location|filmed on location|refused|conceived|"
    r"originally (?:cast|written|titled|conceived)|drew on|modeled)\b", re.I)
_ADMIN = re.compile(
    r"\b(renewed|ordered|premiered|will premiere|announced|greenlit)\b", re.I)


def _session():
    return wikimedia.session("trivia")


def _join(names):
    names = list(dict.fromkeys(names))  # dedup, preserve order
    if len(names) == 1:
        return names[0]
    if len(names) == 2:
        return f"{names[0]} and {names[1]}"
    return ", ".join(names[:-1]) + f", and {names[-1]}"


_MONTHS = ["January", "February", "March", "April", "May", "June", "July",
           "August", "September", "October", "November", "December"]


def _pretty_date(iso):
    try:
        y, m, d = iso.split("-")
        return f"{_MONTHS[int(m) - 1]} {int(d)}, {y}"
    except (ValueError, IndexError):
        return iso


# --- resolution -----------------------------------------------------------

def resolve_ids(media_type, tmdb_id, tmdb_key, session):
    ext = session.get(f"{TMDB}/{media_type}/{tmdb_id}/external_ids",
                      params={"api_key": tmdb_key}).json()
    return ext.get("wikidata_id"), ext.get("imdb_id")


def _wikidata_entity(qid, session, props):
    r = session.get(WD_API, params={"action": "wbgetentities", "ids": qid,
                                     "format": "json", "props": props,
                                     "languages": "en"}).json()
    return r["entities"][qid]


def enwiki_title(qid, session):
    e = _wikidata_entity(qid, session, "sitelinks")
    return e.get("sitelinks", {}).get("enwiki", {}).get("title")


# --- Wikidata structured facts -------------------------------------------

def wikidata_facts(qid, session, max_values=4):
    claims = _wikidata_entity(qid, session, "claims").get("claims", {})

    need, prop_vals = set(), {}
    for pid, (_tmpl, is_ent) in WD_PROPS.items():
        if pid not in claims:
            continue
        vals = []
        for c in claims[pid][:max_values]:
            dv = c.get("mainsnak", {}).get("datavalue", {})
            if is_ent and dv.get("type") == "wikibase-entityid":
                qv = dv["value"]["id"]
                vals.append(qv)
                need.add(qv)
        if vals:
            prop_vals[pid] = vals

    # batch-resolve entity labels
    labels = {}
    ids = list(need)
    for i in range(0, len(ids), 50):
        chunk = ids[i:i + 50]
        r = session.get(WD_API, params={
            "action": "wbgetentities", "ids": "|".join(chunk),
            "props": "labels", "languages": "en", "format": "json"}).json()
        for qv, ent in r.get("entities", {}).items():
            labels[qv] = ent.get("labels", {}).get("en", {}).get("value", qv)

    facts = []
    for pid, vals in prop_vals.items():
        names = [labels.get(v, v) for v in vals]
        facts.append(WD_PROPS[pid][0].format(v=_join(names)))

    # aired-years from time-literal claims
    def year(pid):
        try:
            return claims[pid][0]["mainsnak"]["datavalue"]["value"]["time"][1:5]
        except (KeyError, IndexError):
            return None
    a, b = year("P580"), year("P582")
    if a and b:
        facts.append(f"Aired from {a} to {b}.")
    elif a:
        facts.append(f"First aired in {a}.")
    return facts


# --- Wikipedia editorial facts -------------------------------------------

def _sections_plaintext(title, session):
    r = session.get(WP_API, params={
        "action": "query", "prop": "extracts", "explaintext": 1,
        "exsectionformat": "wiki", "redirects": 1, "titles": title,
        "format": "json"}).json()
    page = next(iter(r["query"]["pages"].values()))
    text = page.get("extract", "")
    sections, cur, buf = {}, "_lead", []
    for line in text.split("\n"):
        m = re.match(r"^==+\s*(.+?)\s*==+$", line.strip())
        if m:
            sections[cur] = "\n".join(buf).strip()
            cur, buf = m.group(1), []
        else:
            buf.append(line)
    sections[cur] = "\n".join(buf).strip()
    return sections


def _sentences(text):
    text = re.sub(r"\s+", " ", text)
    parts = re.split(r"(?<=[.!?])\s+(?=[A-Z0-9\"'])", text)
    return [p.strip() for p in parts if len(p.strip()) > 40]


def wikipedia_facts(title, session, want=("Production", "Development",
                                          "Filming", "Writing", "Casting"),
                    max_facts=8, max_admin=2):
    secs = _sections_plaintext(title, session)
    juicy, admin = [], []
    for name in want:
        for sent in _sentences(secs.get(name, "")):
            if _JUICY.search(sent):
                juicy.append(sent)
            elif _ADMIN.search(sent):
                admin.append(sent)
    # juicy facts first, then a couple of admin facts to fill
    return (juicy + admin[:max_admin])[:max_facts]


# --- top level ------------------------------------------------------------

def title_trivia(media_type, tmdb_id, tmdb_key, max_facts=12):
    """Return schema `trivia` items for a title. media_type: 'tv' | 'movie'."""
    s = _session()
    qid, _imdb = resolve_ids(media_type, tmdb_id, tmdb_key, s)

    collected = []  # (text, source)
    title = None
    if qid:
        title = enwiki_title(qid, s)
    if title:  # Wikipedia editorial facts first (more "fun")
        collected += [(f, "wikipedia") for f in wikipedia_facts(title, s)]
    if qid:
        collected += [(f, "wikidata") for f in wikidata_facts(qid, s)]

    seen, out = set(), []
    for text, source in collected:
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append({"text": text, "source": source,
                    "startMs": None, "endMs": None})
        if len(out) >= max_facts:
            break
    return out


def episode_trivia(tv_id, season, episode, tmdb_key, max_facts=10):
    """Facts specific to ONE episode (TMDb episode crew / guest stars / air date,
    plus a Wikipedia episode article if the episode is notable enough to have
    one). Still untimed: episode-level, not scene-pinned. Tops up with
    series-level facts when the episode's own data is thin.
    """
    s = _session()
    ep = s.get(f"{TMDB}/tv/{tv_id}/season/{season}/episode/{episode}",
               params={"api_key": tmdb_key}).json()

    facts = []  # (text, source)
    name, air = ep.get("name"), ep.get("air_date")
    if name and air:
        facts.append((f'"{name}" first aired {_pretty_date(air)}.', "tmdb"))
    elif air:
        facts.append((f"First aired {_pretty_date(air)}.", "tmdb"))

    crew = ep.get("crew", [])
    directors = [c["name"] for c in crew if c.get("job") == "Director"]
    writers = [c["name"] for c in crew
               if c.get("job") in ("Writer", "Screenplay", "Teleplay", "Story")]
    if directors:
        facts.append((f"Directed by {_join(directors)}.", "tmdb"))
    if writers:
        facts.append((f"Written by {_join(writers)}.", "tmdb"))

    guests = [g["name"] for g in ep.get("guest_stars", []) if g.get("name")][:5]
    if guests:
        facts.append((f"Guest stars: {_join(guests)}.", "tmdb"))

    if ep.get("runtime"):
        facts.append((f"Runs about {ep['runtime']} minutes.", "tmdb"))

    # Wikipedia episode article, if the episode is notable enough to have one
    ext = s.get(f"{TMDB}/tv/{tv_id}/season/{season}/episode/{episode}/external_ids",
                params={"api_key": tmdb_key}).json()
    qid = ext.get("wikidata_id")
    if qid:
        title = enwiki_title(qid, s)
        if title:
            facts += [(f, "wikipedia") for f in
                      wikipedia_facts(title, s,
                                      want=("Production", "Reception"),
                                      max_facts=4)]

    seen, out = set(), []
    for text, src in facts:
        if text.lower() in seen:
            continue
        seen.add(text.lower())
        out.append({"text": text, "source": src, "startMs": None, "endMs": None})
        if len(out) >= max_facts:
            break

    # thin episode → top up with series-level facts
    if len(out) < 5:
        for item in title_trivia("tv", tv_id, tmdb_key, max_facts=6):
            if item["text"].lower() in seen:
                continue
            seen.add(item["text"].lower())
            out.append(item)
            if len(out) >= max_facts:
                break
    return out
