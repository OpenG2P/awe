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
  BEGIN
    CREATE SCHEMA IF NOT EXISTS public;
  EXCEPTION WHEN OTHERS THEN
    NULL;
  END;

  BEGIN
    EXECUTE 'GRANT USAGE, CREATE ON SCHEMA public TO CURRENT_USER';
  EXCEPTION WHEN OTHERS THEN
    NULL;
  END;

  IF NOT has_schema_privilege(current_user, 'public', 'CREATE') THEN
    EXECUTE 'CREATE SCHEMA IF NOT EXISTS awe AUTHORIZATION CURRENT_USER';
  END IF;
END
$awe$;
"""


def _create_all(sync_conn) -> None:
    if sync_conn.dialect.name == "postgresql":
        sync_conn.exec_driver_sql(_PG_ENSURE_SCHEMA)
        sync_conn.exec_driver_sql("SET search_path TO public, awe")
        # gin_trgm_ops on approval_task.search_text. App users cannot always
        # CREATE EXTENSION; postgres-init installs pg_trgm as superuser.
        nested = sync_conn.begin_nested()
        try:
            sync_conn.exec_driver_sql("CREATE EXTENSION IF NOT EXISTS pg_trgm")
            nested.commit()
        except Exception:
            nested.rollback()
            logger.warning("Could not CREATE EXTENSION pg_trgm; it must already exist")
    Base.metadata.create_all(sync_conn)


async def create_schema(engine: AsyncEngine) -> None:
    """Create all tables and indexes if they don't already exist."""
    async with engine.begin() as conn:
        await conn.run_sync(_create_all)
    logger.info("AWE schema ensured (create_all, idempotent)")
