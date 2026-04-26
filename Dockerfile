# =============================================================================
# RuriSkry — FastAPI backend
# =============================================================================
# All governance agents and operational agents run in-process with the API.
# Build:  docker build -t ruriskry-backend:latest .
# Run:    docker run -p 8000:8000 --env-file .env ruriskry-backend:latest
# =============================================================================

FROM python:3.11-slim

# Install system dependencies needed by some Azure SDK packages,
# plus the Azure CLI required by the Tier 3 playbook executor (Phase 34E/F).
# `az` is invoked as a subprocess from src/core/az_executor.py with a hard
# allowlist; without it installed, every live Tier 3 execution fails with
# "command not found". Mock mode never invokes the subprocess.
RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc \
        libffi-dev \
        curl \
        ca-certificates \
    && curl -sL https://aka.ms/InstallAzureCLIDeb | bash \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies first (cached layer — only busts when requirements change)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source
COPY src/ ./src/
COPY data/ ./data/

# data/ contains seed JSON files (seed_resources.json, seed_incidents.json,
# policies.json) used in mock mode and as fallback in live mode.
# decisions/, agents/, scans/, executions/ are written at runtime — they must
# exist as directories so the app can write to them immediately.
RUN mkdir -p data/decisions data/agents data/scans data/executions

# Non-root user for security
RUN useradd -m appuser && chown -R appuser /app
USER appuser

# Expose the port the FastAPI app listens on
EXPOSE 8000

# Start with uvicorn.  --host 0.0.0.0 required inside a container.
# --workers 1: all agents share a single asyncio event loop — multiple workers
# would duplicate in-memory state (scans dict, SSE queues).
CMD ["uvicorn", "src.api.dashboard_api:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
