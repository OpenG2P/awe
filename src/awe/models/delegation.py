"""User delegation (out-of-office) table.

When AWE is about to create a task for user X and an active delegation exists
(`user_id = X`, window covering `now`), the task is created for `delegate_to`
instead, with `delegated_from = X` for audit.

Overlapping delegations for the same user are resolved by picking the most
recently created one — operators maintain this through the admin UI.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin, new_uuid


class UserDelegation(Base, TimestampMixin):
    __tablename__ = "user_delegation"
    __table_args__ = (
        Index("idx_delegation_user_window", "user_id", "starts_at", "ends_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    user_id: Mapped[str] = mapped_column(String(128), nullable=False)
    delegate_to: Mapped[str] = mapped_column(String(128), nullable=False)
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    reason: Mapped[Optional[str]] = mapped_column(Text)
    created_by: Mapped[Optional[str]] = mapped_column(String(128))
