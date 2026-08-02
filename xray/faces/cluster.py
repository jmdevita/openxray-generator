"""Cluster-then-label + temporal smoothing (plan.md §5.3, §6.3).

The standard trick: cluster ALL face embeddings in the title first
(unsupervised), THEN assign each cluster to the nearest cast reference. A
character in heavy makeup still clusters with themselves across the film, so one
good match anywhere labels every appearance.

Embeddings are L2-normalized upstream, so Euclidean distance is monotonic with
cosine, so we cluster in Euclidean space and score labels with cosine (a dot
product).
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from sklearn.cluster import HDBSCAN


def cluster_embeddings(embeddings, min_cluster_size=5, min_samples=None):
    """Return a per-embedding cluster label; -1 is noise."""
    X = np.asarray(embeddings, dtype=np.float32)
    clusterer = HDBSCAN(
        min_cluster_size=min_cluster_size,
        min_samples=min_samples,
        metric="euclidean",
    )
    return clusterer.fit_predict(X)


def cluster_centroids(embeddings, labels):
    """Normalized mean embedding per (non-noise) cluster."""
    X = np.asarray(embeddings, dtype=np.float32)
    centroids = {}
    for lab in sorted(set(int(l) for l in labels)):
        if lab == -1:
            continue
        mean = X[labels == lab].mean(axis=0)
        n = np.linalg.norm(mean)
        centroids[lab] = mean / n if n else mean
    return centroids


def label_clusters(centroids, refs, threshold=0.363):
    """Assign each cluster to the nearest cast reference above `threshold`.

    refs: {actorId: normalized_ref_vector}. `threshold` is cosine similarity;
    0.363 is SFace's default same-identity cosine threshold (plan.md says
    ~0.4-0.5 generally; tune on the spike titles).

    Returns {cluster_label: (actorId, cosine_sim)} for clusters that cleared
    the bar. Unlabeled clusters are background / uncredited faces.
    """
    if not refs:
        return {}
    ref_ids = list(refs.keys())
    ref_mat = np.stack([refs[a] for a in ref_ids])   # (A, D), each row normed
    out = {}
    for lab, c in centroids.items():
        sims = ref_mat @ c                            # cosine, both normalized
        j = int(np.argmax(sims))
        if float(sims[j]) >= threshold:
            out[lab] = (ref_ids[j], float(sims[j]))
    return out


@dataclass
class FaceHit:
    """One detected face: where in the media, and where in the frame.

    `bbox` is (x, y, w, h) in the frame's pixels, optional because the
    matching path never needed it. Naming a cluster does: showing a person
    the faces they are about to name means cropping them back out of the
    frame, and both engine paths already know the box at detection time.
    """
    frame_index: int
    timestamp_ms: int
    bbox: tuple | None = None


def build_intervals(hits, labels, cluster_to_actor, sample_fps,
                    min_run=2, merge_gap_frames=1):
    """Merge per-actor face presence into temporal intervals.

    hits[i] and labels[i] are aligned (one detected face each). We group by
    actor, sort by time, and merge samples that are within `merge_gap_frames`
    of each other; intervals shorter than `min_run` samples are dropped as
    blips (plan.md §5.3 temporal smoothing).
    """
    interval_ms = 1000.0 / sample_fps
    merge_gap_ms = interval_ms * (merge_gap_frames + 1) + 1

    # actor -> list of (timestamp_ms, cosine_sim)
    per_actor: dict[str, list[tuple[int, float]]] = {}
    for hit, lab in zip(hits, labels):
        assigned = cluster_to_actor.get(int(lab))
        if not assigned:
            continue
        actor_id, sim = assigned
        per_actor.setdefault(actor_id, []).append((hit.timestamp_ms, sim))

    intervals = []
    for actor_id, samples in per_actor.items():
        samples.sort()
        run_start = prev = samples[0][0]
        run_sims = [samples[0][1]]
        count = 1

        def flush(end_ts):
            if count >= min_run:
                intervals.append({
                    "actorId": actor_id,
                    "startMs": int(run_start),
                    "endMs": int(end_ts + interval_ms),  # sample covers its slot
                    "confidence": round(float(np.mean(run_sims)), 3),
                })

        for ts, sim in samples[1:]:
            if ts - prev <= merge_gap_ms:
                prev = ts
                run_sims.append(sim)
                count += 1
            else:
                flush(prev)
                run_start = prev = ts
                run_sims = [sim]
                count = 1
        flush(prev)

    intervals.sort(key=lambda iv: (iv["startMs"], iv["actorId"]))
    return intervals
