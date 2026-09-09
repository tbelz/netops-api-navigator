# Stage 1: Builder
FROM python:3.12-slim AS builder

RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    && rm -rf /var/lib/apt/lists/*

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /build

# Copy MCP server source
COPY pyproject.toml uv.lock* README.md ./
COPY src/ ./src/

# Create venv and install all dependencies
# Override python-preference from pyproject.toml — Docker provides system Python
ENV UV_PYTHON_PREFERENCE=only-system
RUN uv venv /opt/venv
ENV VIRTUAL_ENV=/opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Install MCP server (pulls httpx, structlog, mcp, etc.)
RUN uv pip install .

# Stage 2: Runtime
FROM python:3.12-slim

# Copy Python venv from builder
COPY --from=builder /opt/venv /opt/venv
ENV VIRTUAL_ENV=/opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Seed scripts and central_helpers.py are copied at runtime by server.py
# (the Docker volume mount overrides this directory anyway)

# Create required directories (LadybugDB creates graph_db dir itself)
RUN mkdir -p /scripts/library /data/oas_cache

# Environment
ENV PYTHONUNBUFFERED=1
ENV SCRIPT_LIBRARY_PATH=/scripts/library
ENV DOCS_PATH=/docs
ENV SPEC_CACHE_DIR=/data/oas_cache
ENV GRAPH_DB_PATH=/data/graph_db
ENV KNOWLEDGE_RELEASE_REPO=tbelz/netops-api-navigator

ENTRYPOINT ["netops-api-navigator"]
