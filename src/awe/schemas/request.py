"""Runtime (request / task / decision / event) schemas."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, field_validator


VALID_DECISION_ACTIONS = {"approve", "reject", "abstain"}


class CreateRequestIn(BaseModel):
    policy_key: str = Field(..., examples=["registry.change_request.v1"])
    artifact_type: str = Field(..., examples=["registry.change_request"])
    artifact_id: str = Field(..., examples=["cr-42"])
    context: Dict[str, Any] = Field(
        default_factory=dict, examples=[{"district": "D1", "amount": 15000}]
    )
    callback_url: Optional[str] = Field(
        default=None, examples=["https://registry/internal/approval-callbacks"]
    )
    callback_secret_id: Optional[str] = None
    requester: Optional[str] = Field(default=None, examples=["u-alice"])


class TaskOut(BaseModel):
    id: str
    request_id: str
    stage_id: str
    stage_order: int
    assignee: str
    status: str
    claimed_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    due_at: Optional[datetime] = None
    decision_id: Optional[str] = None
    created_at: datetime


class CreateRequestOut(BaseModel):
    request_id: str
    status: str
    current_stage_order: int
    tasks: List[TaskOut] = Field(default_factory=list)


class RequestOut(BaseModel):
    id: str
    policy_id: str
    policy_key: str
    policy_version: int
    artifact_type: str
    artifact_id: str
    source_service: str
    requester: Optional[str] = None
    context: Dict[str, Any]
    status: str
    current_stage_order: int
    callback_url: Optional[str] = None
    completed_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime


class CancelRequest(BaseModel):
    reason: Optional[str] = None
    actor: Optional[str] = None


class DecisionIn(BaseModel):
    action: str = Field(..., examples=["approve"])
    comment: Optional[str] = None
    attachments_ref: Optional[str] = None

    @field_validator("action")
    @classmethod
    def _check_action(cls, v: str) -> str:
        if v not in VALID_DECISION_ACTIONS:
            raise ValueError(
                f"action must be one of {sorted(VALID_DECISION_ACTIONS)}"
            )
        return v


class DecisionOut(BaseModel):
    id: str
    request_id: str
    task_id: str
    stage_order: int
    actor: str
    action: str
    comment: Optional[str] = None
    attachments_ref: Optional[str] = None
    created_at: datetime


class EventOut(BaseModel):
    id: str
    request_id: str
    event_type: str
    payload: Dict[str, Any]
    created_at: datetime
