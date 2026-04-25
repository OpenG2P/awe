"""User delegation (out-of-office) schemas."""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field, model_validator


class DelegationCreate(BaseModel):
    user_id: str = Field(..., examples=["u-alice"], min_length=1)
    delegate_to: str = Field(..., examples=["u-bob"], min_length=1)
    starts_at: datetime
    ends_at: datetime
    reason: Optional[str] = None

    @model_validator(mode="after")
    def _check_window(self):
        if self.ends_at <= self.starts_at:
            raise ValueError("ends_at must be strictly after starts_at")
        if self.user_id == self.delegate_to:
            raise ValueError("delegate_to must differ from user_id")
        return self


class DelegationOut(BaseModel):
    id: str
    user_id: str
    delegate_to: str
    starts_at: datetime
    ends_at: datetime
    reason: Optional[str] = None
    created_by: Optional[str] = None
    created_at: datetime
    updated_at: datetime
