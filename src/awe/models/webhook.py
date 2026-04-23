"""Outbound webhook delivery queue (DB-as-queue)."""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin, new_uuid


class WebhookDelivery(Base, TimestampMixin):
    """One outbound webhook attempt (pending / in-flight / terminal).

    A single approval_event produces exactly one delivery row. The dispatcher
    worker claims `pending` rows whose `next_attempt_at` has passed, POSTs to
    the caller, and updates status.
    """

    __tablename__ = "webhook_delivery"
    __table_args__ = (
        Index("idx_delivery_status_next", "status", "next_attempt_at"),
        Index("idx_delivery_event", "event_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    event_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("approval_event.id", ondelete="CASCADE"), nullable=False
    )
    url: Mapped[str] = mapped_column(Text, nullable=False)

    # Attempt counter — increments on each try.
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # pending | delivered | failed | exhausted
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    last_attempt_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    last_status_code: Mapped[Optional[int]] = mapped_column(Integer)
    last_error: Mapped[Optional[str]] = mapped_column(Text)
