"""
User delegation (out-of-office) endpoints.

Delegations redirect tasks from `user_id` to `delegate_to` for any request
created while the window (starts_at, ends_at) is active. Task creation looks
up the most-recent active delegation for each resolved approver and rewrites
the assignee.

Auth:
  * Viewers can list delegations (read-only visibility into who's delegating).
  * Admins create and delete.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_db
from ..models import UserDelegation
from ..schemas.delegation import DelegationCreate, DelegationOut
from ..schemas.responses import (
    ResponseDelegationNotFound,
    ResponseForbiddenAdmin,
    ResponseForbiddenViewerOrAdmin,
    ResponseUnauthorized,
)
from ..services import audit as audit_svc
from ..services.auth import (
    ROLE_ADMIN,
    ROLE_VIEWER,
    CallerIdentity,
    require_role,
    require_role_any,
)
from ._helpers import error

router = APIRouter(prefix="/v1/awe/delegations", tags=["delegations"])


def _to_out(d: UserDelegation) -> DelegationOut:
    return DelegationOut(
        id=d.id,
        user_id=d.user_id,
        delegate_to=d.delegate_to,
        starts_at=d.starts_at,
        ends_at=d.ends_at,
        reason=d.reason,
        created_by=d.created_by,
        created_at=d.created_at,
        updated_at=d.updated_at,
    )


@router.get(
    "",
    response_model=list[DelegationOut],
    summary="List delegations — filter by user",
    responses={**ResponseUnauthorized, **ResponseForbiddenViewerOrAdmin},
)
async def list_delegations(
    user_id: Optional[str] = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    identity: CallerIdentity = Depends(require_role_any(ROLE_VIEWER, ROLE_ADMIN)),
    session: AsyncSession = Depends(get_db),
):
    stmt = select(UserDelegation).order_by(UserDelegation.starts_at.desc()).limit(limit)
    if user_id:
        stmt = stmt.where(UserDelegation.user_id == user_id)
    rows = await session.execute(stmt)
    return [_to_out(d) for d in rows.scalars()]


@router.post(
    "",
    response_model=DelegationOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create a delegation window (admin only)",
    responses={**ResponseUnauthorized, **ResponseForbiddenAdmin},
)
async def create_delegation(
    payload: DelegationCreate,
    identity: CallerIdentity = Depends(require_role("AWE_ADMIN")),
    session: AsyncSession = Depends(get_db),
):
    delegation = UserDelegation(
        user_id=payload.user_id,
        delegate_to=payload.delegate_to,
        starts_at=payload.starts_at,
        ends_at=payload.ends_at,
        reason=payload.reason,
        created_by=identity.subject,
    )
    session.add(delegation)
    await session.flush()

    await audit_svc.record(
        session,
        identity=identity,
        action="delegation.create",
        resource_type="delegation",
        resource_id=delegation.id,
        summary=(
            f"Delegate {payload.user_id} → {payload.delegate_to} "
            f"({payload.starts_at.isoformat()} — {payload.ends_at.isoformat()})"
        ),
        before=None,
        after={
            "user_id": payload.user_id,
            "delegate_to": payload.delegate_to,
            "starts_at": payload.starts_at.isoformat(),
            "ends_at": payload.ends_at.isoformat(),
        },
        metadata={"reason": payload.reason},
    )
    return _to_out(delegation)


@router.delete(
    "/{delegation_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a delegation (admin only)",
    responses={
        **ResponseUnauthorized,
        **ResponseForbiddenAdmin,
        **ResponseDelegationNotFound,
    },
)
async def delete_delegation(
    delegation_id: str,
    identity: CallerIdentity = Depends(require_role("AWE_ADMIN")),
    session: AsyncSession = Depends(get_db),
):
    delegation = await session.get(UserDelegation, delegation_id)
    if delegation is None:
        return error(404, "AWE-004", f"Delegation {delegation_id} not found")
    before = {
        "user_id": delegation.user_id,
        "delegate_to": delegation.delegate_to,
        "starts_at": delegation.starts_at.isoformat(),
        "ends_at": delegation.ends_at.isoformat(),
    }
    await session.delete(delegation)
    await session.flush()
    await audit_svc.record(
        session,
        identity=identity,
        action="delegation.delete",
        resource_type="delegation",
        resource_id=delegation_id,
        summary=f"Deleted delegation {delegation.user_id} → {delegation.delegate_to}",
        before=before,
        after=None,
    )
    return None
