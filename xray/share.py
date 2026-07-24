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


def export_timeline(store_dir: Path, key: str, out_dir: Path) -> Path:
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
    if not doc.get("sourceRuntimeMs"):
        print("[warn] no sourceRuntimeMs on this timeline; receivers can't "
              "detect cut mismatches (older timeline; re-index to fix)")
    doc["generator"] = {"name": "openxray", "version": __version__}
    doc.setdefault("provenance", {}).pop("people", None)

    st.validate(doc)  # a share file must itself be a valid timeline
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{content_id}{SHARE_SUFFIX}"
    st.atomic_write(out, doc)
    print(f"exported → {out}")
    return out


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


def upload_to_hub(store_dir: Path, key: str, hub_url: str,
                  token: str = "") -> dict:
    """Share-safe export + POST to the hub's /upload (pending review there).

    The export step strips licensed person data before anything leaves this
    machine; the hub strips again server-side (defense in depth)."""
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        out = export_timeline(store_dir, key, Path(tmp))
        headers = {"Content-Type": "application/json"}
        if token:
            headers["X-Upload-Token"] = token
        r = requests.post(f"{hub_url.rstrip('/')}/upload",
                          data=out.read_bytes(), headers=headers, timeout=60)
    r.raise_for_status()
    return r.json()


def fetch_from_hub(store_dir: Path, hub_url: str, content_id: str) -> Path | None:
    """Try the community hub for a timeline. None = not in the catalog."""
    url = f"{hub_url.rstrip('/')}/t/{content_id}.json"
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
