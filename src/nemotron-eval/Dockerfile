FROM nvidia/cuda:12.4-devel-ubuntu22.04

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    OLLAMA_HOST=0.0.0.0:11434 \
    OLLAMA_MODELS=/models/ollama

RUN apt-get update && apt-get install -y --no-install-recommends \
    python3.11 python3.11-venv python3-pip python3.11-dev \
    git curl ca-certificates build-essential \
    && rm -rf /var/lib/apt/lists/*

RUN curl -fsSL https://ollama.ai/install.sh | sh

RUN python3.11 -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

WORKDIR /app

COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

RUN mkdir -p /models/ollama /models/gguf

ARG GCS_MODELS_BUCKET=gs://cloned-models-wb-meteoric-gourd-5139
RUN gcloud storage cp -r ${GCS_MODELS_BUCKET}/* /models/gguf/ 2>/dev/null || true

RUN for f in /models/gguf/*.gguf; do \
      [ -f "$f" ] || continue; \
      base=$(basename "$f" .gguf); \
      echo "FROM $f" > /models/gguf/Modelfile.$base; \
    done

COPY .devcontainer/setup.sh /app/.devcontainer/setup.sh
RUN chmod +x /app/.devcontainer/setup.sh

EXPOSE 8080 11434

CMD ["/app/.devcontainer/entrypoint.sh"]