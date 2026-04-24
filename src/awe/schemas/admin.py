"""Schemas for admin/ops endpoints (webhook deliveries, etc.)."""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel


class DeliveryOut(BaseModel):
    id: str
    event_id: str
    request_id: str
    event_type: str
    url: str
    status: str
    attempt: int
    next_attempt_at: datetime
    last_attempt_at: Optional[datetime] = None
    last_status_code: Optional[int] = None
    last_error: Optional[str] = None
    created_at: datetime
    updated_at: datetime
