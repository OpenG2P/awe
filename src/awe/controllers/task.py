"""
Approver task endpoints — invoked by the Caller Svc on behalf of end users.

The bearer token's `sub` claim is treated as the assignee id. Approvers can
only act on tasks assigned to them; the policy editor can override that via a
`awe-admin` role token (intentional escape hatch for ops).
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_db
from ..models import ApprovalDecision, ApprovalRequest, ApprovalTask
from ..models.base import utcnow
from ..schemas.request import DecisionIn, DecisionOut, TaskOut
from ..services import engine as engine_svc
from ..services.auth import CallerIdentity, current_identity
from ._helpers import decision_to_out, error, task_to_out

router = APIRouter(prefix="/v1/awe/tasks", tags=["tasks"])


@router.get(
    "",
    response_model=list[TaskOut],
    summary="List tasks — by assignee (default = me) and/or by request_id",
)
async def list_tasks(
    assignee: Optional[str] = Query(
        default="me",
        description=(
            "Filter by assignee. Default `me` resolves to the token's `sub`. "
            "Pass `*` (or any non-`me` value) plus `request_id` to enumerate "
            "all tasks for a given request — used by the admin Request Detail page."
        ),
    ),
    request_id: Optional[str] = Query(default=None),
    status_filter: Optional[str] = Query(default=None, alias="status"),
    limit: int = Query(default=100, ge=1, le=500),
    identity: CallerIdentity = Depends(current_identity),
    session: AsyncSession = Depends(get_db),
):
    stmt = select(ApprovalTask).order_by(ApprovalTask.created_at.desc()).limit(limit)
    if assignee == "me":
        stmt = stmt.where(ApprovalTask.assignee == identity.subject)
    elif assignee and assignee != "*":
        stmt = stmt.where(ApprovalTask.assignee == assignee)
    if request_id:
        stmt = stmt.where(ApprovalTask.request_id == request_id)
    if status_filter:
        stmt = stmt.where(ApprovalTask.status == status_filter)
    rows = await session.execute(stmt)
    return [task_to_out(t) for t in rows.scalars()]


@router.post(
    "/{task_id}/claim",
    response_model=TaskOut,
    summary="Claim a task (intent-to-act marker; not required for decision)",
)
async def claim_task(
    task_id: str,
    identity: CallerIdentity = Depends(current_identity),
    session: AsyncSession = Depends(get_db),
):
    task = await session.get(ApprovalTask, task_id)
    if task is None:
        return error(404, "AWE-004", f"Task {task_id} not found")
    if task.assignee != identity.subject and "awe-admin" not in identity.roles:
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
    if task.assignee != identity.subject and "awe-admin" not in identity.roles:
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
