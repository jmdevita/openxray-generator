FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg libsndfile1 \
    && rm -rf /var/lib/apt/lists/*

# Pin to the stack inaSpeechSegmenter was written for. TF >= 2.16 ships Keras 3
# and numpy >= 2 dropped numpy.lib.pad, both break the segmenter. TF 2.15 +
# Keras 2 + numpy 1.23 is the known-good combination.
RUN pip install --no-cache-dir \
        "numpy==1.23.5" "tensorflow==2.15.1" "keras<3" inaSpeechSegmenter

COPY segment.py /app/segment.py
WORKDIR /app
