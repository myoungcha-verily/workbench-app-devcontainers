"""Merge a LoRA adapter (HF/PEFT safetensors layout, as AutoModel writes with enable_hf_state_dict_adapter) into the
Super VL BF16 checkpoint, shard by shard on CPU (no GPU; peak RAM ~ one 4 GB shard plus the adapter).

W' = W + (alpha / r) * B @ A, computed in fp32, stored in bf16. Non-LoRA tensors and all non-weight files (config,
tokenizer, chat template, remote code) are copied unchanged, so the output directory is served by the same vLLM app
(MODEL_DIR=<out>) and evaluated with the unchanged harness model key nemotron-3.5-super-vl-vllm.

usage: merge_lora.py --base /data/models/super-vl-bf16 --adapter /data/runs/<job>/checkpoints/<step>/model \
                     --out /data/models/super-vl-stageB-<job>-<step> [--check]
--check only verifies that every adapter pair maps to exactly one base tensor of matching shape (run this first).
The merged weights are a controlled artifact (trained on Baseline data): keep them on the data disk / workspace bucket.
"""
import argparse
import json
import pathlib
import re
import shutil

import torch
from safetensors import safe_open
from safetensors.torch import save_file


def adapter_pairs(adapter_dir):
    d = pathlib.Path(adapter_dir)
    cfg = json.loads((d / "adapter_config.json").read_text())
    scale = cfg.get("lora_alpha", cfg.get("alpha")) / cfg.get("r", cfg.get("dim"))
    tensors = {}
    for f in sorted(d.glob("*.safetensors")):
        with safe_open(f, "pt") as s:
            for k in s.keys():
                tensors[k] = s.get_tensor(k)
    pairs = {}
    for k, a in tensors.items():
        m = re.match(r"(?:base_model\.model\.)?(.*)\.lora_A(?:\.default)?\.weight$", k)
        if not m:
            continue
        b = tensors[k.replace("lora_A", "lora_B")]
        pairs[m.group(1) + ".weight"] = (a, b)
    return pairs, scale, cfg


def resolve(name, base_keys):
    """Adapter module name -> base checkpoint key (the VL checkpoint nests the LLM; try the common prefixes)."""
    if name in base_keys:
        return name
    for pre in ("language_model.", "model.language_model.", "llm.", "language_model.model."):
        for cand in (pre + name, name.replace("model.", pre, 1)):
            if cand in base_keys:
                return cand
    hits = [k for k in base_keys if k.endswith("." + name) or k.endswith(name.split("model.", 1)[-1])]
    return hits[0] if len(hits) == 1 else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", required=True)
    ap.add_argument("--adapter", required=True)
    ap.add_argument("--out")
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args()
    base = pathlib.Path(a.base)
    index = json.loads((base / "model.safetensors.index.json").read_text())["weight_map"]
    pairs, scale, cfg = adapter_pairs(a.adapter)
    mapping, missing = {}, []
    for name in pairs:
        k = resolve(name, index)
        (mapping.__setitem__(name, k) if k else missing.append(name))
    print(f"adapter: r={cfg.get('r')} alpha={cfg.get('lora_alpha')} scale={scale:g}, {len(pairs)} LoRA pairs, "
          f"{len(mapping)} mapped, {len(missing)} unmapped")
    for n in missing[:20]:
        print("  unmapped:", n)
    by_shard = {}
    for name, k in mapping.items():
        by_shard.setdefault(index[k], []).append((name, k))
    if a.check:
        bad = 0
        for shard, items in by_shard.items():
            with safe_open(base / shard, "pt") as s:
                for name, k in items:
                    A, B = pairs[name]
                    shp = tuple(s.get_slice(k).get_shape())
                    if (B.shape[0], A.shape[1]) != shp:
                        bad += 1
                        print(f"  shape mismatch {k}: base {shp} vs B@A {(B.shape[0], A.shape[1])}")
        print("check:", "OK" if not missing and not bad else f"FAILED ({len(missing)} unmapped, {bad} shape mismatches)")
        return
    out = pathlib.Path(a.out)
    out.mkdir(parents=True, exist_ok=False)
    for f in base.iterdir():  # config, tokenizer, chat template, remote code, index
        if f.is_file() and not f.name.endswith(".safetensors"):
            shutil.copy2(f, out / f.name)
    for shard in sorted(set(index.values())):
        with safe_open(base / shard, "pt") as s:
            meta = s.metadata()
            t = {k: s.get_tensor(k) for k in s.keys()}
        for name, k in by_shard.get(shard, []):
            A, B = pairs[name]
            t[k] = (t[k].float() + scale * (B.float() @ A.float())).to(t[k].dtype)
        save_file(t, str(out / shard), metadata=meta)
        print("wrote", shard, len(by_shard.get(shard, [])), "merged")
    (out / "MERGED_FROM.json").write_text(json.dumps({"base": str(base), "adapter": str(a.adapter), "scale": scale,
                                                      "adapter_config": cfg, "merged": len(mapping)}, indent=1))


if __name__ == "__main__":
    main()
