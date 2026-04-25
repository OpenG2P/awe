"""Policy-side request/response schemas."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, field_validator


VALID_RULE_TYPES = {"user", "role", "group", "expression", "http"}
VALID_RULE_KINDS = {"approver", "observer"}
VALID_MODES = {"all", "any-n", "quorum", "percentage"}
VALID_ON_EMPTY = {"skip", "block"}
VALID_ON_BREACH = {"notify", "auto_approve", "auto_reject", "escalate"}


class ApproverRuleIn(BaseModel):
    rule_type: str = Field(..., examples=["role"])
    rule_value: Dict[str, Any] = Field(..., examples=[{"role": "district-officer"}])
    kind: str = Field(
        default="approver",
        description="'approver' counts toward stage completion; 'observer' gets a comment-only task.",
    )
    required: bool = Field(
        default=False,
        description="If true, every user resolved by this rule must approve (overrides quorum).",
    )

    @field_validator("rule_type")
    @classmethod
    def _check_rule_type(cls, v: str) -> str:
        if v not in VALID_RULE_TYPES:
            raise ValueError(f"rule_type must be one of {sorted(VALID_RULE_TYPES)}")
        return v

    @field_validator("kind")
    @classmethod
    def _check_kind(cls, v: str) -> str:
        if v not in VALID_RULE_KINDS:
            raise ValueError(f"kind must be one of {sorted(VALID_RULE_KINDS)}")
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
    parallel_group: Optional[int] = Field(
        default=None,
        description=(
            "Stages sharing a parallel_group run concurrently and must all "
            "approve before the group completes. Null = stage is its own "
            "group (strictly sequential)."
        ),
    )
    on_breach: Optional[str] = Field(
        default=None,
        description=(
            "What to do when every open task in the stage has crossed its "
            "due_at: 'notify' (default behaviour) / 'auto_approve' / "
            "'auto_reject' / 'escalate'."
        ),
    )
    escalation_rules: List[ApproverRuleIn] = Field(
        default_factory=list,
        description="Rules resolved to add fresh approvers when on_breach='escalate'.",
    )
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

    @field_validator("on_breach")
    @classmethod
    def _check_on_breach(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        if v not in VALID_ON_BREACH:
            raise ValueError(f"on_breach must be one of {sorted(VALID_ON_BREACH)}")
        return v


class StageOut(StageIn):
    id: str
    rules: List[ApproverRuleOut] = Field(default_factory=list)
    escalation_rules: List[ApproverRuleOut] = Field(default_factory=list)


class PolicyCreate(BaseModel):
    policy_key: str = Field(..., examples=["registry.change_request.v1"])
    name: str = Field(..., examples=["Registry CR approval"])
    description: Optional[str] = None
    artifact_type: str = Field(..., examples=["registry.change_request"])
    forbid_self_approval: bool = Field(
        default=False,
        description="Filter the request's requester out of every stage's approver list.",
    )
    forbid_repeat_approvers: bool = Field(
        default=False,
        description="Filter users who approved an earlier stage out of later stages.",
    )
    stages: List[StageIn] = Field(default_factory=list)


class PolicyOut(BaseModel):
    id: str
    policy_key: str
    version: int
    name: str
    description: Optional[str] = None
    status: str
    artifact_type: str
    forbid_self_approval: bool = False
    forbid_repeat_approvers: bool = False
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
