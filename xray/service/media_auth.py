"""Sign-in flows for the setup wizard: Plex PIN + Jellyfin Quick Connect.

Both are poll-based handshakes with the same shape (create → the user acts
somewhere trusted → poll → token), so the orchestrator endpoints and any
other frontend consume the same helpers. Credentials never touch this app in
the Plex flow (the user signs in on plex.tv); Jellyfin Quick Connect is
likewise credential-free, with password auth as the fallback for servers
that have Quick Connect disabled.

Plex flow (strong PIN):
  1. plex_create_pin()  → POST plex.tv/api/v2/pins (strong=true)
  2. user opens authUrl → signs in on app.plex.tv
  3. plex_check_pin()   → GET plex.tv/api/v2/pins/{id} until authToken
  4. plex_servers()     → GET plex.tv/api/v2/resources → pick an origin
The token is bound to the X-Plex-Client-Identifier, so persist it (settings
`client_id`) or the token dies. The app appears under the user's Plex
account → Authorized Devices, individually revocable (SECURITY.md).
"""
from __future__ import annotations

import requests

from .. import __version__

PLEX_TV = "https://plex.tv/api/v2"
PRODUCT = "xray"
TIMEOUT = 15


# --- Plex ------------------------------------------------------------------

def _plex_headers(client_id: str, token: str = "") -> dict:
    h = {
        "Accept": "application/json",
        "X-Plex-Product": PRODUCT,
        "X-Plex-Version": __version__,
        "X-Plex-Client-Identifier": client_id,
    }
    if token:
        h["X-Plex-Token"] = token
    return h


def plex_create_pin(client_id: str) -> dict:
    """{"id", "code", "authUrl"}: send the user to authUrl in a browser."""
    r = requests.post(f"{PLEX_TV}/pins", headers=_plex_headers(client_id),
                      data={"strong": "true"}, timeout=TIMEOUT)
    r.raise_for_status()
    j = r.json()
    auth_url = (f"https://app.plex.tv/auth#?clientID={client_id}"
                f"&code={j['code']}"
                f"&context%5Bdevice%5D%5Bproduct%5D={PRODUCT}")
    return {"id": j["id"], "code": j["code"], "authUrl": auth_url}


def plex_check_pin(client_id: str, pin_id: int) -> str | None:
    """The account token once the PIN is claimed, else None (keep polling)."""
    r = requests.get(f"{PLEX_TV}/pins/{pin_id}",
                     headers=_plex_headers(client_id), timeout=TIMEOUT)
    r.raise_for_status()
    return r.json().get("authToken") or None


def plex_servers(client_id: str, token: str) -> list[dict]:
    """The account's servers with connection URIs, best-first per server
    (local non-relay > remote non-relay > relay; media should never cross a
    relay for indexing)."""
    r = requests.get(f"{PLEX_TV}/resources",
                     params={"includeHttps": "1", "includeRelay": "1"},
                     headers=_plex_headers(client_id, token), timeout=TIMEOUT)
    r.raise_for_status()
    out = []
    for res in r.json():
        if "server" not in (res.get("provides") or ""):
            continue
        conns = sorted(
            res.get("connections") or [],
            key=lambda c: (c.get("relay", False), not c.get("local", False)))
        out.append({
            "name": res.get("name"),
            "product": res.get("product"),
            "connections": [{"uri": c.get("uri"),
                             "local": bool(c.get("local")),
                             "relay": bool(c.get("relay"))} for c in conns],
        })
    return out


# --- Jellyfin --------------------------------------------------------------

def _jf_headers(client_id: str, token: str = "") -> dict:
    auth = (f'MediaBrowser Client="{PRODUCT}", Device="{PRODUCT}", '
            f'DeviceId="{client_id}", Version="{__version__}"')
    if token:
        auth += f', Token="{token}"'
    # Modern Jellyfin reads Authorization; older builds only the Emby header.
    return {"Authorization": auth, "X-Emby-Authorization": auth,
            "Accept": "application/json"}


def jf_quickconnect_enabled(origin: str, client_id: str) -> bool:
    r = requests.get(f"{origin.rstrip('/')}/QuickConnect/Enabled",
                     headers=_jf_headers(client_id), timeout=TIMEOUT)
    r.raise_for_status()
    return r.json() is True


def jf_quickconnect_initiate(origin: str, client_id: str) -> dict:
    """{"code", "secret"}: the user enters code in their Jellyfin UI
    (Settings → Quick Connect). GET on 10.8/10.9; newer builds want POST."""
    url = f"{origin.rstrip('/')}/QuickConnect/Initiate"
    r = requests.get(url, headers=_jf_headers(client_id), timeout=TIMEOUT)
    if r.status_code == 405:  # method moved to POST in newer Jellyfin
        r = requests.post(url, headers=_jf_headers(client_id), timeout=TIMEOUT)
    r.raise_for_status()
    j = r.json()
    return {"code": j["Code"], "secret": j["Secret"]}


def jf_quickconnect_claimed(origin: str, client_id: str, secret: str) -> bool:
    r = requests.get(f"{origin.rstrip('/')}/QuickConnect/Connect",
                     params={"secret": secret},
                     headers=_jf_headers(client_id), timeout=TIMEOUT)
    r.raise_for_status()
    return bool(r.json().get("Authenticated"))


def jf_quickconnect_exchange(origin: str, client_id: str, secret: str) -> dict:
    """{"token", "user_id"} once the code has been approved."""
    r = requests.post(
        f"{origin.rstrip('/')}/Users/AuthenticateWithQuickConnect",
        json={"Secret": secret}, headers=_jf_headers(client_id),
        timeout=TIMEOUT)
    r.raise_for_status()
    j = r.json()
    return {"token": j["AccessToken"], "user_id": (j.get("User") or {}).get("Id", "")}


def jf_password_auth(origin: str, client_id: str, username: str,
                     password: str) -> dict:
    """{"token", "user_id"}: fallback when Quick Connect is disabled."""
    r = requests.post(
        f"{origin.rstrip('/')}/Users/AuthenticateByName",
        json={"Username": username, "Pw": password},
        headers=_jf_headers(client_id), timeout=TIMEOUT)
    r.raise_for_status()
    j = r.json()
    return {"token": j["AccessToken"], "user_id": (j.get("User") or {}).get("Id", "")}
