# HyAtlas Memory — zvec-native image (v3.4+)
# No Qdrant sidecar. Vector store + Kuzu graph run in-process.
FROM python:3.11-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HYATLAS_HOME=/data/hyatlas \
    HY_MEMORY_HOST=0.0.0.0 \
    HY_MEMORY_PORT=19527 \
    HY_DASH_BIND=0.0.0.0 \
    HY_DASH_PORT=8765 \
    HY_MEMORY_BASE=http://127.0.0.1:19527 \
    FOR_DISABLE_CONSOLE_CLOSE_HANDLER=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        curl \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Build-time switch: remote OpenAI-compatible embeddings (default) or local BGE.
#   docker build --build-arg INSTALL_LOCAL_EMBED=1 -t hyatlas-memory .
ARG INSTALL_LOCAL_EMBED=0

COPY pyproject.toml README.md LICENSE MANIFEST.in ./
COPY src/ ./src/
COPY docker/ ./docker/

# Core + zvec always. local-embed is optional (large torch/ST deps).
RUN pip install --no-cache-dir ".[zvec]" \
    && if [ "$INSTALL_LOCAL_EMBED" = "1" ]; then \
         pip install --no-cache-dir ".[local-embed]"; \
       fi \
    && mkdir -p /data/hyatlas/config /data/hyatlas/data /data/hyatlas/logs /data/hyatlas/zvec \
    && chmod +x /app/docker/entrypoint.sh

EXPOSE 19527 8765

VOLUME ["/data/hyatlas"]

HEALTHCHECK --interval=15s --timeout=5s --start-period=40s --retries=5 \
  CMD curl -fsS "http://127.0.0.1:${HY_MEMORY_PORT}/api/v1/status" >/dev/null || exit 1

ENTRYPOINT ["/app/docker/entrypoint.sh"]
CMD ["stack"]
