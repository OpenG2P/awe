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

from fastapi import FastAPI, Header
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
from .schemas.callback import WebhookEvent
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


# Top-level tag metadata — order here drives rendering order in Swagger /
# ReDoc / GitBook. Descriptions show up under each section heading; they
# also enable tag-based grouping + collapsing in GitBook's OpenAPI viewer.
openapi_tags = [
    {
        "name": "policies",
        "description": (
            "Policy CRUD, versioning, activation, and simulation. **Admin "
            "surface only** — used by the bundled admin SPA and GitOps "
            "tooling. Caller services should never call these endpoints."
        ),
    },
    {
        "name": "requests",
        "description": (
            "Service-to-service runtime endpoints. Callers POST here when "
            "an artifact (CR, disbursement, …) is created, cancel when the "
            "underlying artifact is withdrawn, and read the audit timeline "
            "for display."
        ),
    },
    {
        "name": "tasks",
        "description": (
            "Approver-facing endpoints — list inbox, claim, submit a "
            "decision. The caller service proxies these on behalf of the "
            "end-user approver; approvers never talk to AWE directly."
        ),
    },
    {
        "name": "webhooks",
        "description": (
            "Outbound from AWE to the caller's `callback_url`. Declared "
            "here so callers can read the body schema + signed-header "
            "contract from the same OpenAPI spec as the rest of the API. "
            "AWE never invokes its own webhook — implementation lives in "
            "`awe.workers.webhook_dispatcher`."
        ),
    },
    {
        "name": "health",
        "description": (
            "Service-level endpoints — liveness/readiness probe, build "
            "metadata, effective non-sensitive configuration. Unauthenticated."
        ),
    },
]

app = FastAPI(
    title="OpenG2P Approval Workflow Engine",
    description=(
        "Generic, configurable multi-stage approval workflow engine for "
        "OpenG2P modules. Caller services post artifacts; AWE resolves "
        "stages and approvers, then notifies the caller via signed webhook "
        "callbacks when state changes."
    ),
    version="0.1.0",
    openapi_tags=openapi_tags,
    lifespan=lifespan,
    docs_url="/v1/awe/docs",
    redoc_url="/v1/awe/redoc",
    openapi_url="/v1/awe/openapi.json",
)

app.include_router(health_router)
app.include_router(policy_router)
app.include_router(request_router)
app.include_router(task_router)


# ---------------------------------------------------------------------------
# Outbound webhook contract (OpenAPI 3.1 `webhooks` section)
# ---------------------------------------------------------------------------
# These declarations are NEVER invoked by AWE — they exist purely to surface
# the outbound contract in the generated OpenAPI spec, so callers can read
# the body schema + signed headers from the same artifact they read the rest
# of the API from. Implementation lives in awe.services.webhook +
# awe.workers.webhook_dispatcher.
@app.webhooks.post(
    "approval-event",
    tags=["webhooks"],
    summary="Approval workflow state change",
    description=(
        "Sent by AWE to the caller's `callback_url` whenever a status-changing "
        "event occurs on an approval request — `request_created`, "
        "`stage_started`, `stage_completed`, `request_approved`, "
        "`request_rejected`, `request_cancelled`, or `task_expired`.\n\n"
        "**Signature scheme**: `X-Approval-Signature` is "
        "`sha256=` + HMAC-SHA256 over `<X-Approval-Timestamp>.<raw body>` "
        "using the per-caller shared secret. The caller must verify the "
        "signature, dedup on `X-Approval-Event-Id`, and return any 2xx "
        "within the configured timeout (default 10s). Non-2xx triggers "
        "retries on the schedule documented in functional-specifications "
        "(1m → 5m → 15m → 1h → 6h, ~27h total)."
    ),
)
def approval_callback(  # pragma: no cover — declaration only
    body: WebhookEvent,
    x_approval_event_id: str = Header(..., alias="X-Approval-Event-Id"),
    x_approval_timestamp: str = Header(..., alias="X-Approval-Timestamp"),
    x_approval_signature: str = Header(..., alias="X-Approval-Signature"),
):
    """The caller's handler returns 2xx to ACK; non-2xx triggers retries."""

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
