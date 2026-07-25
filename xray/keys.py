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


def jellyfin_token() -> str:
    return _read(".jellyfintoken", "JELLYFIN_TOKEN", "jellyfin_token")


def jellyfin_user() -> str:
    """Jellyfin userId (optional: some endpoints scope to a user)."""
    return _read(".jellyfinuser", "JELLYFIN_USER", "jellyfin_user")


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
