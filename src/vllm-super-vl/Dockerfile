# Image pinned to the model card's recommended release.
FROM vllm/vllm-openai:v0.32.0
COPY scripts/serve.sh /usr/local/bin/serve.sh
RUN chmod +x /usr/local/bin/serve.sh
ENTRYPOINT ["/usr/local/bin/serve.sh"]
