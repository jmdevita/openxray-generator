"""Share-safe export / import (unification plan U3).

A shared timeline is a NORMAL timeline (same schema) minus the licensed
material: TMDb person data (`cast[].person`) and TMDb-hosted thumb URLs are
stripped; recipients rehydrate with their own key via the people pass.
`sourceRuntimeMs` rides along so receiving clients can warn when their copy
of the media is a different cut; `generator` makes bad batches traceable.
"""
from __future__ import annotations

import json
from pathlib import Path

import requests

from . import __version__, store as st

SHARE_SUFFIX = ".xray.json"
#: A bundle is JSON Lines: one share-safe timeline per line, no header, so
#: every line is independently a valid timeline. It exists because a hub rate
#: limits per REQUEST — sharing a library one file at a time burns an hour's
#: allowance on the first ten titles.
BUNDLE_SUFFIX = ".xray.jsonl"
#: Kept under the hub's own ceilings (25MB / 500 items) with room to spare,
#: since a hub reads the whole body into memory. Bigger libraries chunk.
BUNDLE_MAX_BYTES = 20 * 1024 * 1024
BUNDLE_MAX_ITEMS = 400


def share_doc(store_dir: Path, key: str, *, warn: bool = True) -> tuple[str, dict]:
    """One stored timeline as a share-safe document. Returns (content_id, doc).

    The single sole place stripping happens, so a file export and a bundle line
    can never disagree about what leaves the machine.
    """
    files = st.resolve_timelines(store_dir, [key])
    doc = json.loads(files[0].read_text())
    content_id = doc.get("contentId") or files[0].stem
    if not str(content_id).startswith("tmdb-"):
        raise SystemExit(f"only content-keyed timelines are shareable "
                         f"(got {content_id!r})")

    # Strip licensed material: recipients re-enrich with their own keys.
    for c in doc.get("cast") or []:
        c.pop("person", None)
        c.pop("thumb", None)
    if warn and not doc.get("sourceRuntimeMs"):
        print("[warn] no sourceRuntimeMs on this timeline; receivers can't "
              "detect cut mismatches (older timeline; re-index to fix)")
    doc["generator"] = {"name": "openxray", "version": __version__}
    doc.setdefault("provenance", {}).pop("people", None)

    st.validate(doc)  # a share file must itself be a valid timeline
    return str(content_id), doc


def export_timeline(store_dir: Path, key: str, out_dir: Path) -> Path:
    content_id, doc = share_doc(store_dir, key)
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{content_id}{SHARE_SUFFIX}"
    st.atomic_write(out, doc)
    print(f"exported → {out}")
    return out


def export_bundle(store_dir: Path, keys, out_dir: Path, *,
                  name: str = "bundle",
                  max_bytes: int = BUNDLE_MAX_BYTES,
                  max_items: int = BUNDLE_MAX_ITEMS) -> list[Path]:
    """Share-safe JSON Lines bundle(s) for `keys`. Returns the files written.

    Chunks rather than writing one enormous file: a hub caps both body size and
    item count, and a library can exceed either. Each chunk is a complete,
    independently uploadable bundle.

    A key that cannot be shared is reported and skipped — one unshareable title
    should not cost the caller the other nine hundred.
    """
    keys = list(keys)  # counted and iterated once; a generator would vanish
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    skipped: list[str] = []
    lines: list[str] = []
    size = 0

    def flush() -> None:
        """Always numbered; a lone chunk is renamed at the end."""
        nonlocal lines, size
        if not lines:
            return
        out = out_dir / f"{name}-{len(written) + 1}{BUNDLE_SUFFIX}"
        out.write_text("".join(lines), encoding="utf-8")
        written.append(out)
        lines, size = [], 0

    for key in keys:
        try:
            _, doc = share_doc(store_dir, key, warn=False)
        except SystemExit as e:
            skipped.append(f"{key}: {e}")
            continue
        # Compact separators: one line per timeline, and a library's worth of
        # pretty-printing is real bytes over the wire for no reader's benefit.
        line = json.dumps(doc, ensure_ascii=False, separators=(",", ":")) + "\n"
        encoded = len(line.encode("utf-8"))
        if lines and (size + encoded > max_bytes or len(lines) >= max_items):
            flush()
        # A single timeline larger than max_bytes still goes out alone rather
        # than being silently dropped; the hub will say so if it is too big.
        lines.append(line)
        size += encoded
    flush()

    # Renaming after the fact: a single chunk should not be called "bundle-1".
    if len(written) == 1:
        final = out_dir / f"{name}{BUNDLE_SUFFIX}"
        written[0].replace(final)
        written = [final]
    for note in skipped:
        print(f"[skip] {note}")
    shared = len(keys) - len(skipped)
    print(f"bundled {shared} timeline(s) into {len(written)} file(s)"
          + (f"; {len(skipped)} skipped" if skipped else ""))
    return written


def read_bundle(path: Path) -> list[dict]:
    """Parse a bundle back into documents, naming the line that broke.

    The hub does its own parsing; this is for local round-tripping and tests.
    """
    docs = []
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            doc = json.loads(line)
        except json.JSONDecodeError as e:
            raise SystemExit(f"{path.name} line {n}: {e}")
        if not isinstance(doc, dict):
            raise SystemExit(f"{path.name} line {n}: not a JSON object")
        docs.append(doc)
    return docs


def import_timeline(store_dir: Path, src: str, *, force: bool = False) -> Path:
    """Import a shared timeline from a local path or URL into the store.

    NOTE: the manifest gets no lookup entry here (server item ids are local
    knowledge): run the index/map passes, or Plezy's legacy-name fallback
    won't find it until mapped."""
    if src.startswith(("http://", "https://")):
        r = requests.get(src, timeout=30)
        r.raise_for_status()
        doc = r.json()
        src_name = src.rsplit("/", 1)[-1]
    else:
        p = Path(src).expanduser()
        doc = json.loads(p.read_text())
        src_name = p.name

    st.validate(doc)
    content_id = doc.get("contentId")
    if not content_id or not str(content_id).startswith("tmdb-"):
        raise SystemExit("import rejected: no content-keyed contentId in the file")
    expected = f"{content_id}{SHARE_SUFFIX}"
    if src_name not in (expected, f"{content_id}.json"):
        print(f"[warn] filename {src_name!r} doesn't match contentId "
              f"{content_id!r}; trusting the file's contentId")
    dest = st.canonical_path(store_dir, content_id)
    if dest.exists() and not force:
        raise SystemExit(f"{dest.name} already exists; pass --force to replace")
    place_shared_doc(store_dir, doc)
    print(f"imported → {dest.name}  (run `enrich people {content_id}` to "
          f"rehydrate bios; map a server key for Plezy lookup)")
    return dest


def place_shared_doc(store_dir: Path, doc: dict) -> Path:
    """Sanitize-and-write a third-party timeline doc into the store.

    Share-safety on the way IN: never accept third-party TMDb person data:
    strip it and let the local people pass rehydrate under the user's key."""
    stripped = 0
    for c in doc.get("cast") or []:
        if c.pop("person", None) is not None:
            stripped += 1
    if stripped:
        (doc.get("provenance") or {}).pop("people", None)
        print(f"[share] stripped person data from {stripped} cast entries")
    st.validate(doc)
    dest = st.canonical_path(store_dir, doc["contentId"])
    st.write_timeline(dest, doc)
    return dest


def upload_token() -> str:
    """The hub write credential, if this machine has one.

    A public hub gates writes behind a bot check in the browser, so the tooling
    path needs a token that hub issues. There is no issuance flow yet, which is
    why the dashboard offers a file to upload rather than pretending to send
    one. An operator seeding their own hub can set this and use the direct
    path."""
    import os
    return os.environ.get("XRAY_HUB_UPLOAD_TOKEN", "").strip()


def upload_to_hub(store_dir: Path, key: str, hub_url: str,
                  token: str = "") -> dict:
    """Share-safe export + POST to the hub's /upload (pending review there).

    The export step strips licensed person data before anything leaves this
    machine; the hub strips again server-side (defense in depth).

    Needs a write credential: without one a public hub answers 403, so callers
    should check `upload_token()` before offering this as an action."""
    import tempfile
    token = token or upload_token()
    with tempfile.TemporaryDirectory() as tmp:
        out = export_timeline(store_dir, key, Path(tmp))
        headers = {"Content-Type": "application/json"}
        if token:
            headers["X-Upload-Token"] = token
        r = requests.post(f"{hub_url.rstrip('/')}/upload",
                          data=out.read_bytes(), headers=headers, timeout=60)
    r.raise_for_status()
    return r.json()


def upload_bundle(path: Path, hub_url: str, token: str = "") -> dict:
    """POST one bundle file. Answers with the hub's per-item summary.

    A bundle is one request against the hub's rate limit however many timelines
    it carries, which is the entire reason the format exists. A 200 here does
    not mean every line was accepted: read `counts`."""
    token = token or upload_token()
    headers = {"Content-Type": "application/x-ndjson"}
    if token:
        headers["X-Upload-Token"] = token
    r = requests.post(f"{hub_url.rstrip('/')}/upload",
                      data=path.read_bytes(), headers=headers, timeout=300)
    r.raise_for_status()
    return r.json()


def hub_catalog(hub_url: str, timeout: int = 15) -> dict[str, dict] | None:
    """The hub's whole catalog as {contentId: entry}, for planning a run.

    One request answers "which of my 400 titles has somebody already done?",
    which is the difference between an estimate and a guess. Entries carry a
    `units` list, so a caller can tell a full index on the hub from a seed
    and count only the ones that would actually save it work.

    Tri-state on purpose: None means the hub could not be asked (down,
    misconfigured, garbage response), while {} means it answered and holds
    nothing. Collapsing those into one value would let a planner report "no
    community coverage" when the truth is "no idea"."""
    try:
        r = requests.get(f"{hub_url.rstrip('/')}/index.json", timeout=timeout)
        r.raise_for_status()
        entries = r.json().get("catalog") or []
    except (requests.RequestException, ValueError) as e:
        print(f"[hub] catalog unavailable ({e}); planning without it")
        return None
    return {e["contentId"]: e for e in entries if e.get("contentId")}


def timelines_base(hub_url: str, timeout: int = 15) -> str:
    """Where this hub says its timelines live.

    Manifest-first, the same contract Plezy follows: the hub publishes a
    `timelines` base in index.json and points it at whatever is actually
    serving bytes (a CDN in front of a bucket, in production). Asking the
    hub host directly would work, but it would drag every download through
    the origin and quietly undo the read/write split.

    Falls back to the hub's own /t on any failure, which is what a hub with
    no bucket configured serves anyway."""
    fallback = f"{hub_url.rstrip('/')}/t"
    try:
        r = requests.get(f"{hub_url.rstrip('/')}/index.json", timeout=timeout)
        r.raise_for_status()
        return (r.json().get("timelines") or fallback).rstrip("/")
    except (requests.RequestException, ValueError):
        return fallback


def fetch_from_hub(store_dir: Path, hub_url: str, content_id: str) -> Path | None:
    """Try the community hub for a timeline. None = not in the catalog."""
    url = f"{timelines_base(hub_url)}/{content_id}.json"
    try:
        r = requests.get(url, timeout=30)
    except requests.RequestException as e:
        print(f"[hub] unreachable ({e}); continuing without it")
        return None
    if r.status_code == 404:
        return None
    r.raise_for_status()
    doc = r.json()
    if doc.get("contentId") != content_id:
        print(f"[hub] refused: catalog file claims {doc.get('contentId')!r}")
        return None
    dest = place_shared_doc(store_dir, doc)
    print(f"[hub] fetched {content_id} from the hub")
    return dest
