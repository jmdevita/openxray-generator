FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir "numpy" "opencv-python-headless>=4.9" fastapi uvicorn requests jsonschema scikit-learn
COPY xray/ xray/
COPY schema/ schema/
COPY scripts/fetch_models.py scripts/
RUN python scripts/fetch_models.py
ENV PYTHONUNBUFFERED=1
EXPOSE 8081
CMD ["uvicorn", "xray.service.engine_faces:app", "--host", "0.0.0.0", "--port", "8081"]
