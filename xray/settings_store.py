"""Web-managed settings: settings.json in the timeline store volume.

The orchestrator's setup wizard writes here; `keys.py` reads here first, so
values set in the UI win everywhere (pipeline, passes, hub fetch). Env vars
are FIRST-BOOT SEEDS only: on first start the file is created from the
environment (deploy/.env), after which the web UI owns the values. The CLI's
key files at the repo root keep working as the second lookup tier; this
module is inert when no settings file exists (plain CLI/dev usage).

Security posture (SECURITY.md): the file is chmod 0600; secrets are redacted
on read-out (`redacted()`), and the web token gates every orchestrator route.
"""
from __future__ import annotations

import json
import os
import secrets
import tempfile
import uuid
from pathlib import Path

#: settings key → seeding env var (first boot only)
SEEDS = {
    "backend": "XRAY_BACKEND",
    "plex_origin": "PLEX_ORIGIN",
    "plex_token": "PLEX_TOKEN",
    "jellyfin_origin": "JELLYFIN_ORIGIN",
    "jellyfin_token": "JELLYFIN_TOKEN",
    "jellyfin_user": "JELLYFIN_USER",
    "tmdb_key": "TMDB_KEY",
    "audd_token": "AUDD_API_TOKEN",
    "hf_token": "HF_TOKEN",
    "hub_url": "XRAY_HUB_URL",
}

#: never returned un-redacted by the API, never logged
SECRET_KEYS = {"plex_token", "jellyfin_token", "tmdb_key", "audd_token",
               "hf_token", "web_token"}


def settings_path() -> Path | None:
    """Where settings.json lives: XRAY_SETTINGS wins, else the store volume.
    None when neither is configured (CLI/dev usage without a store env)."""
    explicit = os.environ.get("XRAY_SETTINGS", "").strip()
    if explicit:
        return Path(explicit)
    store = os.environ.get("XRAY_STORE", "").strip()
    return Path(store) / "settings.json" if store else None


def load() -> dict:
    p = settings_path()
    if p is None or not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def save(values: dict) -> None:
    p = settings_path()
    if p is None:
        raise RuntimeError("no settings path (set XRAY_STORE or XRAY_SETTINGS)")
    p.parent.mkdir(parents=True, exist_ok=True)
    # atomic + private: secrets live here
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=".settings-")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(values, f, indent=2)
        os.chmod(tmp, 0o600)
        os.replace(tmp, p)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def get(key: str) -> str:
    return str(load().get(key) or "").strip()


def update(patch: dict) -> dict:
    """Merge [patch] into the file (empty-string values delete the key)."""
    values = load()
    for k, v in patch.items():
        if v is None:
            continue
        v = str(v).strip()
        if v:
            values[k] = v
        else:
            values.pop(k, None)
    save(values)
    return values


def ensure_seeded() -> bool:
    """First boot: create settings.json from env seeds + generate the web-UI
    auth token and a persistent Plex client identifier. Returns True when the
    file was created (caller prints the web token to the logs ONCE)."""
    p = settings_path()
    if p is None or p.exists():
        return False
    values = {k: os.environ.get(env, "").strip()
              for k, env in SEEDS.items() if os.environ.get(env, "").strip()}
    values["web_token"] = secrets.token_urlsafe(32)
    # The Plex token is bound to this identifier, so persist it for life.
    values["client_id"] = str(uuid.uuid4())
    save(values)
    return True


def redacted() -> dict:
    """Settings safe to return from the API: secrets become presence flags
    with a last-4 hint; web_token is never surfaced at all."""
    out = {}
    for k, v in load().items():
        if k == "web_token":
            continue
        if k in SECRET_KEYS:
            v = str(v)
            out[k] = f"•••{v[-4:]}" if len(v) > 4 else "•••"
        else:
            out[k] = v
    return out
