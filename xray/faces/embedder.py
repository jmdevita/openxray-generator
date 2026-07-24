"""The swappable embedder interface (plan.md §6.3) + the SFace/YuNet backend.

    detect(frame_bgr)      -> [Detection]           # YuNet
    embed(frame_bgr, det)  -> np.ndarray (L2-normed) # SFace, 128-d

Keeping the CV behind this interface is the load-bearing design choice from
xray-face-finetuning.md §1: buffalo_l or a future *-film model drops in as
another backend with no downstream change. Every embedding is tagged with
`model_version` so vectors from different models are never compared.
"""
from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np


@dataclass
class Detection:
    bbox: tuple          # (x, y, w, h)
    score: float
    landmarks: np.ndarray  # the full 15-value YuNet row, needed for alignCrop


class Embedder:
    model_version = "base"

    def detect(self, frame_bgr) -> list[Detection]:
        raise NotImplementedError

    def embed(self, frame_bgr, det: Detection) -> np.ndarray:
        raise NotImplementedError


class SFaceEmbedder(Embedder):
    """YuNet detection + SFace 128-d recognition, both via OpenCV."""

    model_version = "sface-v1"

    def __init__(self, yunet_path, sface_path, score_threshold=0.6,
                 nms_threshold=0.3, top_k=5000):
        self._detector = cv2.FaceDetectorYN.create(
            str(yunet_path), "", (320, 320),
            score_threshold, nms_threshold, top_k,
        )
        self._recognizer = cv2.FaceRecognizerSF.create(str(sface_path), "")

    def detect(self, frame_bgr) -> list[Detection]:
        h, w = frame_bgr.shape[:2]
        self._detector.setInputSize((w, h))
        _, faces = self._detector.detect(frame_bgr)
        dets: list[Detection] = []
        if faces is not None:
            for row in faces:
                x, y, bw, bh = row[:4]
                dets.append(Detection(
                    bbox=(float(x), float(y), float(bw), float(bh)),
                    score=float(row[-1]),
                    landmarks=row,
                ))
        return dets

    def embed(self, frame_bgr, det: Detection) -> np.ndarray:
        aligned = self._recognizer.alignCrop(frame_bgr, det.landmarks)
        feat = self._recognizer.feature(aligned)          # shape (1, 128)
        v = np.asarray(feat, dtype=np.float32).ravel()
        n = np.linalg.norm(v)
        return v / n if n else v                          # L2-normalized
