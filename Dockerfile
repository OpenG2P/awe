# =============================================================================
# OpenG2P Approval Workflow Engine — Multi-stage Docker Build
# =============================================================================
# Build:
#   docker build -t openg2p-awe:latest \
#     --build-arg GIT_COMMIT=$(git rev-parse --short HEAD) \
#     --build-arg BUILD_TIME=$(date -u +"%Y-%m-%dT%H:%M:%SZ") .
#
# Run:
#   docker run -p 8000:8000 \
#     -e DB_HOST=host.docker.internal \
#     -e DB_PORT=5432 \
#     -e DB_NAME=awe \
#     -e DB_USER=postgres \
#     -e DB_PASSWORD=postgres \
#     openg2p-awe:latest
# =============================================================================

# ---------------------------------------------------------------------------
# Stage 1: Build the wheel
# ---------------------------------------------------------------------------
FROM python:3.13-slim AS builder

WORKDIR /build

COPY pyproject.toml .
COPY src/ src/
COPY config/ config/

RUN pip install --no-cache-dir build && \
    python -m build --wheel --outdir /build/dist

# ---------------------------------------------------------------------------
# Stage 2: Runtime
# ---------------------------------------------------------------------------
FROM python:3.13-slim

ARG GIT_COMMIT=dev
ARG BUILD_TIME=dev

ENV GIT_COMMIT=${GIT_COMMIT}
ENV BUILD_TIME=${BUILD_TIME}

# Database connection (must be provided at runtime).
ENV DB_HOST=localhost
ENV DB_PORT=5432
ENV DB_NAME=awe
ENV DB_USER=postgres
# DB_PASSWORD must be supplied at runtime — never baked into the image.

ENV CONFIG_PATH=/app/config/default.yaml

ENV AWE_WORKER_TYPE=gunicorn
ENV AWE_HOST=0.0.0.0
ENV AWE_PORT=8000
# Default worker count kept low (2) for predictable memory; the Helm chart
# overrides this per-deployment via AWE_NO_OF_WORKERS in envVars.
ENV AWE_NO_OF_WORKERS=2

RUN groupadd --gid 1000 appuser && \
    useradd --uid 1000 --gid 1000 --create-home appuser

WORKDIR /app

COPY --from=builder /build/dist/*.whl /tmp/
RUN pip install --no-cache-dir /tmp/*.whl && \
    rm -f /tmp/*.whl

COPY --chown=appuser:appuser config/ /app/config/
COPY --chown=appuser:appuser docker-entrypoint.sh /app/
RUN chmod +x /app/docker-entrypoint.sh

USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=120s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:8000/v1/awe/health')"]

ENTRYPOINT ["/app/docker-entrypoint.sh"]
CMD []
