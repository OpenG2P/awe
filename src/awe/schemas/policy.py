"""Policy-side request/response schemas."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, field_validator


VALID_RULE_TYPES = {"user", "role", "group", "expression", "http"}
VALID_MODES = {"all", "any-n", "quorum", "percentage"}
VALID_ON_EMPTY = {"skip", "block"}


class ApproverRuleIn(BaseModel):
    rule_type: str = Field(..., examples=["role"])
    rule_value: Dict[str, Any] = Field(..., examples=[{"role": "district-officer"}])

    @field_validator("rule_type")
    @classmethod
    def _check_rule_type(cls, v: str) -> str:
        if v not in VALID_RULE_TYPES:
            raise ValueError(f"rule_type must be one of {sorted(VALID_RULE_TYPES)}")
        return v


class ApproverRuleOut(ApproverRuleIn):
    id: str


class StageIn(BaseModel):
    name: str = Field(..., examples=["District officers"])
    stage_order: int = Field(..., ge=1)
    mode: str = Field(default="all", examples=["any-n"])
    mode_value: Optional[int] = Field(default=None, ge=1)
    sla_hours: Optional[int] = Field(default=None, ge=1)
    skip_if: Optional[Dict[str, Any]] = None
    on_empty: str = Field(default="block")
    rules: List[ApproverRuleIn] = Field(default_factory=list)

    @field_validator("mode")
    @classmethod
    def _normalize_mode(cls, v: str) -> str:
        # Accept both `any-N` and `any-n` spellings; normalize to lowercase.
        v_lower = v.lower()
        if v_lower not in VALID_MODES:
            raise ValueError(f"mode must be one of {sorted(VALID_MODES)}")
        return v_lower

    @field_validator("on_empty")
    @classmethod
    def _check_on_empty(cls, v: str) -> str:
        if v not in VALID_ON_EMPTY:
            raise ValueError(f"on_empty must be one of {sorted(VALID_ON_EMPTY)}")
        return v


class StageOut(StageIn):
    id: str
    rules: List[ApproverRuleOut] = Field(default_factory=list)


class PolicyCreate(BaseModel):
    policy_key: str = Field(..., examples=["registry.change_request.v1"])
    name: str = Field(..., examples=["Registry CR approval"])
    description: Optional[str] = None
    artifact_type: str = Field(..., examples=["registry.change_request"])
    stages: List[StageIn] = Field(default_factory=list)


class PolicyOut(BaseModel):
    id: str
    policy_key: str
    version: int
    name: str
    description: Optional[str] = None
    status: str
    artifact_type: str
    created_by: Optional[str] = None
    created_at: datetime
    updated_at: datetime
    stages: List[StageOut] = Field(default_factory=list)


class PolicyVersionOut(BaseModel):
    id: str
    policy_key: str
    version: int
    status: str
    created_at: datetime


class SimulateRequest(BaseModel):
    context: Dict[str, Any] = Field(
        default_factory=dict, examples=[{"district": "D1", "amount": 15000}]
    )


class SimulateStageOut(BaseModel):
    stage_order: int
    name: str
    mode: str
    mode_value: Optional[int] = None
    resolved_approvers: List[str]
    skipped: bool = False
    skip_reason: Optional[str] = None


class SimulateResponse(BaseModel):
    policy_id: str
    policy_version: int
    stages: List[SimulateStageOut]
