FROM python:3.11.10-slim-bookworm

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Workbench's post-startup.sh needs sudo, a real user, and Java on PATH
# (install-java.sh links `which java`; the wb CLI runs on Java 17). procps
# provides pgrep for the MCP server's start script.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl git jq openjdk-17-jre-headless procps sudo zstd \
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

# OpenShell CLI for governed chats (sandboxes run on the gateway, not here).
ARG OPENSHELL_VERSION=0.1.4
ARG OPENSHELL_SHA256=2b65178e62e1ece1d90d482555e9f1f98ff0296e058409b33418104b6333df07
RUN curl --retry 3 -fsSL "https://github.com/NVIDIA/OpenShell/releases/download/v${OPENSHELL_VERSION}/openshell-x86_64-unknown-linux-musl.tar.gz" -o /tmp/openshell.tar.gz \
    && printf '%s  %s\n' "${OPENSHELL_SHA256}" /tmp/openshell.tar.gz | sha256sum -c - \
    && tar -xzf /tmp/openshell.tar.gz -C /usr/local/bin openshell \
    && rm /tmp/openshell.tar.gz \
    && openshell --version

# Python for ungoverned run_python, with the same analysis packages as the
# sandbox image plus direct BigQuery access.
COPY sandbox-image/requirements.txt /tmp/agent-requirements.txt
RUN /usr/local/bin/python -m venv /opt/agent-python \
    && /opt/agent-python/bin/pip install --no-cache-dir -r /tmp/agent-requirements.txt \
        google-cloud-bigquery==3.25.0 db-dtypes==1.3.0 \
    && rm /tmp/agent-requirements.txt

WORKDIR /app

# Installed into the image's own Python (/usr/local), separate from the conda
# environment the workbench-tools feature adds for the user.
COPY backend/requirements.txt backend/requirements.txt
RUN /usr/local/bin/pip install --no-cache-dir -r backend/requirements.txt

COPY backend/ backend/
COPY frontend/ frontend/
COPY tasks/ tasks/
COPY openshell/ openshell/
COPY redteam/ redteam/
COPY scripts/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

ENV APP_ROOT=/app
EXPOSE 8080

CMD ["/usr/local/bin/entrypoint.sh"]
