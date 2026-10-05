#!/bin/sh
# Entrypoint for OpenG2P Approval Workflow Engine.
# exec so gunicorn receives SIGTERM directly from Docker for graceful shutdown.

exec ${AWE_WORKER_TYPE:-gunicorn} "awe.main:app" \
    --workers ${AWE_NO_OF_WORKERS:-2} \
    --worker-class uvicorn.workers.UvicornWorker \
    --bind ${AWE_HOST:-0.0.0.0}:${AWE_PORT:-8000} \
    --log-level info
