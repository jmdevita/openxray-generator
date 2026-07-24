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

TMDB = "https://api.themoviedb.org/3"


def _fetch_bytes(url, timeout=20):
    r = requests.get(url, timeout=timeout)
    r.raise_for_status()
    return r.content


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


def cast_from_tmdb(tmdb_id, api_key, max_images=5, max_cast=60, timeout=20):
    cr = requests.get(f"{TMDB}/movie/{tmdb_id}/credits",
                      params={"api_key": api_key}, timeout=timeout)
    cr.raise_for_status()
    members = sorted(cr.json().get("cast", []), key=lambda c: c.get("order", 999))
    return [_tmdb_member(c["id"], c.get("name", ""), c.get("character", ""),
                         c.get("profile_path"), api_key, max_images)
            for c in members[:max_cast]]


def search_tv(name, api_key, timeout=20):
    r = requests.get(f"{TMDB}/search/tv",
                     params={"api_key": api_key, "query": name}, timeout=timeout)
    r.raise_for_status()
    return r.json().get("results", [])


def cast_from_tmdb_tv(tv_id, api_key, season=1, episode=1, max_images=5,
                      max_cast=40, timeout=20):
    """Series regulars (aggregate_credits) + this episode's guest stars, deduped.

    A pilot's on-screen faces are the recurring leads plus episode-specific
    guests; combining both gives the reference set Spike 1 labels against.
    """
    people = {}  # pid -> (name, character, profile_path, order)
    ac = requests.get(f"{TMDB}/tv/{tv_id}/aggregate_credits",
                      params={"api_key": api_key}, timeout=timeout)
    if ac.ok:
        for c in ac.json().get("cast", []):
            char = c["roles"][0].get("character", "") if c.get("roles") else ""
            people[c["id"]] = (c.get("name", ""), char, c.get("profile_path"),
                               c.get("order", 999))
    if season and episode:
        ec = requests.get(
            f"{TMDB}/tv/{tv_id}/season/{season}/episode/{episode}/credits",
            params={"api_key": api_key}, timeout=timeout)
        if ec.ok:
            j = ec.json()
            for c in j.get("cast", []) + j.get("guest_stars", []):
                people.setdefault(c["id"], (c.get("name", ""),
                                            c.get("character", ""),
                                            c.get("profile_path"), 500))
    ordered = sorted(people.items(), key=lambda kv: kv[1][3])[:max_cast]
    return [_tmdb_member(pid, n, ch, pp, api_key, max_images)
            for pid, (n, ch, pp, _order) in ordered]


# --- reference embeddings -------------------------------------------------

def _collect_reference_embeddings(cast, vec_for_image, max_actors, log,
                                  fetch=None):
    """Shared enrollment loop for both transports: download each member's
    photos, extract one best-face vector per photo via
    `vec_for_image(image_bytes) -> vec | None`, and average+normalize per
    actor. Actors with no usable photo are skipped (can't be matched).
    `fetch` resolves at call time (module attr) so tests can patch it."""
    fetch = fetch or _fetch_bytes
    refs = {}
    for member in cast:
        if max_actors and len(refs) >= max_actors:
            break
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


def build_reference_embeddings(cast, embedder, max_actors=None, log=print):
    """{actorId: normalized_avg_vector} via the in-process engine.

    Detects the largest face in each reference photo (group-shot protection),
    embeds it, and averages per actor.
    """
    def vec_for_image(content):
        img = cv2.imdecode(np.frombuffer(content, dtype=np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            return None
        dets = embedder.detect(img)
        if not dets:
            return None
        best = max(dets, key=lambda d: d.bbox[2] * d.bbox[3])
        return embedder.embed(img, best)

    return _collect_reference_embeddings(cast, vec_for_image, max_actors, log)


def build_reference_embeddings_http(cast, transport, spool_dir,
                                    max_actors=None, log=print):
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
        faces = transport.embed_image_file(spool)
        if not faces:
            return None
        best = max(faces, key=lambda f: f["bbox"][2] * f["bbox"][3])
        return np.asarray(best["embedding"], dtype=np.float32)

    try:
        return _collect_reference_embeddings(cast, vec_for_image, max_actors, log)
    finally:
        spool.unlink(missing_ok=True)


def public_cast(cast):
    """Drop the internal `images` list; keep the schema's cast shape."""
    return [{k: m[k] for k in ("actorId", "name", "character", "thumb")}
            for m in cast]
