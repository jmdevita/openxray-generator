"""One HTTP client for Wikidata, Wikipedia and Commons.

Wikimedia requires a descriptive User-Agent and rate-limits bursts with 429.
Three call sites had each grown their own copy of both; a swallowed 429 looks
exactly like "this actor has no photo", which once cost half a cast.
"""
from __future__ import annotations

import time

import requests

from .. import __version__

#: Wikimedia asks for identification, not a browser string. `purpose` says
#: which part of the project is calling, so their logs can tell our traffic
#: apart when one of these misbehaves.
UA = "plex-xray/{v} (github.com/plex-xray) {purpose}"


def user_agent(purpose: str) -> str:
    return UA.format(v=__version__, purpose=purpose)


def session(purpose: str) -> requests.Session:
    """A Session (so connections are reused) carrying the required UA."""
    s = requests.Session()
    s.headers.update({"User-Agent": user_agent(purpose)})
    return s


def get(sess, url, *, params=None, timeout: float = 20.0,
        retries: int = 3) -> requests.Response:
    """GET, waiting out 429s. Raises for other error statuses.

    `Retry-After` is honored when present and capped: the header is advice
    from a busy server, not a licence to hang a pass for an hour.
    """
    for attempt in range(retries + 1):
        r = sess.get(url, params=params, timeout=timeout)
        if r.status_code == 429 and attempt < retries:
            try:
                wait = float(r.headers.get("Retry-After", ""))
            except ValueError:
                wait = 2.0 ** attempt
            time.sleep(min(wait, 30.0))
            continue
        r.raise_for_status()
        return r


def get_json(sess, url, *, timeout: float = 20.0, retries: int = 3, **params):
    """`get` for the MediaWiki APIs, which all speak format=json."""
    params.setdefault("format", "json")
    return get(sess, url, params=params, timeout=timeout,
               retries=retries).json()
