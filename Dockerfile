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

# Notification connector (openg2p-notification, Novu) — optional at runtime:
# the app soft-imports it, so this stays a build-time-only extra. Wheels are
# built here (git is needed only for the git+https fetch) and installed in
# the runtime stage from the local wheelhouse. Mirrors registry-platform's
# NOTIFICATION_REPO/NOTIFICATION_REF pattern.
ARG NOTIFICATION_REPO=openg2p/notifications
ARG NOTIFICATION_REF=develop

WORKDIR /build

COPY pyproject.toml .
COPY src/ src/
COPY config/ config/

RUN apt-get update && \
    apt-get install -y --no-install-recommends git && \
    pip install --no-cache-dir build && \
    python -m build --wheel --outdir /build/dist && \
    pip wheel --no-cache-dir --wheel-dir /build/connector-wheels \
        "git+https://github.com/${NOTIFICATION_REPO}@${NOTIFICATION_REF}#subdirectory=connector"

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

ENV UVICORN_HOST=0.0.0.0
ENV UVICORN_PORT=8000
ENV UVICORN_WORKERS=1
ENV UVICORN_LOG_LEVEL=info

RUN groupadd --gid 1000 appuser && \
    useradd --uid 1000 --gid 1000 --create-home appuser

WORKDIR /app

COPY --from=builder /build/dist/*.whl /tmp/
COPY --from=builder /build/connector-wheels /tmp/connector-wheels
RUN pip install --no-cache-dir /tmp/*.whl && \
    pip install --no-index --no-cache-dir \
        --find-links=/tmp/connector-wheels openg2p-notification && \
    rm -rf /tmp/*.whl /tmp/connector-wheels

COPY --chown=appuser:appuser config/ /app/config/
COPY --chown=appuser:appuser docker-entrypoint.sh /app/
RUN chmod +x /app/docker-entrypoint.sh

USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=120s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:8000/v1/awe/health')"]

ENTRYPOINT ["/app/docker-entrypoint.sh"]
CMD []
