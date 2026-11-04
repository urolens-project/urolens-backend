# syntax=docker/dockerfile:1
# UroLens backend: web + mobile API and in-process AI inference (urolens-ai-engine).

FROM python:3.13-slim AS builder

RUN apt-get update \
    && apt-get install -y --no-install-recommends git git-lfs \
    && git lfs install --system \
    && rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH" PIP_NO_CACHE_DIR=1

# CPU-only torch first, so pip never resolves the multi-GB CUDA wheels.
RUN pip install torch torchvision --index-url https://download.pytorch.org/whl/cpu

# Drop dev-only tooling from the runtime image.
COPY requirements.txt .
RUN grep -viE '^(pytest|ruff|pre-commit)' requirements.txt > requirements.runtime.txt \
    && pip install -r requirements.runtime.txt

FROM python:3.13-slim AS runtime

# libgl1 / libglib2.0-0: opencv (pulled in by ultralytics) loads them at import.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 urolens

COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    YOLO_CONFIG_DIR=/tmp/Ultralytics \
    OMP_NUM_THREADS=1 \
    MKL_NUM_THREADS=1
# TORCH_NUM_THREADS (default 1) is read by the engine preload; raise it with the CPU count.

WORKDIR /app
COPY --chown=urolens:urolens main.py alembic.ini ./
COPY --chown=urolens:urolens src ./src
COPY --chown=urolens:urolens alembic ./alembic

USER urolens
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD python -c "import urllib.request as u; u.urlopen('http://127.0.0.1:8000/', timeout=3)"

# One worker: the model is held in memory per process.
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
