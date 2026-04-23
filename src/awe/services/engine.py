"""
Stage transition engine.

Drives the lifecycle:
  request_create → resolve stage 1 → create tasks → emit `stage_started`
  decision arrives → recompute stage state → either:
        - stage still open (mode unmet) → no-op
        - stage approved → close remaining open tasks (skipped),
                           emit `stage_completed`,
                           advance to next stage OR mark request `approved`
        - stage rejected → mark request `rejected`

Skip rules:
  * `stage.skip_if` — JSONLogic over request.context, evaluated when the stage
    becomes current. Truthy → emit `stage_skipped` and advance.
  * `stage.on_empty == 'skip'` — no resolved approvers → skip the stage.
  * `stage.on_empty == 'block'` — no resolved approvers → reject the request.
"""

from __future__ import annotations

import logging
import math
from datetime import timedelta
from typing import List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ..models import (
    ApprovalDecision,
    ApprovalEvent,
    ApprovalPolicy,
    ApprovalRequest,
    ApprovalStage,
    ApprovalTask,
    WebhookDelivery,
)
from ..models.base import new_uuid, utcnow
from . import resolver as resolver_svc

logger = logging.getLogger(__name__)


class EngineError(Exception):
    pass


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
async def start_request(
    session: AsyncSession,
    policy: ApprovalPolicy,
    artifact_type: str,
    artifact_id: str,
    source_service: str,
    context: dict,
    callback_url: Optional[str],
    callback_secret_id: Optional[str],
    requester: Optional[str],
) -> ApprovalRequest:
    """Create the request, resolve the first non-skipped stage, emit events."""
    request = ApprovalRequest(
        policy_id=policy.id,
        policy_key=policy.policy_key,
        policy_version=policy.version,
        artifact_type=artifact_type,
        artifact_id=artifact_id,
        source_service=source_service,
        requester=requester,
        context=context or {},
        status="pending",
        current_stage_order=1,
        callback_url=callback_url,
        callback_secret_id=callback_secret_id,
    )
    session.add(request)
    await session.flush()

    await _emit_event(
        session,
        request,
        "request_created",
        {"policy_key": policy.policy_key, "policy_version": policy.version},
    )

    stages = sorted(policy.stages, key=lambda s: s.stage_order)
    if not stages:
        # Zero-stage policy → instant approval. Webhook fires immediately.
        request.status = "approved"
        request.completed_at = utcnow()
        await _emit_event(session, request, "request_approved", {})
        return request

    await _advance_to_stage(session, request, stages, target_order=1, actor=requester)
    return request


async def apply_decision(
    session: AsyncSession,
    request: ApprovalRequest,
    task: ApprovalTask,
    actor: str,
    action: str,
) -> ApprovalRequest:
    """Recompute stage state after a decision. Mutates request + tasks in place."""
    if request.status not in ("pending", "in_review"):
        raise EngineError(
            f"Request is in terminal state '{request.status}' — cannot accept decisions"
        )
    if task.status not in ("open", "claimed"):
        raise EngineError(f"Task is not open (status={task.status})")
    if task.stage_order != request.current_stage_order:
        raise EngineError("Task does not belong to the current stage")

    task.status = "completed"
    task.completed_at = utcnow()
    await session.flush()

    # Reload the policy with stages/rules so we can look up mode + advance.
    policy = await _load_policy(session, request.policy_id)
    stage = _stage_at(policy, request.current_stage_order)
    stage_tasks = await _stage_tasks(session, request.id, request.current_stage_order)
    decision_counts = await _stage_decision_counts(
        session, request.id, request.current_stage_order
    )

    decision_outcome = _evaluate_stage(stage, stage_tasks, decision_counts)

    if decision_outcome == "open":
        # Mode not yet satisfied — keep waiting.
        return request

    if decision_outcome == "rejected":
        # Close any remaining open tasks as skipped, then reject the request.
        for t in stage_tasks:
            if t.status in ("open", "claimed"):
                t.status = "skipped"
                t.completed_at = utcnow()
        request.status = "rejected"
        request.completed_at = utcnow()
        await _emit_event(
            session,
            request,
            "stage_completed",
            {"stage_order": stage.stage_order, "outcome": "rejected"},
        )
        await _emit_event(
            session, request, "request_rejected", {"actor": actor}
        )
        return request

    # decision_outcome == "approved" — close remaining open tasks then advance.
    for t in stage_tasks:
        if t.status in ("open", "claimed"):
            t.status = "skipped"
            t.completed_at = utcnow()
    await _emit_event(
        session,
        request,
        "stage_completed",
        {"stage_order": stage.stage_order, "outcome": "approved"},
    )

    stages = sorted(policy.stages, key=lambda s: s.stage_order)
    next_order = request.current_stage_order + 1
    if next_order > stages[-1].stage_order:
        # Last stage → request approved.
        request.status = "approved"
        request.completed_at = utcnow()
        await _emit_event(session, request, "request_approved", {"actor": actor})
        return request

    await _advance_to_stage(session, request, stages, next_order, actor=actor)
    return request


async def cancel_request(
    session: AsyncSession,
    request: ApprovalRequest,
    actor: Optional[str],
    reason: Optional[str],
) -> ApprovalRequest:
    if request.status not in ("pending", "in_review"):
        raise EngineError(
            f"Request is in terminal state '{request.status}' — cannot cancel"
        )
    request.status = "cancelled"
    request.completed_at = utcnow()
    # Skip outstanding tasks.
    rows = await session.execute(
        select(ApprovalTask).where(
            ApprovalTask.request_id == request.id,
            ApprovalTask.status.in_(("open", "claimed")),
        )
    )
    for t in rows.scalars():
        t.status = "skipped"
        t.completed_at = utcnow()
    await _emit_event(
        session, request, "request_cancelled", {"actor": actor, "reason": reason}
    )
    return request


# ---------------------------------------------------------------------------
# Stage transition internals
# ---------------------------------------------------------------------------
async def _advance_to_stage(
    session: AsyncSession,
    request: ApprovalRequest,
    stages: List[ApprovalStage],
    target_order: int,
    actor: Optional[str],
) -> None:
    """Find the next non-skipped stage from target_order onward; create tasks; emit events."""
    cache: dict = {}
    cursor = target_order
    while cursor <= stages[-1].stage_order:
        stage = next((s for s in stages if s.stage_order == cursor), None)
        if stage is None:
            cursor += 1
            continue

        if _should_skip(stage, request.context):
            await _emit_event(
                session,
                request,
                "stage_skipped",
                {"stage_order": stage.stage_order, "reason": "skip_if"},
            )
            cursor += 1
            continue

        approvers = await resolver_svc.resolve_stage(stage.rules, request.context, cache)

        if not approvers:
            if stage.on_empty == "skip":
                await _emit_event(
                    session,
                    request,
                    "stage_skipped",
                    {"stage_order": stage.stage_order, "reason": "no_approvers"},
                )
                cursor += 1
                continue
            # Block → reject the request.
            request.status = "rejected"
            request.completed_at = utcnow()
            await _emit_event(
                session,
                request,
                "request_rejected",
                {"reason": "no_approvers_resolved", "stage_order": stage.stage_order},
            )
            return

        request.current_stage_order = stage.stage_order
        request.status = "in_review"

        due_at = (
            utcnow() + timedelta(hours=stage.sla_hours) if stage.sla_hours else None
        )
        for user_id in approvers:
            session.add(
                ApprovalTask(
                    request_id=request.id,
                    stage_id=stage.id,
                    stage_order=stage.stage_order,
                    assignee=user_id,
                    status="open",
                    due_at=due_at,
                )
            )
        await session.flush()

        await _emit_event(
            session,
            request,
            "stage_started",
            {
                "stage_order": stage.stage_order,
                "name": stage.name,
                "mode": stage.mode,
                "mode_value": stage.mode_value,
                "approvers": approvers,
            },
        )
        return

    # Fell off the end without creating tasks — treat as approved.
    request.status = "approved"
    request.completed_at = utcnow()
    await _emit_event(
        session, request, "request_approved", {"reason": "all_stages_skipped"}
    )


def _should_skip(stage: ApprovalStage, context: dict) -> bool:
    if not stage.skip_if:
        return False
    try:
        from json_logic import jsonLogic  # type: ignore

        return bool(jsonLogic(stage.skip_if, context))
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "skip_if evaluation failed on stage %s — proceeding (fail-closed): %s",
            stage.stage_order,
            e,
        )
        return False


def _evaluate_stage(
    stage: ApprovalStage,
    stage_tasks: List[ApprovalTask],
    decision_counts: dict[str, int],
) -> str:
    """Return one of: open | approved | rejected.

    Decisions counts come from the actual `approval_decision` rows, scoped to
    this request+stage. Capacity is the remaining open/claimed task count.
    """
    total = len(stage_tasks)
    open_or_claimed = sum(1 for t in stage_tasks if t.status in ("open", "claimed"))
    approves = decision_counts.get("approve", 0)
    rejects = decision_counts.get("reject", 0)

    if stage.mode == "all":
        if rejects > 0:
            return "rejected"
        if approves == total:
            return "approved"
        return "open"

    if stage.mode in ("any-n", "quorum"):
        needed = stage.mode_value or 1
        if approves >= needed:
            return "approved"
        if approves + open_or_claimed < needed:
            return "rejected"
        return "open"

    if stage.mode == "percentage":
        needed = math.ceil((stage.mode_value or 100) / 100 * total)
        if approves >= needed:
            return "approved"
        if approves + open_or_claimed < needed:
            return "rejected"
        return "open"

    return "open"


def _stage_at(policy: ApprovalPolicy, order: int) -> ApprovalStage:
    for s in policy.stages:
        if s.stage_order == order:
            return s
    raise EngineError(f"Stage order {order} not in policy {policy.id}")


async def _load_policy(session: AsyncSession, policy_id: str) -> ApprovalPolicy:
    row = await session.execute(
        select(ApprovalPolicy)
        .options(
            selectinload(ApprovalPolicy.stages).selectinload(ApprovalStage.rules)
        )
        .where(ApprovalPolicy.id == policy_id)
    )
    policy = row.scalar_one_or_none()
    if policy is None:
        raise EngineError(f"Policy {policy_id} not found")
    return policy


async def _stage_tasks(
    session: AsyncSession, request_id: str, stage_order: int
) -> List[ApprovalTask]:
    rows = await session.execute(
        select(ApprovalTask).where(
            ApprovalTask.request_id == request_id,
            ApprovalTask.stage_order == stage_order,
        )
    )
    return list(rows.scalars())


async def _stage_decision_counts(
    session: AsyncSession, request_id: str, stage_order: int
) -> dict[str, int]:
    rows = await session.execute(
        select(ApprovalDecision.action).where(
            ApprovalDecision.request_id == request_id,
            ApprovalDecision.stage_order == stage_order,
        )
    )
    counts: dict[str, int] = {}
    for (action,) in rows.all():
        counts[action] = counts.get(action, 0) + 1
    return counts


# ---------------------------------------------------------------------------
# Event emission + webhook enqueue
# ---------------------------------------------------------------------------
_TERMINAL = {"request_approved", "request_rejected", "request_cancelled"}
_DELIVERABLE = _TERMINAL | {"request_created", "stage_started", "stage_completed"}


async def _emit_event(
    session: AsyncSession,
    request: ApprovalRequest,
    event_type: str,
    payload: dict,
) -> ApprovalEvent:
    """Append an event row and, if it's deliverable, queue a webhook attempt."""
    now = utcnow()
    event = ApprovalEvent(
        id=new_uuid(),
        request_id=request.id,
        event_type=event_type,
        payload={
            **payload,
            "request_id": request.id,
            "artifact_type": request.artifact_type,
            "artifact_id": request.artifact_id,
            "status": request.status,
            "stage_order": request.current_stage_order,
        },
        created_at=now,
    )
    session.add(event)
    await session.flush()

    if event_type in _DELIVERABLE and request.callback_url:
        session.add(
            WebhookDelivery(
                event_id=event.id,
                url=request.callback_url,
                attempt=0,
                status="pending",
                next_attempt_at=now,
            )
        )
        await session.flush()

    return event
