FROM ollama/ollama:latest

ENV OLLAMA_HOST=0.0.0.0:11434 \
    OLLAMA_MODELS=/data/models \
    OLLAMA_NUM_PARALLEL=1 \
    OLLAMA_MAX_LOADED_MODELS=1

COPY start.sh /usr/local/bin/start-embeddings
ENTRYPOINT ["/bin/sh", "/usr/local/bin/start-embeddings"]
CMD []
