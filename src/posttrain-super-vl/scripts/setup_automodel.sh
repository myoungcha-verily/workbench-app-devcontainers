#!/bin/bash
# Overlay the pinned AutoModel commit on the container's install (idempotent; marker on the data disk). The container
# itself is rebuilt from the pinned digest on every app (re)creation, so this runs once per container.
set -euo pipefail
C=${AUTOMODEL_COMMIT:?}
MARK=/opt/posttrain/.overlay_$C   # container filesystem: a rebuilt container needs the overlay again
[ -f "$MARK" ] && { echo "already at $C"; exit 0; }
SRC=/data/src/Automodel
if [ ! -d "$SRC/.git" ]; then
  mkdir -p /data/src && git clone https://github.com/NVIDIA-NeMo/Automodel.git "$SRC"
fi
git -C "$SRC" fetch origin && git -C "$SRC" checkout --force "$C"
PY=/opt/venv/bin/python; [ -x "$PY" ] || PY=$(command -v python3)
cd "$SRC"
# The documented install is `uv pip install ".[vlm-media]"` (docs/guides/vlm/nemotron-3-5-super-vl.mdx, "26.04+");
# it failed in the first deploy (2026-10-18, log not captured), so:
# Install only AutoModel's own code on top of the container's tested stack (torch / TE / mamba kernels): a full
# dependency resolve can try to replace pinned packages. Missing pure-Python deps are added one by one below.
if command -v uv >/dev/null; then PIP=(uv pip install --python "$PY"); else PIP=("$PY" -m pip install); fi
"${PIP[@]}" --no-deps -e .
# The pinned transformers (and its huggingface_hub) are newer than the container's; upgrade them while pinning the
# container's own torch build so nothing replaces the tested CUDA stack.
TORCH_PIN=$(mktemp); "$PY" -c "import torch; print('torch==' + torch.__version__)" > "$TORCH_PIN"
TF=$(grep -oE '"transformers==[^"]+"' pyproject.toml | tr -d '"')
"${PIP[@]}" --constraint "$TORCH_PIN" "${TF:-transformers}"
for i in 1 2 3 4 5 6; do
  miss=$("$PY" -c "
import importlib, sys
try:
    from nemo_automodel.components.datasets.llm import agent_chat
    from nemo_automodel.components.distributed.context_parallel import mamba
    import nemo_automodel.cli.app  # noqa: the `automodel` entry point (imports transformers, peft, ...)
except ModuleNotFoundError as e:
    print('missing', e.name.split('.')[0]); sys.exit(0)
except ImportError as e:  # e.g. cannot import name X from huggingface_hub -> too old
    print('upgrade', (e.name or '').split('.')[0]); sys.exit(0)
" 2>/dev/null)
  [ -z "$miss" ] && break
  set -- $miss
  [ -z "${2:-}" ] && { echo "unresolved import problem: $miss"; break; }
  echo "$1 dependency: $2"
  if [ "$1" = upgrade ]; then "${PIP[@]}" --constraint "$TORCH_PIN" -U "$2"; else "${PIP[@]}" --constraint "$TORCH_PIN" "$2"; fi
done
"$PY" -c "import nemo_automodel, sys; print('nemo_automodel', getattr(nemo_automodel, '__version__', '?'), nemo_automodel.__file__)"
"$PY" -c "from nemo_automodel.components.datasets.llm import agent_chat; from nemo_automodel.components.distributed.context_parallel import mamba; print('agent_chat + mamba CP present')"
touch "$MARK"
echo "AutoModel overlay at $C done"
