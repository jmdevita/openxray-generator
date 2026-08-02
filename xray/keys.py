"""Credential lookup, three tiers: web-managed settings.json (the orchestrator
wizard writes it; see settings_store.py), gitignored key files at the repo
root (.plextoken / .tmdbkey / …, the CLI/dev path), env var fallback."""
from __future__ import annotations

import os
from pathlib import Path

from . import settings_store

ROOT = Path(__file__).resolve().parents[1]


def _read(filename: str, env: str, settings_key: str = "") -> str:
    if settings_key:
        v = settings_store.get(settings_key)
        if v:
            return v
    p = ROOT / filename
    if p.exists():
        return p.read_text().strip()
    return os.environ.get(env, "").strip()


def plex_token() -> str:
    return _read(".plextoken", "PLEX_TOKEN", "plex_token")


def tmdb_key() -> str:
    return _read(".tmdbkey", "TMDB_KEY", "tmdb_key")


def audd_token() -> str:
    return _read(".auddtoken", "AUDD_API_TOKEN", "audd_token")


def hf_token() -> str:
    """HuggingFace token, used ONLY to fetch the gated pyannote weights.

    Lower-value than the others in this module and needed for less time: it
    downloads openly-licensed models that sit behind an accept-the-conditions
    gate, and it stops being needed once the weights are cached. An image
    built with the weights baked in never needs one at all.
    """
    return _read(".hftoken", "HF_TOKEN", "hf_token")


def jellyfin_token() -> str:
    return _read(".jellyfintoken", "JELLYFIN_TOKEN", "jellyfin_token")


def jellyfin_user() -> str:
    """Jellyfin userId (optional: some endpoints scope to a user)."""
    return _read(".jellyfinuser", "JELLYFIN_USER", "jellyfin_user")


#: Where reference photos come from. Not offered in the setup UI: `tmdb`
#: exists so the choice is reversible in one env var, not so it is browsed.
ENROLLMENT_SOURCES = ("commons", "tmdb")


def enrollment_source() -> str:
    """Which photos build the face REFERENCE embeddings. Commons by default.

    TMDb's API terms §1.C bar using TMDb Content "in connection with … a
    machine learning (ML) or artificial intelligence (AI) based Application",
    and enrollment is exactly that use. Wikimedia Commons photos carry their
    own free licences and no such clause, so they are the default while
    written permission is outstanding.

    Only ENROLLMENT moves. Cast lists, character names, and the `thumb` URLs
    the timeline displays still come from TMDb -- ordinary metadata use,
    which the terms permit with attribution -- so the data contract is
    untouched and actorIds still read `tmdb:380`.

    Set XRAY_ENROLLMENT_SOURCE=tmdb to go back: it enrolls better (several
    photos per actor against usually one, and ~90% of top-billed cast against
    ~100%), and it becomes the sensible default the day TMDb says yes.
    """
    v = _read(".enrollmentsource", "XRAY_ENROLLMENT_SOURCE",
              "enrollment_source").strip().lower()
    return v if v in ENROLLMENT_SOURCES else "commons"


def backend_token(backend: str) -> str:
    """The auth token for whichever backend is selected."""
    return jellyfin_token() if (backend or "").lower() == "jellyfin" else plex_token()


DEFAULT_HUB = "https://hub.openxray.net"


def hub_url() -> str:
    """Community hub base URL, defaulting to the project's own hub.

    This is the API host: uploads, moderation, and the manifest. DOWNLOADS
    go wherever that manifest's `timelines` base points (currently the CDN),
    so nobody configures the CDN directly and it can move without a client
    release.

    Still overridable, because pointing the stack at a hub on localhost is
    how the hub itself gets developed and tested. Set it to "-" to run with
    no hub at all."""
    configured = _read(".huburl", "XRAY_HUB_URL", "hub_url").strip()
    if configured == "-":
        return ""
    return (configured or DEFAULT_HUB).rstrip("/")


def hub_autoshare() -> bool:
    """Whether finished timelines are uploaded to the hub automatically.

    OFF unless explicitly turned on. Indexing is a private act; publishing
    is not, and which titles you hold is disclosed by the upload itself. So
    this is a decision the operator makes once and on purpose, never a
    default that arrives with an install.

    Stored as a string because settings_store coerces values with str():
    a real False would round-trip to the truthy "False"."""
    return _read(".hubautoshare", "XRAY_HUB_AUTOSHARE",
                 "hub_autoshare").strip().lower() in ("1", "on", "true", "yes")
