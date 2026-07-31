# syntax=docker/dockerfile:1.7
# engine-speakers: diarization (pyannote) behind an HTTP seam.
#
# Deliberately NOT part of the orchestrator image. torch is ~200 MB even in
# the CPU-only build and most libraries are live action, where this never
# runs. The compose service carries `profiles: ["speakers"]` so a default
# `docker compose up` does not pull any of it.
#
# CPU torch by DEFAULT: the CUDA wheels are roughly ten times the size, and
# Docker Desktop on macOS exposes no GPU at all (no MPS passthrough), so on
# the machine most people run this the CUDA build buys nothing.
#
# On a CUDA host, the SAME Dockerfile builds the GPU variant -- the immich
# pattern: one parameterized build, backend picked by an ARG.
#
#   SPEAKERS_TORCH_INDEX=https://download.pytorch.org/whl/cu124 \
#     docker compose -f docker-compose.yml -f gpu.yml --profile speakers up -d --build
#
# No nvidia/cuda base image is needed: pip's CUDA torch wheels bundle the
# CUDA runtime libraries, so python:slim serves both variants and only the
# host driver + nvidia-container-toolkit matter (granted at RUNTIME by the
# device reservation in gpu.yml, never at build time). Diarization measures
# ~2.6x realtime on 8 CPU threads and roughly 20-40x on a modest GPU.
FROM python:3.12-slim

WORKDIR /app

# ffmpeg: pyannote reads audio through torchaudio, which shells out for
# anything that is not plain PCM.
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# torchcodec is listed HERE, from the torch index, on purpose. pyannote 4.x
# depends on it for audio decoding, and installing it transitively in the next
# step resolves against PyPI, which serves the CUDA-linked wheel: in a CPU
# image it then fails to load with `libnvrtc.so.13: cannot open shared
# object file`, and every diarize dies with "torchcodec is not available".
# Naming it before pyannote means that install finds it already satisfied,
# and taking it from ${TORCH_INDEX} keeps the flavor consistent with torch.
ARG TORCH_INDEX=https://download.pytorch.org/whl/cpu
RUN pip install --no-cache-dir --index-url ${TORCH_INDEX} \
        torch torchaudio torchcodec
RUN pip install --no-cache-dir \
        "pyannote.audio>=4.0" fastapi uvicorn numpy

COPY xray/ xray/
COPY schema/ schema/
COPY scripts/fetch_speaker_models.py scripts/

# OPTIONALLY bake the weights in. With HF_TOKEN mounted this produces a
# self-contained image that needs no credential and reaches nothing at
# runtime, which is what an air-gapped host wants. WITHOUT it the step prints
# a note and succeeds: the image is still usable, and the weights are fetched
# on request once someone saves a token in the dashboard.
#
# Optional on purpose. Requiring the token here made a failed `docker build`
# the place people first met the three HuggingFace gates -- the worst possible
# place for it, since a build cannot say which gate is missing or link it. The
# dashboard can, so that is the default path and this is the opt-in one.
#
# The token arrives as a BuildKit secret, not a build ARG: an ARG is written
# into image history in plain text and is readable by anyone who has the
# image, whereas a secret mount exists only for this RUN and never lands in a
# layer. Verify with `docker history` -- the token appears nowhere.
#
# HF_HOME is set for THIS RUN only. Leaving it in the environment would point
# the running container at the baked cache unconditionally, and a container
# whose image was built without the bake needs to write to the shared volume
# instead. engine_speakers._cache() picks between them by looking for weights.
RUN --mount=type=secret,id=hf_token \
    HF_HOME=/opt/hf \
    HF_TOKEN="$(cat /run/secrets/hf_token 2>/dev/null || true)" \
    python scripts/fetch_speaker_models.py

# No HF_HUB_OFFLINE here, deliberately. The server process sets it itself
# before importing huggingface_hub, so loading stays offline either way; a
# baked-in ENV would also gag the fetch subprocess, which is the one thing in
# this image that is supposed to reach the network.
ENV PYTHONUNBUFFERED=1

EXPOSE 8083
CMD ["uvicorn", "xray.service.engine_speakers:app", \
     "--host", "0.0.0.0", "--port", "8083"]
