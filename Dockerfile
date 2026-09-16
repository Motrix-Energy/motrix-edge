FROM python:3.12-slim-bookworm

# OCI image metadata.
LABEL org.opencontainers.image.title="Motrix Edge" \
      org.opencontainers.image.description="Extensible single-site energy management system" \
      org.opencontainers.image.vendor="Radisio" \
      org.opencontainers.image.licenses="Apache-2.0" \
      org.opencontainers.image.source="https://github.com/Motrix-Energy/motrix-edge"

# Prevent .pyc files and enable unbuffered stdout for Docker log collection
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Install dependencies first (layer caching — requirements change less often than code)
# requirements-api.txt pulls in requirements.txt and adds fastapi + uvicorn. Those are
# optional for the source tree (a CSV-only EMS installs neither) but not for the image:
# the viewer container's nginx reverse-proxies /api/* to edge:8000, so the deployment
# artefact always ships the service, whether or not a given config.json declares it.
COPY requirements.txt requirements-api.txt ./
RUN pip install --no-cache-dir -r requirements-api.txt

# Copy application code
COPY . .

# Create data directory mount point
RUN mkdir -p /app/data

# Non-root user for security
RUN addgroup --system motrix && adduser --system --ingroup motrix motrix \
    && chown -R motrix:motrix /app
USER motrix

# Documentation only — EXPOSE publishes nothing. The rest_api service's default port,
# reachable across the compose network (that is what the viewer proxies to) and
# deliberately not mapped to the host; see docker-compose.yml.
EXPOSE 8000

# Health check. Three things here are load-bearing:
#   * No curl. This is a slim base and adding a package for one GET is not worth the
#     layer; urllib is in the stdlib and already resident.
#   * ProxyHandler({}) is not decoration. urllib honours http_proxy/HTTP_PROXY from the
#     environment and does NOT auto-bypass 127.0.0.1 (no_proxy has to list it). On a
#     gateway with a proxy set, the bare form times out against a perfectly healthy EMS.
#   * MOTRIX_HEALTHCHECK_URL unset falls back to the old PID-1 liveness probe. The API is an
#     optional service, so a hard HTTP check would mark every EMS whose config.json does
#     not declare one permanently unhealthy — and under `restart: unless-stopped` that
#     means a restart loop. Compose sets the variable; a stripped deployment leaves it empty.
# Exit codes are Docker's contract: 0 healthy, 1 unhealthy. A non-2xx raises HTTPError and
# a refused connection raises URLError; uncaught, both exit 1. /health answers 503 only
# when a worker is down past its restart cap — the one state a container restart can fix.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD ["python", "-c", "import os, urllib.request as u; url = os.environ.get('MOTRIX_HEALTHCHECK_URL'); u.build_opener(u.ProxyHandler({})).open(url, timeout=3) if url else os.kill(1, 0)"]

ENTRYPOINT ["python", "main.py"]
