"""Download YuNet (detector) + SFace (embedder) ONNX weights from OpenCV Zoo.

Both are Apache-2.0, genuinely open weights (plan.md decision ledger). The
files are Git-LFS-tracked in opencv_zoo, so we fetch from the LFS media host,
a plain raw.githubusercontent URL would return a text pointer, not the model.

Run:  indexer/.venv/bin/python indexer/scripts/fetch_models.py
"""
from __future__ import annotations

import sys
import urllib.request
from pathlib import Path

LFS = "https://media.githubusercontent.com/media/opencv/opencv_zoo/main/models"
MODELS = {
    "face_detection_yunet_2023mar.onnx":
        f"{LFS}/face_detection_yunet/face_detection_yunet_2023mar.onnx",
    "face_recognition_sface_2021dec.onnx":
        f"{LFS}/face_recognition_sface/face_recognition_sface_2021dec.onnx",
}


def main() -> int:
    models_dir = Path(__file__).resolve().parents[1] / "models"
    models_dir.mkdir(exist_ok=True)
    for name, url in MODELS.items():
        dest = models_dir / name
        if dest.exists() and dest.stat().st_size > 100_000:
            print(f"have  {name} ({dest.stat().st_size} bytes)")
            continue
        print(f"fetch {name} …")
        urllib.request.urlretrieve(url, dest)
        head = dest.read_bytes()[:64]
        if head.startswith(b"version https://git-lfs"):
            print("ERROR: got an LFS pointer, not the model binary.",
                  file=sys.stderr)
            return 1
        print(f"  -> {dest} ({dest.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
