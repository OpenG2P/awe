# =============================================================================
# OpenG2P AWE Admin UI — Multi-stage Docker Build
# =============================================================================
# Build:
#   docker build -t openg2p-awe-ui:latest \
#     --build-arg GIT_COMMIT=$(git rev-parse --short HEAD) \
#     --build-arg BUILD_TIME=$(date -u +"%Y-%m-%dT%H:%M:%SZ") ./ui
#
# Run:
#   docker run -p 8080:8080 openg2p-awe-ui:latest
#
# Deploy pattern:
#   Static SPA served by nginx. API calls (`/v1/awe/*`) are routed to the
#   backend Service by Istio at the cluster level — nginx does NOT proxy.
#   React-router deep links fall back to /index.html (see nginx.conf).
# =============================================================================

# ---------------------------------------------------------------------------
# Stage 1: build
# ---------------------------------------------------------------------------
FROM node:20-alpine AS builder

WORKDIR /app

# Install deps first for better layer caching.
COPY package*.json ./
RUN npm ci

# Copy sources + build.
COPY . .
RUN npm run build

# ---------------------------------------------------------------------------
# Stage 2: runtime (non-root nginx)
# ---------------------------------------------------------------------------
FROM nginxinc/nginx-unprivileged:1.27-alpine

ARG GIT_COMMIT=dev
ARG BUILD_TIME=dev

LABEL org.opencontainers.image.source="https://github.com/OpenG2P/awe"
LABEL org.opencontainers.image.title="OpenG2P AWE Admin UI"
LABEL org.opencontainers.image.revision="${GIT_COMMIT}"
LABEL org.opencontainers.image.created="${BUILD_TIME}"

COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=builder /app/dist /usr/share/nginx/html

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD wget --quiet --spider http://localhost:8080/ || exit 1
