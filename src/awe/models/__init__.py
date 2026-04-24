"""
SQLAlchemy ORM models.

Schema is created idempotently at service startup via `create_schema()` —
matching the audit-manager pattern (no Alembic). The `postgres-init` helm
subchart provisions the database + user; this service creates the tables.
"""

from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncEngine

from .audit import AuditAction
from .base import Base
from .policy import ApprovalPolicy, ApprovalStage, ApproverRule
from .request import ApprovalDecision, ApprovalEvent, ApprovalRequest, ApprovalTask
from .secret import CallbackSecret, IdempotencyKey
from .webhook import WebhookDelivery

logger = logging.getLogger(__name__)

__all__ = [
    "Base",
    "ApprovalPolicy",
    "ApprovalStage",
    "ApproverRule",
    "ApprovalRequest",
    "ApprovalTask",
    "ApprovalDecision",
    "ApprovalEvent",
    "WebhookDelivery",
    "CallbackSecret",
    "IdempotencyKey",
    "AuditAction",
    "create_schema",
]


async def create_schema(engine: AsyncEngine) -> None:
    """Create all tables and indexes if they don't already exist."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("AWE schema ensured (create_all, idempotent)")
