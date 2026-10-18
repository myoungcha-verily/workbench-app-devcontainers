#!/usr/bin/env python3
"""Run one training job spec (YAML) inside the container and mirror its state to the workspace bucket.

Job spec fields:
  name:      run name (also the GCS folder $GCS_PREFIX/runs/<name>/)
  config:    AutoModel YAML, relative to /workspace/training/configs (or absolute)
  data:      gs:// prefix holding train*.jsonl / val.jsonl from export_sft.py (default $GCS_PREFIX/sft/latest/)
  nproc:     GPUs (default: all)
  overrides: list of AutoModel CLI overrides, e.g. ["--step_scheduler.max_steps", "2"]
  upload_every_min: how often status + log go to GCS (default 10)
  upload_checkpoints: true -> copy the run's checkpoint dir (LoRA adapters, optimizer) to GCS at the end (default true)
Cancel a running job by uploading $GCS_PREFIX/jobs/<name>.cancel.
"""
import json
import os
import pathlib
import re
import signal
import subprocess
import sys
import threading
import time

import yaml

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gcs  # noqa: E402

PREFIX = os.environ["GCS_PREFIX"].rstrip("/")
STEP = re.compile(r"\bstep\s+\d+|val_loss|Traceback|Error|OutOfMemory|CUDA out of memory", re.I)


def wait_for(path, what):
    while not pathlib.Path(path).exists():
        print(f"waiting for {what} ({path})", flush=True)
        time.sleep(60)


def main():
    spec = yaml.safe_load(open(sys.argv[1]))
    name = spec["name"]
    run = pathlib.Path("/data/runs") / name
    run.mkdir(parents=True, exist_ok=True)
    remote = f"{PREFIX}/runs/{name}"
    status = {"name": name, "state": "preparing", "started": time.strftime("%FT%TZ", time.gmtime()), "spec": spec}

    def push(final=False):
        (run / "status.json").write_text(json.dumps(status, indent=1))
        gcs.put(str(run / "status.json"), f"{remote}/status.json")
        if (run / "train.log").exists():
            lines = [l for l in open(run / "train.log", errors="replace") if STEP.search(l)]
            (run / "progress.log").write_text("".join(lines[-2000:]))
            gcs.put(str(run / "progress.log"), f"{remote}/progress.log")
            if final or status["state"] == "running":
                gcs.put(str(run / "train.log"), f"{remote}/train.log")

    push()
    wait_for(os.environ["MODEL_DIR"] + "/.complete", "base weights")
    wait_for(f"/opt/posttrain/.overlay_{os.environ['AUTOMODEL_COMMIT']}", "AutoModel overlay")
    data = spec.get("data", f"{PREFIX}/sft/latest/")
    local_data = pathlib.Path("/data/sft") / name
    gcs.getdir(data, str(local_data))
    status["data"] = {"source": data, "files": sorted(p.name for p in local_data.iterdir())}
    cfg = spec["config"] if spec["config"].startswith("/") else f"/workspace/training/configs/{spec['config']}"
    nproc = str(spec.get("nproc") or subprocess.run(["nvidia-smi", "-L"], capture_output=True, text=True).stdout.count("GPU "))
    cmd = ["automodel", cfg, "--nproc-per-node", nproc] + [str(x) for x in spec.get("overrides", [])]
    env = {**os.environ, "PYTHONPATH": "/workspace/training:" + os.environ.get("PYTHONPATH", ""),
           "SFT_DATA_DIR": str(local_data), "RUN_DIR": str(run)}
    status.update(state="running", cmd=cmd)
    push()
    with open(run / "train.log", "a") as log:
        p = subprocess.Popen(cmd, cwd=run, env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        stop = threading.Event()

        def watcher():
            last = 0
            while not stop.wait(60):
                if gcs.ls(f"{PREFIX}/jobs/{name}.cancel"):
                    status["state"] = "cancelling"
                    os.killpg(p.pid, signal.SIGTERM)
                if time.time() - last > 60 * spec.get("upload_every_min", 10):
                    try:
                        push()
                    except Exception as e:  # noqa: BLE001
                        print("status upload failed:", e, flush=True)
                    last = time.time()

        threading.Thread(target=watcher, daemon=True).start()
        rc = p.wait()
        stop.set()
    status.update(state="done" if rc == 0 else ("cancelled" if status["state"] == "cancelling" else "failed"),
                  returncode=rc, finished=time.strftime("%FT%TZ", time.gmtime()))
    if spec.get("upload_checkpoints", True):
        for ck in run.glob("checkpoints*"):
            gcs.putdir(str(ck), f"{remote}/{ck.name}")
    push(final=True)
    print("job", name, status["state"], flush=True)


if __name__ == "__main__":
    main()
