"""Run inaSpeechSegmenter on an audio file and emit music segments as JSON.

Runs inside the container. inaSpeechSegmenter labels every span of audio as
speech / music / noEnergy / noise; we keep the music spans.
"""
import json
import sys

from inaSpeechSegmenter import Segmenter

MARKER = "INA_JSON:"


def main() -> None:
    if len(sys.argv) < 2:
        print("usage: segment.py <audio_path>", file=sys.stderr)
        sys.exit(2)
    # 'smn' = speech / music / noise engine; no gender split needed here.
    seg = Segmenter(vad_engine="smn", detect_gender=False)
    segmentation = seg(sys.argv[1])
    out = [
        {"label": label, "start": float(start), "end": float(end)}
        for label, start, end in segmentation
    ]
    print(MARKER + json.dumps(out))


if __name__ == "__main__":
    main()
