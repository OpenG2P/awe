"""Schemas for admin/ops endpoints (webhook deliveries, audit log, etc.)."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional

from pydantic import BaseModel, ConfigDict, Field


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


class KeycloakUserOut(BaseModel):
    user_id: str = Field(..., description="Task assignee id (preferred_username)")
    username: str
    email: Optional[str] = None
    name: Optional[str] = None


class KeycloakClientOut(BaseModel):
    client_id: str
    name: str


class KeycloakRoleOut(BaseModel):
    name: str
    client: Optional[str] = Field(
        default=None,
        description="clientId when this is a client role; null for realm roles.",
    )
    description: Optional[str] = None


class KeycloakGroupOut(BaseModel):
    path: str
    name: str


class AuditActionOut(BaseModel):
    # `metadata` clashes with BaseModel's own `metadata`; source attribute is
    # `metadata_` on the ORM model, exposed as `metadata` in the response.
    model_config = ConfigDict(populate_by_name=True)

    id: str
    occurred_at: datetime
    actor: str
    actor_email: Optional[str] = None
    action: str
    resource_type: str
    resource_id: str
    summary: Optional[str] = None
    before: Optional[Dict[str, Any]] = None
    after: Optional[Dict[str, Any]] = None
    metadata: Optional[Dict[str, Any]] = Field(default=None, alias="metadata_")
