"""Per-caller HMAC secrets + idempotency keys."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from sqlalchemy import JSON, DateTime, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin, new_uuid


class CallbackSecret(Base, TimestampMixin):
    """HMAC signing key per caller service.

    The secret itself is stored hashed; a separate out-of-band channel delivers
    the raw secret to the caller when provisioned. Rotation creates a new row
    with `status=active` and flips the previous one to `rotated`.
    """

    __tablename__ = "callback_secret"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    caller_service: Mapped[str] = mapped_column(String(128), nullable=False)
    secret_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    # active | rotated | revoked
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    rotated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))


class IdempotencyKey(Base):
    """Replay-safe POST /v1/requests — keys survive long enough to dedup retries.

    The stored `response_payload` lets us replay the original 2xx response on a
    retried key, so the caller's retry policy doesn't accidentally create two
    approval requests for the same artifact.
    """

    __tablename__ = "idempotency_key"
    __table_args__ = (Index("idx_idempotency_created", "created_at"),)

    key: Mapped[str] = mapped_column(String(255), primary_key=True)
    response_payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
