"""Cast + reference photos → per-actor reference embeddings (plan.md §5.3).

Enrollment is the actual hard part. One studio headshot is a weak reference;
TMDb gives several profile photos per actor which we average into a sturdier
per-actor vector. Plex gives a single thumb: usable, but weaker. Both supported so
the spike can run with or without a TMDb key.
"""
from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np
import requests

from .sources import wikimedia

TMDB = "https://api.themoviedb.org/3"

#: One Session for every reference photo: identifies us to Wikimedia (which
#: rejects anonymous clients), waits out the 429s Commons sends under a burst
#: of downloads, and reuses connections. TMDb ignores all three, so its path
#: is unaffected.
_PHOTOS = wikimedia.session("refs")


def _fetch_bytes(url, timeout=20):
    return wikimedia.get(_PHOTOS, url, timeout=timeout).content


# --- cast sources ---------------------------------------------------------

def cast_from_plex(origin, token, rating_key, timeout=20):
    r = requests.get(
        f"{origin}/library/metadata/{rating_key}",
        headers={"Accept": "application/json", "X-Plex-Token": token},
        timeout=timeout,
    )
    r.raise_for_status()
    md = r.json()["MediaContainer"]["Metadata"][0]
    cast = []
    for role in md.get("Role", []):
        thumb = role.get("thumb")
        images = []
        if thumb:
            images = [thumb if thumb.startswith("http")
                      else f"{origin}{thumb}?X-Plex-Token={token}"]
        cast.append({
            "actorId": f"plex:{role.get('tagKey') or role.get('id')}",
            "name": role.get("tag", ""),
            "character": role.get("role", ""),
            "thumb": images[0] if images else None,
            "images": images,
        })
    return cast


def _person_image_urls(pid, api_key, max_images, timeout=20):
    try:
        imgs = requests.get(f"{TMDB}/person/{pid}/images",
                            params={"api_key": api_key}, timeout=timeout)
        profiles = imgs.json().get("profiles", []) if imgs.ok else []
        return [f"https://image.tmdb.org/t/p/w342{p['file_path']}"
                for p in profiles[:max_images]]
    except requests.RequestException:
        return []


def _tmdb_member(pid, name, character, profile_path, api_key, max_images):
    # max_images=0: thumb-only mode (level-0 seeding): the credits response
    # already carries profile_path, so skip the per-person images call
    # entirely (60-cast title: 2 TMDb calls instead of ~62).
    urls = _person_image_urls(pid, api_key, max_images) if max_images else []
    if not urls and profile_path:
        urls = [f"https://image.tmdb.org/t/p/w342{profile_path}"]
    return {
        "actorId": f"tmdb:{pid}",
        "name": name,
        "character": character,
        "thumb": urls[0] if urls else None,
        "images": urls,
    }


def _year(date_str):
    """Leading year of a TMDb date ("1990-09-19" -> 1990), or None."""
    head = (date_str or "")[:4]
    return int(head) if head.isdigit() else None


#: TMDb's Animation genre. Same id for movies and TV.
ANIMATION_GENRE_ID = 16


def _is_animated(details: dict) -> bool:
    """Is this an animated title, per TMDb's genre list?

    Free: `genres` already rides along in the details response the cast call
    makes, so this costs no extra request. Matched on the numeric id, not the
    name, because `name` is localised.

    Faces cannot work on animation (see docs/ANIMATION.md): YuNet needs
    five-point human landmarks and most animated principals are not humanoid.
    Callers use this to skip the face passes rather than spend a full
    extraction producing an empty timeline.
    """
    for g in (details.get("genres") or []):
        if isinstance(g, dict) and g.get("id") == ANIMATION_GENRE_ID:
            return True
    return False


def movie_bundle(tmdb_id, api_key, max_images=5, max_cast=60, timeout=20):
    """Credits AND display labels for a film, in ONE request.

    `append_to_response=credits` folds the credits payload into the details
    call, so picking up title/year costs no extra round trip: it is the same
    request the cast always needed, at a different URL."""
    r = requests.get(f"{TMDB}/movie/{tmdb_id}",
                     params={"api_key": api_key,
                             "append_to_response": "credits"}, timeout=timeout)
    r.raise_for_status()
    j = r.json()
    members = sorted((j.get("credits") or {}).get("cast", []),
                     key=lambda c: c.get("order", 999))
    return {
        "cast": [_tmdb_member(c["id"], c.get("name", ""), c.get("character", ""),
                              c.get("profile_path"), api_key, max_images)
                 for c in members[:max_cast]],
        "labels": {"title": j.get("title") or j.get("original_title"),
                   "year": _year(j.get("release_date")),
                   "series": None},
        "animated": _is_animated(j),
    }


def cast_from_tmdb(tmdb_id, api_key, max_images=5, max_cast=60, timeout=20):
    """Cast only; use movie_bundle when the display labels are wanted too."""
    return movie_bundle(tmdb_id, api_key, max_images=max_images,
                        max_cast=max_cast, timeout=timeout)["cast"]


def search_tv(name, api_key, timeout=20):
    r = requests.get(f"{TMDB}/search/tv",
                     params={"api_key": api_key, "query": name}, timeout=timeout)
    r.raise_for_status()
    return r.json().get("results", [])


def episode_bundle(tv_id, api_key, season=1, episode=1, max_images=5,
                   max_cast=40, timeout=20):
    """Series regulars + this episode's guest stars, AND display labels.

    A pilot's on-screen faces are the recurring leads plus episode-specific
    guests; combining both gives the reference set the labeller works against.

    Still the same two requests as before: `append_to_response` hangs the
    credits off the series and episode details calls, so the show name and
    the episode name arrive for free. `series` is the show's name and `title`
    is the episode's own, because an episode called "Pilot" cannot be
    identified without both.
    """
    people = {}  # pid -> (name, character, profile_path, order)
    labels = {"title": None, "year": None, "series": None}
    animated = False   # series-level: the show is animated, not the episode

    ac = requests.get(f"{TMDB}/tv/{tv_id}",
                      params={"api_key": api_key,
                              "append_to_response": "aggregate_credits"},
                      timeout=timeout)
    if ac.ok:
        j = ac.json()
        labels["series"] = j.get("name") or j.get("original_name")
        animated = _is_animated(j)
        for c in (j.get("aggregate_credits") or {}).get("cast", []):
            char = c["roles"][0].get("character", "") if c.get("roles") else ""
            people[c["id"]] = (c.get("name", ""), char, c.get("profile_path"),
                               c.get("order", 999))
    if season and episode:
        ec = requests.get(
            f"{TMDB}/tv/{tv_id}/season/{season}/episode/{episode}",
            params={"api_key": api_key, "append_to_response": "credits"},
            timeout=timeout)
        if ec.ok:
            j = ec.json()
            labels["title"] = j.get("name")
            labels["year"] = _year(j.get("air_date"))
            credits = j.get("credits") or {}
            for c in (credits.get("cast", []) + credits.get("guest_stars", [])
                      + j.get("guest_stars", [])):
                people.setdefault(c["id"], (c.get("name", ""),
                                            c.get("character", ""),
                                            c.get("profile_path"), 500))
    ordered = sorted(people.items(), key=lambda kv: kv[1][3])[:max_cast]
    return {
        "cast": [_tmdb_member(pid, n, ch, pp, api_key, max_images)
                 for pid, (n, ch, pp, _order) in ordered],
        "labels": labels,
        "animated": animated,
    }


def cast_from_tmdb_tv(tv_id, api_key, season=1, episode=1, max_images=5,
                      max_cast=40, timeout=20):
    """Cast only; use episode_bundle when the display labels are wanted too."""
    return episode_bundle(tv_id, api_key, season=season, episode=episode,
                          max_images=max_images, max_cast=max_cast,
                          timeout=timeout)["cast"]


# --- reference embeddings -------------------------------------------------

#: A reference photo whose second face is at least this fraction of the
#: largest face's area is AMBIGUOUS and gets skipped: a co-portrait (Commons
#: P18 photos are sometimes "actor with partner/castmate") would otherwise
#: silently enroll the wrong person. Background faces in a group shot are
#: much smaller and still pass.
AMBIGUOUS_FACE_RATIO = 0.5


def _dominant(faces, area):
    """The clearly-largest face by `area(face)`, or None when ambiguous."""
    if not faces:
        return None
    by_area = sorted(faces, key=area, reverse=True)
    if len(by_area) > 1 and area(by_area[1]) >= AMBIGUOUS_FACE_RATIO * area(by_area[0]):
        return None
    return by_area[0]


def _collect_reference_embeddings(cast, vec_for_image, max_actors, log,
                                  fetch=None, on_progress=None):
    """Shared enrollment loop for both transports: download each member's
    photos, extract one best-face vector per photo via
    `vec_for_image(image_bytes) -> vec | None`, and average+normalize per
    actor. Actors with no usable photo are skipped (can't be matched).
    `fetch` resolves at call time (module attr) so tests can patch it."""
    fetch = fetch or _fetch_bytes
    refs = {}
    for i, member in enumerate(cast):
        if max_actors and len(refs) >= max_actors:
            break
        # A download plus a detect+embed per photo. Counted over the cast
        # rather than over photos: a member may have one or four, and a
        # denominator that moves is worse than a coarse one.
        if on_progress:
            on_progress(i, len(cast))
        vecs = []
        for url in member.get("images", []):
            try:
                v = vec_for_image(fetch(url))
            except requests.RequestException:
                continue
            if v is not None:
                vecs.append(v)
        if vecs:
            mean = np.mean(np.stack(vecs), axis=0)
            n = np.linalg.norm(mean)
            refs[member["actorId"]] = mean / n if n else mean
        else:
            log(f"  (no usable reference photo for {member['name']})")
    return refs


def build_reference_embeddings(cast, embedder, max_actors=None, log=print,
                               on_progress=None):
    """{actorId: normalized_avg_vector} via the in-process engine.

    Detects the largest face in each reference photo (group-shot protection),
    embeds it, and averages per actor.
    """
    def vec_for_image(content):
        img = cv2.imdecode(np.frombuffer(content, dtype=np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            return None
        best = _dominant(embedder.detect(img),
                         lambda d: d.bbox[2] * d.bbox[3])
        return embedder.embed(img, best) if best is not None else None

    return _collect_reference_embeddings(cast, vec_for_image, max_actors, log,
                                         on_progress=on_progress)


def build_reference_embeddings_http(cast, transport, spool_dir,
                                    max_actors=None, log=print,
                                    on_progress=None):
    """{actorId: normalized_avg_vector} via the engine-faces service.

    Photos are spooled one at a time to [spool_dir] (which must live on the
    volume shared with the service) and embedded via /embed-image; the
    largest returned bbox is picked, mirroring the local path.
    """
    spool_dir = Path(spool_dir)
    spool_dir.mkdir(parents=True, exist_ok=True)
    spool = spool_dir / "ref_photo.jpg"

    def vec_for_image(content):
        spool.write_bytes(content)
        best = _dominant(transport.embed_image_file(spool),
                         lambda f: f["bbox"][2] * f["bbox"][3])
        if best is None:
            return None
        return np.asarray(best["embedding"], dtype=np.float32)

    try:
        return _collect_reference_embeddings(cast, vec_for_image, max_actors,
                                             log, on_progress=on_progress)
    finally:
        spool.unlink(missing_ok=True)


def public_cast(cast):
    """Drop the internal `images` list; keep the schema's cast shape."""
    return [{k: m[k] for k in ("actorId", "name", "character", "thumb")}
            for m in cast]
