FROM python:3.11-slim

WORKDIR /app

# System deps for Kuzu (graph DB) and Qdrant client
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    wget \
    && rm -rf /var/lib/apt/lists/*

# Install the package
COPY pyproject.toml README.md LICENSE ./
COPY src/ ./src/
COPY MANIFEST.in ./
COPY assets/ ./assets/
COPY docs/ ./docs/

RUN pip install --no-cache-dir .

# Default: start the upstream server
EXPOSE 19527 8765

CMD ["python", "-m", "hyatlas_memory.server.start_server"]
