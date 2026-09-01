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
from .delegation import UserDelegation
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
    "UserDelegation",
    "create_schema",
]

# postgres-init users often have an empty search_path (PG15+ revokes CREATE
# on public from PUBLIC). SET inside a DO block does not persist, so GRANT /
# CREATE SCHEMA here and SET search_path on the same connection as create_all.
_PG_ENSURE_SCHEMA = """
DO $awe$
BEGIN
  -- Ensure public schema exists
  BEGIN
    CREATE SCHEMA IF NOT EXISTS public;
  EXCEPTION WHEN OTHERS THEN
    NULL;
  END;

  -- Grant CREATE on public schema to current user
  BEGIN
    EXECUTE 'GRANT ALL ON SCHEMA public TO CURRENT_USER';
  EXCEPTION WHEN OTHERS THEN
    NULL;
  END;

  -- Grant default privileges on public schema
  BEGIN
    EXECUTE 'ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO CURRENT_USER';
  EXCEPTION WHEN OTHERS THEN
    NULL;
  END;
END
$awe$;
"""


def _create_all(sync_conn) -> None:
    if sync_conn.dialect.name == "postgresql":
        sync_conn.exec_driver_sql(_PG_ENSURE_SCHEMA)
        sync_conn.exec_driver_sql("SET search_path TO public")
    Base.metadata.create_all(sync_conn)


async def create_schema(engine: AsyncEngine) -> None:
    """Create all tables and indexes if they don't already exist."""
    async with engine.begin() as conn:
        await conn.run_sync(_create_all)
    logger.info("AWE schema ensured (create_all, idempotent)")
