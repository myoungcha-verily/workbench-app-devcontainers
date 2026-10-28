# Kumo Relational NIM (Workbench custom app)

Runs NVIDIA's Kumo Relational NIM (`nvcr.io/nim/nvidia/kumo-relational`) on a GPU VM, behind a token-checking proxy on
port 8080. Supported GPUs per NGC: A10G, L4, L40S, H100 (an L4 `g2-standard-*` VM is the cheapest fit on GCP).

Before creating the app, put two secrets in the workspace bucket (never in this repo):

- `gs://myoung-bucket-wb-fleeting-lemon-7624/kumo_zus/secrets/ngc_api_key`: an NGC API key whose account has accepted the
  NIM's terms (non-commercial research/evaluation license).
- `gs://myoung-bucket-wb-fleeting-lemon-7624/kumo_zus/secrets/kumo_nim_token`: a random token clients send as `X-API-Key`.

The launcher mounts the host Docker socket to run the NIM as a sibling container (no published port; reachable only via
the proxy). Check `wb app logs` for `[kumo-nim]` lines; `GET /proxy-health` (no token) reports the NIM's readiness.
