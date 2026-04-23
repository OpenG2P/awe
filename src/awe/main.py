"""
FastAPI application entrypoint for OpenG2P AWE.

Lifespan:
  Startup:
    1. Init Postgres engine.
    2. Create schema (idempotent — Base.metadata.create_all).
    3. Start webhook dispatcher loop.
    4. Start SLA monitor loop.
    5. Mark startup complete → /health returns 200.
  Shutdown:
    Reverse order — cancel workers, dispose DB engine.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from .config import get_settings
from .controllers import (
    health_router,
    policy_router,
    request_router,
    task_router,
)
from .db import dispose_engine, init_engine
from .models import create_schema
from .workers.sla_monitor import sla_monitor_loop
from .workers.webhook_dispatcher import webhook_dispatcher_loop

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

_startup_complete = False
_webhook_task: Optional[asyncio.Task] = None
_sla_task: Optional[asyncio.Task] = None


def is_startup_complete() -> bool:
    return _startup_complete


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _startup_complete, _webhook_task, _sla_task

    logger.info("Initialising database engine...")
    engine = init_engine()

    logger.info("Ensuring AWE schema...")
    await create_schema(engine)

    logger.info("Starting webhook dispatcher loop...")
    _webhook_task = asyncio.create_task(
        webhook_dispatcher_loop(engine), name="awe-webhook-dispatcher"
    )

    logger.info("Starting SLA monitor loop...")
    _sla_task = asyncio.create_task(sla_monitor_loop(engine), name="awe-sla-monitor")

    _startup_complete = True
    logger.info("Startup complete. /health will now return 200.")

    yield

    logger.info("Shutting down...")
    _startup_complete = False

    for task in (_sla_task, _webhook_task):
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    await dispose_engine()
    logger.info("Shutdown complete.")


app = FastAPI(
    title="OpenG2P Approval Workflow Engine",
    description=(
        "Generic, configurable multi-stage approval workflow engine for "
        "OpenG2P modules. Caller services post artifacts; AWE resolves "
        "stages and approvers, then notifies the caller via signed webhook "
        "callbacks when state changes."
    ),
    version="0.1.0",
    lifespan=lifespan,
    docs_url="/v1/awe/docs",
    redoc_url="/v1/awe/redoc",
    openapi_url="/v1/awe/openapi.json",
)

app.include_router(health_router)
app.include_router(policy_router)
app.include_router(request_router)
app.include_router(task_router)

# Mount the bundled admin SPA if present (built from `ui/` into
# `src/awe/admin_ui/static/`). Absent in dev — that's fine; the API still works.
_admin_cfg = get_settings().awe.admin_ui
_static_dir = Path(__file__).parent / "admin_ui" / "static"
if _admin_cfg.enabled and _static_dir.is_dir():
    app.mount(
        _admin_cfg.mount_path,
        StaticFiles(directory=_static_dir, html=True),
        name="admin-ui",
    )
