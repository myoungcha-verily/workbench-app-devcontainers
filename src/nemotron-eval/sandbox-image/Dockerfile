# Workload image for OpenShell sandboxes (governed run_shell / run_python).
# Packages are baked in: the sandbox policy allows no package downloads.
FROM python:3.11.10-slim-bookworm

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl iproute2 jq \
    && rm -rf /var/lib/apt/lists/*

COPY sandbox-image/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt && rm /tmp/requirements.txt

COPY backend/sandbox_helper/workbench_data.py /usr/local/lib/python3.11/site-packages/workbench_data.py

RUN useradd --create-home --uid 1500 --shell /bin/bash sandbox \
    && mkdir -p /sandbox && chown sandbox:sandbox /sandbox
USER sandbox
WORKDIR /sandbox
