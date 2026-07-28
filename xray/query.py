"""Episode selectors in a search box: "smallville s1e1".

Plex matches queries against titles, so a selector typed into the search box
matches nothing at all: Smallville's episodes are called "Pilot" and
"Metamorphosis". Splitting the selector off first lets the show be found by
name and the episode be picked by number, which is how people actually refer
to television.

Parsing only. Resolving a (show, season, episode) to an item is the caller's
job, because that needs a media server and this does not.
"""
from __future__ import annotations

import re

#: Deliberately anchored to the END of the query. A leading match would
#: mangle titles that legitimately begin with something selector-shaped, and
#: the selector is a suffix in every form anyone types.
#:
#: Covers s1e1 / S01E01 / s1 e1 / 1x01 / s1 / season 2 episode 3. NOT bare
#: "1 1": too many real titles end in digits ("Se7en", "Blade Runner 2049")
#: and a wrong split silently searches for the wrong show.
_PATTERNS = (
    re.compile(r"^(?P<stem>.+?)[\s._-]+s(?:eason)?[\s._-]*(?P<season>\d{1,2})"
               r"[\s._-]*(?:e(?:pisode)?[\s._-]*(?P<episode>\d{1,3}))?$", re.I),
    re.compile(r"^(?P<stem>.+?)[\s._-]+(?P<season>\d{1,2})x(?P<episode>\d{1,3})$",
               re.I),
)


def split_selector(query: str) -> tuple[str, int | None, int | None]:
    """`("smallville s1e1")` -> `("smallville", 1, 1)`.

    Returns the query unchanged with two Nones when there is no selector, so
    a caller can always unpack the result and act on whether season is None.
    Season 0 is Specials and parses like any other, which is why the check
    downstream must be `is not None` and never truthiness.
    """
    text = (query or "").strip()
    if not text:
        return text, None, None
    for pattern in _PATTERNS:
        m = pattern.match(text)
        if not m:
            continue
        stem = m.group("stem").strip(" .-_")
        if not stem:
            break          # the whole query was a selector; nothing to search
        episode = m.group("episode")
        return stem, int(m.group("season")), (int(episode) if episode else None)
    return text, None, None


def pick(leaves, season: int, episode: int | None) -> list[dict]:
    """The leaves of one show narrowed to a season, and maybe one episode.

    `leaves` is whatever `MediaSource.series_leaves` returned. Numbers are
    compared as ints because backends report them inconsistently as ints or
    strings, and "1" == 1 is False in a way that silently returns nothing.
    """
    def num(leaf, key):
        try:
            return int(leaf.get(key))
        except (TypeError, ValueError):
            return None

    out = [lf for lf in leaves if num(lf, "season") == season]
    if episode is not None:
        out = [lf for lf in out if num(lf, "episode") == episode]
    return out
