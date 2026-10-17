# Image pinned to the model card's recommended nightly (the card also names v0.32.0, which is not published on Docker Hub).
FROM vllm/vllm-openai:nightly-3f1bcfd82bb2e93fcd364fc7a5491718453a77e2
COPY scripts/serve.sh /usr/local/bin/serve.sh
RUN chmod +x /usr/local/bin/serve.sh
ENTRYPOINT ["/usr/local/bin/serve.sh"]
