# vLLM Nemotron 3.5 Super VL

Token-protected, OpenAI-compatible vLLM server for `nvidia/NVIDIA-Nemotron-3.5-Super-VL-120B-A12B-BF16`, used by the
eval-bench harness (agent runs and post-training data generation) inside the workspace.

- Create on an 8-GPU machine, e.g. `wb app create gcp --config=vllm-super-vl --machine-type=a2-highgpu-8g
  --accelerator-type=nvidia-tesla-a100 --accelerator-core-count=8 --data-disk-size=800`.
- First start downloads ~250 GB of weights to the data disk (`/data`), then serves on port 8080 (`/v1/...`) with model
  name `nemotron-3.5-super-vl`. Clients send `Authorization: Bearer <token>`; the token is read from
  `API_TOKEN_GCS` at start, and the server refuses to start without it.
- A100 40GB is not an NVIDIA-listed target; context, concurrency and speculation settings are environment variables in
  `docker-compose.yaml`.
- The GPUs cost roughly $30/hour: stop the app when idle.
