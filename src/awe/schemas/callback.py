"""Outbound webhook payload schema."""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class WebhookEvent(BaseModel):
    """The body POSTed to the caller's `callback_url`.

    Headers (set by the dispatcher, not part of this body):
      X-Approval-Event-Id   — same as `event_id` below
      X-Approval-Signature  — `sha256=<hex>`, HMAC over the raw body
      X-Approval-Timestamp  — Unix seconds, included in the signed payload
    """

    event_id: str = Field(..., examples=["7f3e..."])
    event_type: str = Field(
        ...,
        examples=["request_approved"],
        description=(
            "request_created | stage_started | stage_completed | "
            "request_approved | request_rejected | request_cancelled"
        ),
    )
    request_id: str
    artifact_type: str
    artifact_id: str
    status: str
    stage_order: Optional[int] = None
    actor: Optional[str] = None
    occurred_at: datetime
