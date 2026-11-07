FROM python:3.11-slim

WORKDIR /app

# Install git (needed for git dependencies like cortex-utils)
RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Copy project files
COPY pyproject.toml uv.lock* ./
COPY src/ ./src/

# Install dependencies
RUN uv sync --frozen --no-dev

# Create non-root user with home directory
RUN useradd -r -m -s /bin/false appuser && chown -R appuser:appuser /app
USER appuser

# Set UV cache in writable location
ENV UV_CACHE_DIR=/home/appuser/.cache/uv

# Expose ports
# 8080 = internal (peer traffic, no token). 8098 = external (token required).
# Publish ONLY 8098 to the host; see processes/gateway-two-port-deploy.md.
EXPOSE 8080 8098 8001

# Run with gunicorn (single worker to avoid metrics port conflict)
CMD ["/app/.venv/bin/gunicorn", "-w", "1", "-b", "0.0.0.0:8080", "-b", "0.0.0.0:8098", "gateway.app:create_app()"]
