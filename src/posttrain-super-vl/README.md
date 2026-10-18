# Post-training Nemotron 3.5 Super VL (Stage B LoRA SFT)

Training container for LoRA SFT of `nvidia/NVIDIA-Nemotron-3.5-Super-VL-120B-A12B-BF16` on the workspace's own agent
trajectories (eval-bench `harness/export_sft.py`). Plan, memory estimates and gates: eval-bench
`docs/stageB_training_plan.md`.

- Image: `nvcr.io/nvidia/nemo-automodel:26.08`, pinned by digest. At first start the app overlays the pinned AutoModel
  commit (`AUTOMODEL_COMMIT`), because the Super VL recipes came after the 26.08 release. It also downloads the base
  weights (~249 GB, pinned HF revision) to the data disk.
- The app never starts training by itself. It serves a status page on :8080 (GPU use plus each job's state and
  step/loss lines) and polls `$GCS_PREFIX/jobs/` for job specs (`training/jobs/*.yaml`). Each job runs once.
  Logs, status and LoRA checkpoints go to `$GCS_PREFIX/runs/<name>/`, which is in the workspace bucket.
- Create on one `a2-highgpu-8g` with a large data disk:
  `wb app create gcp --config=posttrain-super-vl --machine-type=a2-highgpu-8g --accelerator-type=nvidia-tesla-a100 --accelerator-core-count=8 --data-disk-size=1500`.
  Use DWS flex-start the same way as `vllm-super-vl`.
- From the Jupyter VM:
  - `scripts/submit_job.sh data ~/eval-bench/scratch_train/sft` uploads the training data.
  - `scripts/submit_job.sh job training/jobs/g0_load_8k.yaml` queues a job. Run the gates in order: g0, g2, g3, then
    stageB_run1.
  - `scripts/submit_job.sh status g0-load-8k` shows progress; `scripts/submit_job.sh cancel <name>` stops a job.
- Manual use: `docker exec -it application-server bash`, then `python3 /opt/posttrain/scripts/run_job.py <spec.yaml>`.
- `training/test_sft_dataset.py` checks the data and labels against the real tokenizer and chat template (CPU).
  `training/merge_lora.py --check`, then without `--check`, merges an adapter into a full BF16 checkpoint for serving.
  Point `vllm-super-vl` at it by setting `MODEL_DIR`.
- Trained adapters and merged weights are controlled artifacts, because they were trained on Baseline data. Keep them
  on the data disk or in the workspace bucket. Never upload them to Hugging Face or any other external service.
- 8x A100 is about $30/hour. Stop the app when no job is running.
