#!/bin/sh
set -eu

mkdir -p "$OLLAMA_MODELS"
ollama serve &
server_pid=$!
trap 'kill "$server_pid" 2>/dev/null || true; wait "$server_pid" 2>/dev/null || true' EXIT
trap 'exit 143' TERM
trap 'exit 130' INT

export OLLAMA_HOST=127.0.0.1:11434
attempt=0
until ollama list >/dev/null 2>&1; do
    attempt=$((attempt + 1))
    if ! kill -0 "$server_pid" 2>/dev/null || [ "$attempt" -ge 30 ]; then
        echo "Ollama did not start" >&2
        exit 1
    fi
    sleep 1
done

if ! ollama show bge-m3 >/dev/null 2>&1; then
    ollama pull bge-m3
fi

echo 'Ollama model bge-m3 is ready'
wait "$server_pid"
