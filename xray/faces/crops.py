"""Exemplar face crops: what a person actually looks at when naming a cluster.

Kept out of the store module on purpose -- that one is pure bookkeeping, this
one needs OpenCV and the decoded frames, which exist only while the pass is
running. Frames are deleted with the work directory, so the crops have to be
cut before it goes.

Three exemplars per cluster, drawn from WIDELY separated points in the
runtime rather than consecutive samples. Consecutive faces look alike even
when clustering has merged two people; start/middle/end makes an impure
cluster visible as the face changing partway along the row, which is the only
purity check a person can actually perform at a glance. (It is the same
argument the speaker audition clips are built on, for the same reason.)
"""
from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np

#: Face box padded by this much of its longer side, so the crop carries hair
#: and jaw. A tight box on the features alone is oddly hard to recognise.
PAD = 0.25

TILE = 128
EXEMPLARS = 3


def _tile(frame_path: str, bbox) -> np.ndarray | None:
    img = cv2.imread(str(frame_path))
    if img is None or not bbox:
        return None
    x, y, w, h = (int(v) for v in bbox)
    pad = int(PAD * max(w, h))
    crop = img[max(0, y - pad):y + h + pad, max(0, x - pad):x + w + pad]
    return cv2.resize(crop, (TILE, TILE)) if crop.size else None


def _spread(samples, n=EXEMPLARS):
    """`n` samples spanning the cluster's life, not its first `n`."""
    ordered = sorted(samples, key=lambda s: s.get("ms") or 0)
    if len(ordered) <= n:
        return ordered
    return [ordered[round(i * (len(ordered) - 1) / (n - 1))] for i in range(n)]


def write_crops(doc: dict, frames, out_dir: Path,
                exemplars: int = EXEMPLARS) -> int:
    """One montage per cluster in `doc`. Returns how many were written.

    `frames` is the pass's extraction record; crops are addressed by frame
    INDEX because that is the only id shared by both engine paths (the
    service knows filenames, the pass knows media time).
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    path_by_index = {fr.index: fr.path for fr in frames}
    written = 0
    for cluster in doc.get("clusters") or []:
        key = str(cluster["cluster"])
        samples = (doc.get("samples") or {}).get(key) or []
        tiles = []
        for s in _spread(samples, exemplars):
            path = path_by_index.get(s.get("frame"))
            tile = _tile(path, s.get("bbox")) if path else None
            if tile is not None:
                tiles.append(tile)
        if tiles:
            cv2.imwrite(str(out_dir / f"{key}.jpg"), np.hstack(tiles))
            written += 1
    return written
