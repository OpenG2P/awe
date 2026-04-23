#!/bin/sh
# Entrypoint for OpenG2P Approval Workflow Engine.
# exec so uvicorn receives SIGTERM directly from Docker for graceful shutdown.

exec uvicorn awe.main:app \
    --host "${UVICORN_HOST:-0.0.0.0}" \
    --port "${UVICORN_PORT:-8000}" \
    --workers "${UVICORN_WORKERS:-1}" \
    --log-level "${UVICORN_LOG_LEVEL:-info}" \
    --loop asyncio
