"""Health, version, config endpoints."""

from __future__ import annotations

import importlib.metadata
import os

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from sqlalchemy import text

from ..config import get_settings
from ..db import get_engine
from ..schemas.common import (
    HealthPayload,
    HealthResponse,
    VersionPayload,
    VersionResponse,
    make_envelope,
    make_error_response,
    now_iso,
)

router = APIRouter(prefix="/v1/awe", tags=["health"])


@router.get(
    "/health",
    summary="Health / readiness probe",
    response_model=HealthResponse,
    responses={
        200: {"model": HealthResponse, "description": "Service is ready."},
        503: {"description": "Not ready (AWE-005 startup; AWE-006 db)."},
    },
)
async def health():
    from ..main import is_startup_complete

    if not is_startup_complete():
        return JSONResponse(
            status_code=503,
            content=make_error_response("AWE-005", "Service not ready: startup not complete"),
        )

    try:
        engine = get_engine()
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception as e:  # noqa: BLE001
        return JSONResponse(
            status_code=503,
            content=make_error_response("AWE-006", f"Database health check failed: {e}"),
        )

    return HealthResponse(responsetime=now_iso(), response=HealthPayload(status="UP"))


@router.get(
    "/version",
    summary="Service version + build metadata",
    response_model=VersionResponse,
)
async def version():
    try:
        svc_version = importlib.metadata.version("openg2p-awe")
    except importlib.metadata.PackageNotFoundError:
        svc_version = "0.1.0-dev"
    return VersionResponse(
        responsetime=now_iso(),
        response=VersionPayload(
            service_version=svc_version,
            build_time=os.environ.get("BUILD_TIME", "dev"),
            git_commit=os.environ.get("GIT_COMMIT", "dev"),
        ),
    )


@router.get(
    "/config",
    summary="Effective non-sensitive configuration",
)
async def config_view():
    cfg = get_settings().awe
    return make_envelope(
        {
            "service_id": cfg.service_id,
            "api_version": cfg.api_version,
            "module": cfg.module,
            "webhook": {
                "max_attempts": cfg.webhook.max_attempts,
                "backoff_seconds": cfg.webhook.backoff_seconds,
                "timeout_seconds": cfg.webhook.timeout_seconds,
            },
            "sla": cfg.sla.model_dump(),
            "admin_ui": cfg.admin_ui.model_dump(),
        }
    )
