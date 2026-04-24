"""
Append-only audit-trail recording for admin / ops actions.

Controllers call `record(...)` after a mutation succeeds; the row is
written within the same DB transaction so audit writes ride along with
the change they describe.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from ..models import AuditAction
from .auth import CallerIdentity


async def record(
    session: AsyncSession,
    *,
    identity: CallerIdentity,
    action: str,
    resource_type: str,
    resource_id: str,
    summary: Optional[str] = None,
    before: Optional[dict[str, Any]] = None,
    after: Optional[dict[str, Any]] = None,
    metadata: Optional[dict[str, Any]] = None,
) -> AuditAction:
    """Append a single audit row in the current session."""
    row = AuditAction(
        occurred_at=datetime.now(timezone.utc),
        actor=identity.subject,
        actor_email=identity.raw_claims.get("email")
        if identity.raw_claims
        else None,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        before=before,
        after=after,
        metadata_=metadata,
        summary=summary,
    )
    session.add(row)
    await session.flush()
    return row
