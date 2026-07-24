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


def hub_url() -> str:
    """Community hub base URL (BYO: no default is shipped)."""
    return _read(".huburl", "XRAY_HUB_URL", "hub_url").rstrip("/")
