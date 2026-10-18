# NeMo AutoModel 26.08 (newest release, built 2026-08-25; CUDA 13.3, PyTorch 2.13, TE 2.16, mamba_ssm/causal_conv1d
# prebuilt, sm80 in TORCH_CUDA_ARCH_LIST). Pinned by digest: the multi-arch index of nvcr.io/nvidia/nemo-automodel:26.08,
# checked against the nvcr.io registry API on 2026-10-18 (amd64 manifest sha256:c21c0c3b...6294f6).
# The Nemotron 3.5 Super VL recipes landed in AutoModel main after this release (2026-09-09), so scripts/setup_automodel.sh
# overlays a pinned AutoModel commit at first start (kept on the data disk), not at build time: a failed build would
# stop the app from launching at all.
FROM nvcr.io/nvidia/nemo-automodel:26.08@sha256:b0bd0e50b29cb0a3ec69c3cbbb49652d3c1aabedbfe22ee0fa6d053b6bb33aee
COPY scripts/ /opt/posttrain/scripts/
RUN chmod +x /opt/posttrain/scripts/*.sh
ENTRYPOINT ["/opt/posttrain/scripts/start.sh"]
