"""
Approval requests — service-to-service runtime endpoints.

Auth: any valid Keycloak token (Caller services use client_credentials
service tokens that don't carry user roles). Cancel additionally requires
`AWE_ADMIN` since destructive ops on shared state warrant a higher bar.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Header, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.exc import IntegrityError

from ..db import get_db
from ..models import (
    ApprovalEvent,
    ApprovalRequest,
    ApprovalTask,
    IdempotencyKey,
)
from ..schemas.request import (
    CancelRequest,
    CreateRequestIn,
    CreateRequestOut,
    EventOut,
    RequestOut,
)
from ..schemas.responses import (
    ResponseBadCreateRequest,
    ResponseForbiddenAdmin,
    ResponsePolicyKeyHasNoActive,
    ResponseRequestNotFound,
    ResponseStateConflict,
    ResponseUnauthorized,
)
from ..services import audit as audit_svc
from ..services import engine as engine_svc
from ..services import policy as policy_svc
from ..services.auth import CallerIdentity, current_identity, require_role
from ._helpers import (
    error,
    event_to_out,
    request_to_out,
    tasks_to_out,
)

router = APIRouter(prefix="/v1/awe/requests", tags=["requests"])


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    response_model=CreateRequestOut,
    summary="Create an approval request for a caller-owned artifact",
    responses={
        **ResponseUnauthorized,
        **ResponseBadCreateRequest,
        **ResponsePolicyKeyHasNoActive,
    },
)
async def create_request(
    payload: CreateRequestIn,
    identity: CallerIdentity = Depends(current_identity),
    session: AsyncSession = Depends(get_db),
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
):
    if idempotency_key:
        existing = await session.get(IdempotencyKey, idempotency_key)
        if existing is not None:
            return existing.response_payload

    policy = await policy_svc.get_active(session, payload.policy_key)
    if policy is None:
        return error(404, "AWE-001", f"No active policy for '{payload.policy_key}'")

    try:
        request = await engine_svc.start_request(
            session=session,
            policy=policy,
            artifact_type=payload.artifact_type,
            artifact_id=payload.artifact_id,
            source_service=identity.subject,
            context=payload.context,
            callback_url=payload.callback_url,
            callback_secret_id=payload.callback_secret_id,
            requester=payload.requester or identity.subject,
        )
    except engine_svc.EngineError as e:
        return error(400, "AWE-007", str(e))

    tasks_rows = await session.execute(
        select(ApprovalTask).where(
            ApprovalTask.request_id == request.id,
            ApprovalTask.stage_order == request.current_stage_order,
        )
    )
    tasks = list(tasks_rows.scalars())

    out = CreateRequestOut(
        request_id=request.id,
        status=request.status,
        current_stage_order=request.current_stage_order,
        tasks=tasks_to_out(tasks),
    )
    response_payload = json.loads(out.model_dump_json())

    if idempotency_key:
        session.add(
            IdempotencyKey(
                key=idempotency_key,
                response_payload=response_payload,
                created_at=datetime.now(timezone.utc),
            )
        )
        try:
            await session.flush()
        except IntegrityError:
            # Concurrent retry inserted the same key — fall through.
            await session.rollback()

    return response_payload


@router.get(
    "/{request_id}",
    response_model=RequestOut,
    summary="Fetch an approval request by id",
    responses={**ResponseUnauthorized, **ResponseRequestNotFound},
)
async def get_request(
    request_id: str,
    identity: CallerIdentity = Depends(current_identity),
    session: AsyncSession = Depends(get_db),
):
    request = await session.get(ApprovalRequest, request_id)
    if request is None:
        return error(404, "AWE-003", f"Request {request_id} not found")
    return request_to_out(request)


@router.get(
    "",
    response_model=list[RequestOut],
    summary="Search requests by artifact reference and/or status",
    responses={**ResponseUnauthorized},
)
async def search_requests(
    artifact_type: Optional[str] = Query(default=None),
    artifact_id: Optional[str] = Query(default=None),
    status_filter: Optional[str] = Query(default=None, alias="status"),
    limit: int = Query(default=50, ge=1, le=500),
    identity: CallerIdentity = Depends(current_identity),
    session: AsyncSession = Depends(get_db),
):
    stmt = select(ApprovalRequest).order_by(ApprovalRequest.created_at.desc())
    if artifact_type:
        stmt = stmt.where(ApprovalRequest.artifact_type == artifact_type)
    if artifact_id:
        stmt = stmt.where(ApprovalRequest.artifact_id == artifact_id)
    if status_filter:
        stmt = stmt.where(ApprovalRequest.status == status_filter)
    stmt = stmt.limit(limit)
    rows = await session.execute(stmt)
    return [request_to_out(r) for r in rows.scalars()]


@router.post(
    "/{request_id}/cancel",
    response_model=RequestOut,
    summary="Cancel an in-flight approval request (admin only)",
    responses={
        **ResponseUnauthorized,
        **ResponseForbiddenAdmin,
        **ResponseRequestNotFound,
        **ResponseStateConflict,
    },
)
async def cancel_request(
    request_id: str,
    payload: CancelRequest,
    identity: CallerIdentity = Depends(require_role("AWE_ADMIN")),
    session: AsyncSession = Depends(get_db),
):
    request = await session.get(ApprovalRequest, request_id)
    if request is None:
        return error(404, "AWE-003", f"Request {request_id} not found")
    before_status = request.status
    try:
        await engine_svc.cancel_request(
            session, request, actor=identity.subject, reason=payload.reason
        )
    except engine_svc.EngineError as e:
        return error(409, "AWE-007", str(e))
    await audit_svc.record(
        session,
        identity=identity,
        action="request.cancel",
        resource_type="request",
        resource_id=request.id,
        summary=(
            f"Cancelled {request.artifact_type}/{request.artifact_id}"
            + (f" ({payload.reason})" if payload.reason else "")
        ),
        before={"status": before_status},
        after={"status": "cancelled"},
        metadata={"reason": payload.reason, "artifact_id": request.artifact_id},
    )
    return request_to_out(request)


@router.get(
    "/{request_id}/events",
    response_model=list[EventOut],
    summary="Timeline of every event for a request (audit log)",
    responses={**ResponseUnauthorized},
)
async def request_events(
    request_id: str,
    identity: CallerIdentity = Depends(current_identity),
    session: AsyncSession = Depends(get_db),
):
    rows = await session.execute(
        select(ApprovalEvent)
        .where(ApprovalEvent.request_id == request_id)
        .order_by(ApprovalEvent.created_at.asc())
    )
    return [event_to_out(e) for e in rows.scalars()]
