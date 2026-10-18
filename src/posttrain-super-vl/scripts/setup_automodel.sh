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
# Documented install inside the container (docs/guides/vlm/nemotron-3-5-super-vl.mdx, "26.04+"); keeps the
# container's torch / TE / mamba kernels.
uv pip install --python "$PY" ".[vlm-media]"
"$PY" -c "import nemo_automodel, sys; print('nemo_automodel', getattr(nemo_automodel, '__version__', '?'), nemo_automodel.__file__)"
"$PY" -c "from nemo_automodel.components.datasets.llm import agent_chat; from nemo_automodel.components.distributed.context_parallel import mamba; print('agent_chat + mamba CP present')"
touch "$MARK"
echo "AutoModel overlay at $C done"
