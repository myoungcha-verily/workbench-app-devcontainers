import asyncio
import os
import time
from pathlib import Path
from typing import Dict, List

import ollama_client
from gpu_detect import SPECS_BY_ID, ModelCompat, ModelSpec
from models import ModelStatus

MODELS_BUCKET = os.environ.get("MODELS_BUCKET", "cloned-models-wb-meteoric-gourd-5139")
DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))


class ModelManager:
    """Makes every model that fits on this GPU available in Ollama.

    Runs once in the background at startup. Weights are cached under
    OLLAMA_MODELS on the data volume, so restarts skip straight to READY.
    """

    def __init__(self):
        self.status: Dict[str, Dict[str, str]] = {}
        # capabilities and supported `think` values from /api/show
        self.info: Dict[str, Dict[str, list]] = {}

    def set(self, model_id: str, status: ModelStatus, detail: str = ""):
        self.status[model_id] = {"status": status.value, "detail": detail}
        print(f"[models] {model_id}: {status.value} {detail}", flush=True)

    def is_ready(self, model_id: str) -> bool:
        return self.status.get(model_id, {}).get("status") == ModelStatus.READY.value

    async def bootstrap(self, compat: List[ModelCompat]):
        for m in compat:
            if m.can_run:
                self.set(m.model_id, ModelStatus.PENDING, "Waiting for Ollama")
            else:
                self.set(m.model_id, ModelStatus.UNAVAILABLE, m.warning or "")

        for _ in range(90):
            if await ollama_client.is_up():
                break
            await asyncio.sleep(2)
        else:
            for m in compat:
                if m.can_run:
                    self.set(m.model_id, ModelStatus.ERROR, "Ollama did not start; see container logs")
            return

        for m in compat:
            if not m.can_run:
                continue
            try:
                await self.prepare(SPECS_BY_ID[m.model_id])
                await self.describe(m.model_id)
                self.set(m.model_id, ModelStatus.READY)
            except Exception as e:
                self.set(m.model_id, ModelStatus.ERROR, str(e)[:300])

        # The first request to a model pays for loading it into VRAM and a
        # one-time CUDA warm-up (about 2 minutes for the 30B on an A100). Pay
        # that here for the largest ready model, so the first chat is fast.
        ready = [m for m in compat if self.is_ready(m.model_id)]
        if ready:
            default = max(ready, key=lambda m: m.min_vram_gb).model_id
            print(f"[models] warming up {default}", flush=True)
            try:
                await ollama_client.warm_up(default)
                print(f"[models] {default} loaded on GPU", flush=True)
            except Exception as e:
                print(f"[models] warm-up of {default} failed: {e}", flush=True)

    async def describe(self, model_id: str):
        try:
            show = await ollama_client.show(model_id)
        except Exception as e:
            print(f"[models] could not describe {model_id}: {e}", flush=True)
            return
        self.info[model_id] = {
            "capabilities": show.get("capabilities") or [],
            "thinking_values": (show.get("thinking") or {}).get("values") or [],
        }

    async def prepare(self, spec: ModelSpec):
        if spec.model_id in await ollama_client.installed_models():
            return

        errors = []
        if spec.ollama_tag:
            try:
                await self.pull(spec)
                return
            except Exception as e:
                errors.append(f"registry: {e}")
        if spec.gcs_object:
            try:
                await self.import_gguf(spec)
                return
            except Exception as e:
                errors.append(f"gcs: {e}")
        raise RuntimeError("; ".join(errors) or "no model source configured")

    async def pull(self, spec: ModelSpec):
        last_report = 0.0
        async for chunk in ollama_client.stream_lines("POST", "/api/pull", {"model": spec.ollama_tag}):
            total, done = chunk.get("total"), chunk.get("completed")
            if total and done is not None and time.time() - last_report > 2:
                pct = 100 * done / total
                self.set(spec.model_id, ModelStatus.LOADING, f"Pulling {spec.ollama_tag}: {pct:.0f}% of {total / 1e9:.1f} GB")
                last_report = time.time()
        if spec.ollama_tag != spec.model_id:
            async with ollama_client.client() as c:
                resp = await c.post("/api/copy", json={"source": spec.ollama_tag, "destination": spec.model_id})
                resp.raise_for_status()

    async def import_gguf(self, spec: ModelSpec):
        gguf_dir = DATA_DIR / "gguf"
        gguf_dir.mkdir(parents=True, exist_ok=True)
        gguf_path = gguf_dir / Path(spec.gcs_object).name
        modelfile = gguf_dir / f"{gguf_path.stem}.Modelfile"

        self.set(spec.model_id, ModelStatus.LOADING, f"Downloading gs://{MODELS_BUCKET}/{spec.gcs_object}")
        await asyncio.to_thread(_download_gcs, MODELS_BUCKET, spec.gcs_object, gguf_path)

        self.set(spec.model_id, ModelStatus.LOADING, "Importing GGUF into Ollama")
        modelfile.write_text(f"FROM {gguf_path}\n")
        proc = await asyncio.create_subprocess_exec(
            "ollama", "create", spec.model_id, "-f", str(modelfile),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
        out, _ = await proc.communicate()
        # Ollama copies the weights into its blob store; drop the duplicate.
        gguf_path.unlink(missing_ok=True)
        modelfile.unlink(missing_ok=True)
        if proc.returncode != 0:
            raise RuntimeError(f"ollama create failed: {out.decode(errors='replace')[-300:]}")


def _download_gcs(bucket_name: str, object_name: str, dest: Path):
    from google.cloud import storage
    from google.cloud.storage import transfer_manager

    blob = storage.Client().bucket(bucket_name).blob(object_name)
    tmp = dest.with_suffix(dest.suffix + ".part")
    transfer_manager.download_chunks_concurrently(
        blob, str(tmp), chunk_size=64 * 1024 * 1024, max_workers=8,
        worker_type=transfer_manager.THREAD,
    )
    tmp.rename(dest)
