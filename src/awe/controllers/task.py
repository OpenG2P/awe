"""
Approver task endpoints — invoked by the Caller Svc on behalf of end users.

Task assignee matching uses `preferred_username`, then `username`, then `sub`.
Approvers can only act on tasks assigned to them; the policy editor can
override that via a `AWE_ADMIN` role token (intentional escape hatch for ops).
"""

from __future__ import annotations

from typing import Optional, Union

import math

from fastapi import APIRouter, Depends, Query, status
from fastapi.responses import JSONResponse
from sqlalchemy import String, cast, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ..db import get_db
from ..models import ApprovalDecision, ApprovalRequest, ApprovalTask
from ..models.base import utcnow
from ..schemas.responses import (
    ResponseForbiddenAdmin,
    ResponseForbiddenNotAssignee,
    ResponseRequestNotFound,
    ResponseStateConflict,
    ResponseTaskNotFound,
    ResponseUnauthorized
)
from ..schemas.request import (
    DecisionIn,
    DecisionOut,
    PagedTasksOut,
    ReassignTaskIn,
    TaskOut,
    TaskStatsOut,
)
from ..services import audit as audit_svc
from ..services import engine as engine_svc
from ..services.auth import CallerIdentity, current_identity, require_role
from ._helpers import decision_to_out, error, task_to_out

router = APIRouter(prefix="/v1/awe/tasks", tags=["tasks"])

REGISTRY_CHANGE_REQUEST_ARTIFACT = "registry.change_request"
REGISTRY_INTAKE_FORM_ARTIFACT = "registry.intake_form"


def _require_assignee_id(identity: CallerIdentity) -> Union[str, JSONResponse]:
    if not identity.assignee_id:
        return error(
            401,
            "AWE-001",
            "Token missing assignee claim (`preferred_username`, `username`, or `sub`)",
        )
    return identity.assignee_id


@router.get(
    "/stats",
    response_model=TaskStatsOut,
    summary="Task counts for the logged-in user, grouped by artifact type",
)
async def task_stats(
    status_filter: Optional[str] = Query(default=None, alias="status"),
    identity: CallerIdentity = Depends(current_identity),
    session: AsyncSession = Depends(get_db),
):
    assignee_id = _require_assignee_id(identity)
    if isinstance(assignee_id, JSONResponse):
        return assignee_id

    task_filters = [ApprovalTask.assignee == assignee_id]
    if status_filter:
        task_filters.append(ApprovalTask.status == status_filter)

    count_stmt = (
        select(
            ApprovalRequest.artifact_type,
            func.count(ApprovalTask.id),
        )
        .join(ApprovalRequest, ApprovalTask.request_id == ApprovalRequest.id)
        .where(*task_filters)
        .group_by(ApprovalRequest.artifact_type)
    )
    rows = await session.execute(count_stmt)
    by_type = {artifact_type: count for artifact_type, count in rows.all()}

    change_request_count = by_type.get(REGISTRY_CHANGE_REQUEST_ARTIFACT, 0)
    intake_form_count = by_type.get(REGISTRY_INTAKE_FORM_ARTIFACT, 0)
    other_total = sum(
        count
        for artifact_type, count in by_type.items()
        if artifact_type
        not in (REGISTRY_CHANGE_REQUEST_ARTIFACT, REGISTRY_INTAKE_FORM_ARTIFACT)
    )

    return TaskStatsOut(
        total=change_request_count + intake_form_count + other_total,
        change_request_count=change_request_count,
        intake_form_count=intake_form_count,
    )


@router.get(
    "",
    response_model=PagedTasksOut,
    summary="List tasks — paginated, by assignee (default = me) and/or by request_id",
    responses={**ResponseUnauthorized},
)
async def list_tasks(
    assignee: Optional[str] = Query(
        default="me",
        description=(
            "Filter by assignee. Default `me` resolves to `preferred_username`, "
            "then `username`, then `sub`. "
            "Pass `*` (or any non-`me` value) plus `request_id` to enumerate "
            "all tasks for a given request — used by the admin Request Detail page."
        ),
    ),
    request_id: Optional[str] = Query(default=None),
    status_filter: Optional[str] = Query(default=None, alias="status"),
    artifact_type: Optional[str] = Query(default=None),
    policy_key: Optional[str] = Query(default=None),
    search_text: Optional[str] = Query(
        default=None,
        description="Case-insensitive substring match against task search_text.",
    ),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25, ge=1, le=100),
    identity: CallerIdentity = Depends(current_identity),
    session: AsyncSession = Depends(get_db),
):
    # Collect task-level and request-level WHERE clauses separately so they
    # can be applied to both the COUNT and the data query without duplication.
    task_filters = []
    if assignee == "me":
        me = _require_assignee_id(identity)
        if isinstance(me, JSONResponse):
            return me
        task_filters.append(ApprovalTask.assignee == me)
    elif assignee and assignee != "*":
        task_filters.append(ApprovalTask.assignee == assignee)
    if request_id:
        task_filters.append(ApprovalTask.request_id == request_id)
    if status_filter:
        task_filters.append(ApprovalTask.status == status_filter)

    request_filters = []
    needs_request_join = bool(artifact_type or policy_key or search_text)
    if artifact_type:
        request_filters.append(ApprovalRequest.artifact_type == artifact_type)
    if policy_key:
        request_filters.append(ApprovalRequest.policy_key == policy_key)
    if search_text:
        pattern = f"%{search_text.strip()}%"
        request_filters.append(
            or_(
                ApprovalTask.search_text.ilike(pattern),
                cast(ApprovalRequest.context, String).ilike(pattern),
            )
        )

    def _apply_request_join(stmt):
        return (
            stmt
            .join(ApprovalRequest, ApprovalTask.request_id == ApprovalRequest.id)
            .where(*request_filters)
        )

    # Total count (no offset/limit)
    count_stmt = select(func.count(ApprovalTask.id)).where(*task_filters)
    if needs_request_join:
        count_stmt = _apply_request_join(count_stmt)
    total: int = await session.scalar(count_stmt) or 0

    # Paginated data
    offset = (page - 1) * page_size
    stmt = (
        select(ApprovalTask)
        .options(selectinload(ApprovalTask.request))
        .where(*task_filters)
        .order_by(ApprovalTask.created_at.desc())
        .offset(offset)
        .limit(page_size)
    )
    if needs_request_join:
        stmt = _apply_request_join(stmt)

    rows = await session.execute(stmt)
    tasks = rows.scalars().all()

    decision_ids = [t.decision_id for t in tasks if t.decision_id]
    decision_map: dict[str, ApprovalDecision] = {}
    if decision_ids:
        decision_rows = await session.execute(
            select(ApprovalDecision).where(ApprovalDecision.id.in_(decision_ids))
        )
        decision_map = {d.id: d for d in decision_rows.scalars().all()}

    return PagedTasksOut(
        items=[
            task_to_out(t, t.request, decision_map.get(t.decision_id) if t.decision_id else None)
            for t in tasks
        ],
        total=total,
        page=page,
        page_size=page_size,
        pages=math.ceil(total / page_size) if total else 1,
    )


@router.post(
    "/{task_id}/claim",
    response_model=TaskOut,
    summary="Claim a task (intent-to-act marker; not required for decision)",
    responses={
        **ResponseUnauthorized,
        **ResponseForbiddenNotAssignee,
        **ResponseTaskNotFound,
        **ResponseStateConflict,
    },
)
async def claim_task(
    task_id: str,
    identity: CallerIdentity = Depends(current_identity),
    session: AsyncSession = Depends(get_db),
):
    task = await session.get(ApprovalTask, task_id)
    if task is None:
        return error(404, "AWE-004", f"Task {task_id} not found")
    if task.assignee != identity.assignee_id and "AWE_ADMIN" not in identity.roles:
        return error(403, "AWE-008", "Task is not assigned to you")
    if task.status != "open":
        return error(409, "AWE-007", f"Task is in '{task.status}' state — cannot claim")

    task.status = "claimed"
    task.claimed_at = utcnow()
    return task_to_out(task)


@router.post(
    "/{task_id}/decision",
    status_code=status.HTTP_201_CREATED,
    response_model=DecisionOut,
    summary="Record a decision (approve / reject / abstain) on a task",
    responses={
        **ResponseUnauthorized,
        **ResponseForbiddenNotAssignee,
        **ResponseTaskNotFound,
        **ResponseRequestNotFound,
        **ResponseStateConflict,
    },
)
async def decide(
    task_id: str,
    payload: DecisionIn,
    identity: CallerIdentity = Depends(current_identity),
    session: AsyncSession = Depends(get_db),
):
    task = await session.get(ApprovalTask, task_id)
    if task is None:
        return error(404, "AWE-004", f"Task {task_id} not found")
    if task.assignee != identity.assignee_id and "AWE_ADMIN" not in identity.roles:
        return error(403, "AWE-008", "Task is not assigned to you")
    if task.status not in ("open", "claimed"):
        return error(409, "AWE-007", f"Task is in '{task.status}' state — cannot decide")

    request = await session.get(ApprovalRequest, task.request_id)
    if request is None:
        return error(404, "AWE-003", "Owning request not found (data inconsistency)")

    decision = ApprovalDecision(
        request_id=request.id,
        task_id=task.id,
        stage_order=task.stage_order,
        actor=identity.subject,
        action=payload.action,
        comment=payload.comment,
        attachments_ref=payload.attachments_ref,
    )
    session.add(decision)
    await session.flush()
    task.decision_id = decision.id

    try:
        await engine_svc.apply_decision(
            session=session,
            request=request,
            task=task,
            actor=identity.subject,
            action=payload.action,
        )
    except engine_svc.EngineError as e:
        return error(409, "AWE-007", str(e))

    return decision_to_out(decision)


@router.post(
    "/{task_id}/reassign",
    response_model=TaskOut,
    summary="Reassign an open task to a different user (admin only)",
    responses={
        **ResponseUnauthorized,
        **ResponseForbiddenAdmin,
        **ResponseTaskNotFound,
        **ResponseRequestNotFound,
        **ResponseStateConflict,
    },
)
async def reassign(
    task_id: str,
    payload: ReassignTaskIn,
    identity: CallerIdentity = Depends(require_role("AWE_ADMIN")),
    session: AsyncSession = Depends(get_db),
):
    task = await session.get(ApprovalTask, task_id)
    if task is None:
        return error(404, "AWE-004", f"Task {task_id} not found")
    request = await session.get(ApprovalRequest, task.request_id)
    if request is None:
        return error(404, "AWE-003", "Owning request not found (data inconsistency)")
    try:
        new_task = await engine_svc.reassign_task(
            session=session,
            request=request,
            task=task,
            new_assignee=payload.new_assignee,
            actor=identity.subject,
            reason=payload.reason,
        )
    except engine_svc.EngineError as e:
        return error(409, "AWE-007", str(e))

    await audit_svc.record(
        session,
        identity=identity,
        action="task.reassign",
        resource_type="task",
        resource_id=task.id,
        summary=(
            f"Reassigned stage-{task.stage_order} task from {task.assignee} "
            f"to {payload.new_assignee}"
            + (f" ({payload.reason})" if payload.reason else "")
        ),
        before={"assignee": task.assignee, "status": "open"},
        after={"assignee": payload.new_assignee, "status": "open", "new_task_id": new_task.id},
        metadata={"request_id": request.id, "reason": payload.reason},
    )
    return task_to_out(new_task)
