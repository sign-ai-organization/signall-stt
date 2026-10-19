FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
        libsndfile1 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Ubicación de las libs CUDA instaladas vía pip (nvidia-cublas-cu12, nvidia-cudnn-cu12),
# necesarias para que ctranslate2 encuentre cuBLAS/cuDNN en tiempo de ejecución.
ENV LD_LIBRARY_PATH="/usr/local/lib/python3.11/site-packages/nvidia/cublas/lib:/usr/local/lib/python3.11/site-packages/nvidia/cudnn/lib"

COPY app ./app
# `scripts/` no va en la imagen a propósito: hay utilidades de escritorio que
# necesitan Piper y `sounddevice`, que el servicio no tiene. Las pruebas se
# corren montando la carpeta desde el host (ver README, "Pruebas").

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
