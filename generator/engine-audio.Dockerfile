FROM python:3.11-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg libsndfile1 \
    && rm -rf /var/lib/apt/lists/*
# TF 2.15 + Keras 2 + numpy<2: the known-good inaSpeechSegmenter stack
RUN pip install --no-cache-dir "numpy==1.23.5" "tensorflow==2.15.1" "keras<3" \
    inaSpeechSegmenter fastapi uvicorn pydantic
WORKDIR /app
COPY xray/service/engine_audio.py xray/service/engine_audio.py
RUN touch xray/__init__.py xray/service/__init__.py
ENV PYTHONUNBUFFERED=1
EXPOSE 8082
CMD ["uvicorn", "xray.service.engine_audio:app", "--host", "0.0.0.0", "--port", "8082"]
