"""Shared response-envelope primitives — matches the OpenG2P id-generator / audit-manager convention."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, List, Optional

from pydantic import BaseModel, Field


SERVICE_ENVELOPE_ID = "openg2p.awe"
ENVELOPE_VERSION = "1.0"


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


class ErrorDetail(BaseModel):
    errorCode: str = Field(
        ...,
        description=(
            "OpenG2P-assigned error code. AWE catalog: "
            "`AWE-001` (policy not found), "
            "`AWE-002` (policy conflict / version clash), "
            "`AWE-003` (request not found), "
            "`AWE-004` (task not found), "
            "`AWE-005` (service not ready — startup incomplete), "
            "`AWE-006` (database health check failed), "
            "`AWE-007` (invalid state transition), "
            "`AWE-008` (unauthorized / forbidden), "
            "`AWE-009` (idempotency key conflict with different payload), "
            "`AWE-010` (validation — bad policy definition)."
        ),
        examples=["AWE-003"],
    )
    message: str = Field(..., examples=["Approval request not found"])


class EnvelopeBase(BaseModel):
    id: str = Field(default=SERVICE_ENVELOPE_ID, examples=[SERVICE_ENVELOPE_ID])
    version: str = Field(default=ENVELOPE_VERSION, examples=[ENVELOPE_VERSION])
    responsetime: str = Field(..., examples=["2026-04-23T10:00:00.000Z"])


class HealthPayload(BaseModel):
    status: str = Field(..., examples=["UP"])


class HealthResponse(EnvelopeBase):
    response: HealthPayload
    errors: List[ErrorDetail] = Field(default_factory=list)


class VersionPayload(BaseModel):
    service_version: str = Field(..., examples=["0.1.0"])
    build_time: str = Field(..., examples=["2026-04-23T08:30:00.000Z"])
    git_commit: str = Field(..., examples=["a1b2c3d"])


class VersionResponse(EnvelopeBase):
    response: VersionPayload
    errors: List[ErrorDetail] = Field(default_factory=list)


class ErrorResponse(EnvelopeBase):
    response: Optional[dict] = Field(default=None)
    errors: List[ErrorDetail] = Field(...)


def make_envelope(response_data: Any) -> dict:
    return {
        "id": SERVICE_ENVELOPE_ID,
        "version": ENVELOPE_VERSION,
        "responsetime": now_iso(),
        "response": response_data,
        "errors": [],
    }


def make_error_response(error_code: str, message: str) -> dict:
    return {
        "id": SERVICE_ENVELOPE_ID,
        "version": ENVELOPE_VERSION,
        "responsetime": now_iso(),
        "response": None,
        "errors": [{"errorCode": error_code, "message": message}],
    }
