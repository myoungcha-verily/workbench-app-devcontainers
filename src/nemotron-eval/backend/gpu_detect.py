import os
import re
import subprocess
from dataclasses import dataclass, field
from typing import Optional, List


@dataclass
class GPUInfo:
    name: str
    vram_gb: int
    count: int
    cuda_version: str
    driver_version: str


@dataclass
class ModelSpec:
    model_id: str
    name: str
    min_vram_gb: int
    recommended_vram_gb: int
    # Tried in order until one succeeds: an Ollama registry tag, then a GGUF
    # object in MODELS_BUCKET imported with `ollama create`.
    ollama_tag: Optional[str] = None
    gcs_object: Optional[str] = None


@dataclass
class ModelCompat:
    model_id: str
    name: str
    min_vram_gb: int
    recommended_vram_gb: int
    can_run: bool
    warning: Optional[str] = None
    sources: List[str] = field(default_factory=list)


# Smallest first: the bootstrap prepares models in this order, so the 4B is
# usable within minutes while the 30B is still downloading.
MODEL_SPECS = [
    ModelSpec(
        model_id="nemotron-3-nano:4b",
        name="Nemotron 3 Nano 4B",
        min_vram_gb=6,
        recommended_vram_gb=8,
        ollama_tag="nemotron-3-nano:4b",
        gcs_object="models/NVIDIA-Nemotron3-Nano-4B-Q4_K_M.gguf",
    ),
    ModelSpec(
        model_id="nemotron-nano:9b-v2",
        name="Nemotron Nano 9B v2",
        min_vram_gb=10,
        recommended_vram_gb=12,
        gcs_object="models/nvidia_NVIDIA-Nemotron-Nano-9B-v2-Q4_K_M.gguf",
    ),
    ModelSpec(
        model_id="nemotron-3.5-bolt:74b",
        name="Nemotron 3.5 Bolt 74B-A9B (2-bit, 24GB)",
        # 22 GB of native 2-bit weights plus KV cache; fits the A100 40GB with room for context.
        min_vram_gb=24,
        recommended_vram_gb=32,
        ollama_tag="nemotron-3.5-bolt:74b-a9b-iq2",
        gcs_object="models/NVIDIA-Nemotron-3.5-Bolt-74B-A9B-24GB.gguf",
    ),
    ModelSpec(
        model_id="nemotron-3.5-lightning:30b",
        name="Nemotron 3.5 Lightning 30B",
        # 25.5 GB of weights plus KV cache at a 16K context.
        min_vram_gb=32,
        recommended_vram_gb=40,
        ollama_tag="nemotron-3.5-lightning:30b",
        gcs_object="models/NVIDIA-Nemotron-3.5-Lightning-30B-A3B-Q4_K_M.gguf",
    ),
]

SPECS_BY_ID = {m.model_id: m for m in MODEL_SPECS}


def detect_gpu() -> Optional[GPUInfo]:
    try:
        result = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=name,memory.total,driver_version",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
    except Exception:
        return None

    gpus = [
        [field.strip() for field in line.split(",")]
        for line in result.stdout.strip().splitlines()
        if line.strip()
    ]
    if not gpus:
        return None

    total_vram_mb = sum(int(g[1]) for g in gpus)

    # nvcc is not in the image; the banner of plain nvidia-smi reports the
    # highest CUDA version the driver supports.
    cuda_version = "unknown"
    try:
        banner = subprocess.run(["nvidia-smi"], capture_output=True, text=True, timeout=10).stdout
        match = re.search(r"CUDA Version:\s*([\d.]+)", banner)
        if match:
            cuda_version = match.group(1)
    except Exception:
        pass

    return GPUInfo(
        name=gpus[0][0],
        vram_gb=round(total_vram_mb / 1024),
        count=len(gpus),
        cuda_version=cuda_version,
        driver_version=gpus[0][2],
    )


def check_compatibility(gpu: Optional[GPUInfo]) -> List[ModelCompat]:
    allow_cpu = os.environ.get("ALLOW_CPU", "").lower() in ("1", "true", "yes")
    # Optional comma-separated model_id allowlist, e.g. to skip the 30B download.
    enabled = {m.strip() for m in os.environ.get("ENABLED_MODELS", "").split(",") if m.strip()}
    results = []
    for m in MODEL_SPECS:
        sources = [s for s in (m.ollama_tag and "ollama", m.gcs_object and "gcs") if s]
        if enabled and m.model_id not in enabled:
            can_run, warning = False, "Disabled by ENABLED_MODELS"
        elif not gpu:
            can_run = allow_cpu
            warning = "No GPU detected — CPU inference (very slow)" if allow_cpu else "No GPU detected"
        else:
            can_run = gpu.vram_gb >= m.min_vram_gb
            warning = None
            if not can_run:
                warning = f"Needs {m.min_vram_gb}GB VRAM, have {gpu.vram_gb}GB"
            elif gpu.vram_gb < m.recommended_vram_gb:
                warning = f"Will run but may be slow (recommended: {m.recommended_vram_gb}GB)"
        results.append(
            ModelCompat(
                model_id=m.model_id,
                name=m.name,
                min_vram_gb=m.min_vram_gb,
                recommended_vram_gb=m.recommended_vram_gb,
                can_run=can_run,
                warning=warning,
                sources=sources,
            )
        )
    return results
