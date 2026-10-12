FROM python:3.11.10-slim-bookworm

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# sudo and a real user are required by Workbench's post-startup.sh; procps
# provides pgrep for the MCP server's start script.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl git jq procps sudo zstd \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --shell /bin/bash --uid 1000 nemotron \
    && echo "nemotron ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/nemotron \
    && chmod 440 /etc/sudoers.d/nemotron

# Same pinned Ollama release as the opencode-nemotron-mcp app. The tarball
# bundles its CUDA runtime; Workbench injects the driver on GPU VMs.
COPY ollama-release.json /tmp/ollama-release.json
RUN version="$(jq -r .version /tmp/ollama-release.json)" \
    && sha256="$(jq -r .sha256 /tmp/ollama-release.json)" \
    && curl --retry 3 -fsSL "https://github.com/ollama/ollama/releases/download/v${version}/ollama-linux-amd64.tar.zst" -o /tmp/ollama.tar.zst \
    && printf '%s  %s\n' "${sha256}" /tmp/ollama.tar.zst | sha256sum -c - \
    && tar --zstd -xf /tmp/ollama.tar.zst -C /usr \
    && rm /tmp/ollama.tar.zst \
    && ollama --version

WORKDIR /app

# Installed into the image's own Python (/usr/local), separate from the conda
# environment the workbench-tools feature adds for the user.
COPY backend/requirements.txt backend/requirements.txt
RUN /usr/local/bin/pip install --no-cache-dir -r backend/requirements.txt

COPY backend/ backend/
COPY frontend/ frontend/
COPY tasks/ tasks/
COPY scripts/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

ENV APP_ROOT=/app
EXPOSE 8080

CMD ["/usr/local/bin/entrypoint.sh"]
