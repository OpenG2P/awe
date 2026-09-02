"""
Stage transition engine.

Lifecycle:
  request_create → activate first parallel-group → create tasks → emit `stage_started`
  decision arrives → recompute stage state → if stage approved/rejected:
        stage rejected → request rejected (any stage veto terminates the flow)
        stage approved → close remaining open tasks (skipped),
                         emit `stage_completed`,
                         check if the whole parallel group is now approved,
                         if yes advance to next parallel group, else keep waiting
        final group complete → request approved

Parallel groups:
  Stages sharing `parallel_group` run concurrently. A group completes when all
  its stages are approved. Any single stage rejecting rejects the request.
  Stage whose `parallel_group` is null is its own group = strictly sequential.

Skip / empty rules:
  * `stage.skip_if` — JSONLogic over request.context, evaluated when the stage
    activates. Truthy → emit `stage_skipped` and treat as approved.
  * `stage.on_empty == 'skip'` — no resolved approvers → same as above.
  * `stage.on_empty == 'block'` — no resolved approvers → reject the request.

Segregation-of-duties:
  * `policy.forbid_self_approval` — request.requester filtered from every
    stage's resolved approvers.
  * `policy.forbid_repeat_approvers` — anyone who has already decided on any
    earlier stage of this request is filtered out.

Delegation:
  * When creating tasks, `user_delegation` rows active at `now` redirect the
    task to `delegate_to`. The task records `delegated_from` for audit.

Observer rules:
  * Rules with `kind="observer"` produce read-only tasks (`task.kind="observer"`)
  * They are shown in the UI and receive webhook visibility but are NOT counted
    toward stage completion.
"""

from __future__ import annotations

import logging
import math
from datetime import timedelta
from typing import Dict, List, Optional, Sequence

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
    ApproverRule,
    UserDelegation,
    WebhookDelivery,
)
from ..models.base import new_uuid, utcnow
from . import resolver as resolver_svc
from .task_search import build_task_search_text

logger = logging.getLogger(__name__)


class EngineError(Exception):
    pass


# Simple policy cache to avoid repeated database loads
_policy_cache: Dict[str, tuple[ApprovalPolicy, float]] = {}
_POLICY_CACHE_TTL = 300  # 5 minutes


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
    """Create the request, activate the first parallel group, emit events."""
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

    await emit_event(
        session,
        request,
        "request_created",
        {"policy_key": policy.policy_key, "policy_version": policy.version},
    )

    stages = sorted(policy.stages, key=lambda s: s.stage_order)
    if not stages:
        request.status = "approved"
        request.completed_at = utcnow()
        await emit_event(session, request, "request_approved", {"actor": requester})
        return request

    await _activate_next_group(
        session, request, policy, stages, after_order=0, actor=requester
    )
    return request


async def apply_decision(
    session: AsyncSession,
    request: ApprovalRequest,
    task: ApprovalTask,
    actor: str,
    action: str,
) -> ApprovalRequest:
    """Recompute stage state after a decision."""
    if request.status not in ("pending", "in_review"):
        raise EngineError(
            f"Request is in terminal state '{request.status}' — cannot accept decisions"
        )
    if task.status not in ("open", "claimed"):
        raise EngineError(f"Task is not open (status={task.status})")
    if task.kind == "observer":
        # Observers can leave comments without closing the stage. The decision
        # row has already been written by the controller for the audit trail;
        # here we just flip the task and keep the stage state unchanged.
        task.status = "completed"
        task.completed_at = utcnow()
        await session.flush()
        return request

    task.status = "completed"
    task.completed_at = utcnow()
    await session.flush()

    policy = await _load_policy(session, request.policy_id)
    stage = _stage_at(policy, task.stage_order)
    outcome = await _recompute_stage(session, stage, request.id)

    if outcome == "open":
        return request

    if outcome == "rejected":
        await _close_group_tasks_skipped(session, request, policy, task.stage_order)
        request.status = "rejected"
        request.completed_at = utcnow()
        await emit_event(
            session,
            request,
            "stage_completed",
            {"stage_order": stage.stage_order, "outcome": "rejected"},
        )
        await emit_event(session, request, "request_rejected", {"actor": actor})
        return request

    # outcome == "approved" for this stage. Close any leftover open tasks on
    # THIS stage (e.g. quorum hit — no need for the remaining approvers).
    await _close_stage_open_tasks(session, request.id, stage.stage_order)
    await emit_event(
        session,
        request,
        "stage_completed",
        {"stage_order": stage.stage_order, "outcome": "approved", "actor": actor},
    )

    # Parallel-group completion check: are all stages in this group done?
    stages = sorted(policy.stages, key=lambda s: s.stage_order)
    group_key = _group_key(stage)
    group_stages = [s for s in stages if _group_key(s) == group_key]
    all_group_done = True
    for s in group_stages:
        if not await _stage_is_terminal(session, s, request.id):
            all_group_done = False
            break
    if not all_group_done:
        # Other stages in the parallel group still open — keep waiting.
        return request

    # Advance to the next group.
    await _activate_next_group(
        session,
        request,
        policy,
        stages,
        after_order=max(s.stage_order for s in group_stages),
        actor=actor,
    )
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
    rows = await session.execute(
        select(ApprovalTask).where(
            ApprovalTask.request_id == request.id,
            ApprovalTask.status.in_(("open", "claimed")),
        )
    )
    for t in rows.scalars():
        t.status = "skipped"
        t.completed_at = utcnow()
    await emit_event(
        session, request, "request_cancelled", {"actor": actor, "reason": reason}
    )
    return request


async def reassign_task(
    session: AsyncSession,
    request: ApprovalRequest,
    task: ApprovalTask,
    new_assignee: str,
    actor: str,
    reason: Optional[str],
) -> ApprovalTask:
    """Close an open task as `reassigned`, create a fresh task for new_assignee.

    Decision-making never happens on a closed task, so reassignment always
    produces a new task row — keeps the audit trail crisp.
    """
    if request.status not in ("pending", "in_review"):
        raise EngineError(
            f"Request is in terminal state '{request.status}' — cannot reassign"
        )
    if task.status not in ("open", "claimed"):
        raise EngineError(
            f"Task is in '{task.status}' state — only open/claimed tasks can be reassigned"
        )
    if task.assignee == new_assignee:
        raise EngineError("new_assignee equals the current assignee")

    old_assignee = task.assignee
    task.status = "reassigned"
    task.completed_at = utcnow()
    new_task = ApprovalTask(
        request_id=request.id,
        stage_id=task.stage_id,
        stage_order=task.stage_order,
        assignee=new_assignee,
        kind=task.kind,
        reassigned_from=old_assignee,
        status="open",
        due_at=task.due_at,
        search_text=task.search_text or build_task_search_text(request),
    )
    session.add(new_task)
    await session.flush()

    await emit_event(
        session,
        request,
        "task_reassigned",
        {
            "task_id": task.id,
            "new_task_id": new_task.id,
            "stage_order": task.stage_order,
            "from": old_assignee,
            "to": new_assignee,
            "actor": actor,
            "reason": reason,
        },
    )
    return new_task


# ---------------------------------------------------------------------------
# Stage activation
# ---------------------------------------------------------------------------
async def _activate_next_group(
    session: AsyncSession,
    request: ApprovalRequest,
    policy: ApprovalPolicy,
    stages: List[ApprovalStage],
    after_order: int,
    actor: Optional[str],
) -> None:
    """Find the next parallel group with stage_order > after_order and activate
    every non-skipped stage in it. If the group is entirely skipped, recurse.

    Adjacent stages sharing a `parallel_group` value are the same group. A
    stage whose `parallel_group` is null is its own group.
    """
    # Build ordered groups.
    groups: List[List[ApprovalStage]] = []
    for stage in stages:
        if not groups or _group_key(stage) != _group_key(groups[-1][0]):
            groups.append([stage])
        else:
            groups[-1].append(stage)

    for group in groups:
        if group[0].stage_order <= after_order:
            continue

        any_active = False
        cache: dict = {}
        approved_in_prior_stages = await _approved_actors_before(
            session, request.id, min(s.stage_order for s in group)
        )
        for stage in group:
            activated = await _activate_one_stage(
                session,
                request,
                policy,
                stage,
                cache,
                approved_in_prior_stages,
            )
            if activated == "blocked":
                # Stage had no approvers and on_empty=block → request rejected.
                return
            if activated == "started":
                any_active = True

        if any_active:
            # Put current_stage_order on the smallest active stage order for UI.
            active_orders = []
            for s in group:
                tasks = await _stage_tasks(session, request.id, s.stage_order)
                if any(t.status in ("open", "claimed") for t in tasks if t.kind == "approver"):
                    active_orders.append(s.stage_order)
            if active_orders:
                request.current_stage_order = min(active_orders)
            request.status = "in_review"
            return

    # Fell off the end — every remaining stage was skipped.
    request.status = "approved"
    request.completed_at = utcnow()
    payload: dict = {"reason": "all_stages_skipped"}
    if actor:
        payload["actor"] = actor
    await emit_event(session, request, "request_approved", payload)


async def _activate_one_stage(
    session: AsyncSession,
    request: ApprovalRequest,
    policy: ApprovalPolicy,
    stage: ApprovalStage,
    cache: dict,
    approved_in_prior_stages: set[str],
) -> str:
    """Returns 'started' / 'skipped' / 'blocked'."""
    if _should_skip(stage, request.context):
        await emit_event(
            session,
            request,
            "stage_skipped",
            {"stage_order": stage.stage_order, "reason": "skip_if"},
        )
        return "skipped"

    approver_rules = [r for r in stage.rules if r.kind == "approver"]
    observer_rules = [r for r in stage.rules if r.kind == "observer"]
    approvers = await resolver_svc.resolve_stage(approver_rules, request.context, cache)
    observers = await resolver_svc.resolve_stage(observer_rules, request.context, cache)
    required_ids = await _resolve_required(stage, request.context, cache)

    # Apply SoD filters (approvers only — observers may see but not block).
    approvers = _apply_sod_filters(
        approvers, policy, request.requester, approved_in_prior_stages
    )
    required_ids = _apply_sod_filters(
        required_ids, policy, request.requester, approved_in_prior_stages
    )

    # Apply delegation: replace approver X with delegate Y when X has active
    # delegation. Observers get the same treatment (they still get the task).
    approver_tasks = await _build_tasks_with_delegation(session, approvers)
    observer_tasks = await _build_tasks_with_delegation(session, observers)

    if not approver_tasks:
        if stage.on_empty == "skip":
            await emit_event(
                session,
                request,
                "stage_skipped",
                {"stage_order": stage.stage_order, "reason": "no_approvers"},
            )
            return "skipped"
        request.status = "rejected"
        request.completed_at = utcnow()
        await emit_event(
            session,
            request,
            "request_rejected",
            {"reason": "no_approvers_resolved", "stage_order": stage.stage_order},
        )
        return "blocked"

    due_at = utcnow() + timedelta(hours=stage.sla_hours) if stage.sla_hours else None
    task_search = build_task_search_text(request)
    for assignee, delegated_from in approver_tasks:
        session.add(
            ApprovalTask(
                request_id=request.id,
                stage_id=stage.id,
                stage_order=stage.stage_order,
                assignee=assignee,
                kind="approver",
                delegated_from=delegated_from,
                status="open",
                due_at=due_at,
                search_text=task_search,
            )
        )
    for assignee, delegated_from in observer_tasks:
        session.add(
            ApprovalTask(
                request_id=request.id,
                stage_id=stage.id,
                stage_order=stage.stage_order,
                assignee=assignee,
                kind="observer",
                delegated_from=delegated_from,
                status="open",
                search_text=task_search,
            )
        )
    await session.flush()

    await emit_event(
        session,
        request,
        "stage_started",
        {
            "stage_order": stage.stage_order,
            "name": stage.name,
            "mode": stage.mode,
            "mode_value": stage.mode_value,
            "approvers": [a for a, _ in approver_tasks],
            "observers": [a for a, _ in observer_tasks],
            "required_approvers": sorted(required_ids),
        },
    )
    return "started"


# ---------------------------------------------------------------------------
# Stage evaluation
# ---------------------------------------------------------------------------
async def _recompute_stage(
    session: AsyncSession, stage: ApprovalStage, request_id: str
) -> str:
    stage_tasks = await _stage_tasks(session, request_id, stage.stage_order)
    approver_tasks = [t for t in stage_tasks if t.kind == "approver"]
    decisions = await _stage_decisions(session, request_id, stage.stage_order)
    decision_counts = {"approve": 0, "reject": 0, "abstain": 0}
    approver_ids_who_approved: set[str] = set()
    for d in decisions:
        decision_counts[d.action] = decision_counts.get(d.action, 0) + 1
        if d.action == "approve":
            approver_ids_who_approved.add(d.actor)

    required_ids = await _required_assignees_for_stage(session, stage, request_id)

    quorum_outcome = _evaluate_quorum(stage, approver_tasks, decision_counts)
    if quorum_outcome == "rejected":
        return "rejected"

    # Required approvers gate: even if quorum met, if there are required users
    # who haven't approved yet AND still have open tasks, keep waiting.
    open_required = {
        t.assignee
        for t in approver_tasks
        if t.assignee in required_ids and t.status in ("open", "claimed")
    }
    missing_required = {
        uid for uid in required_ids if uid not in approver_ids_who_approved
    }
    if quorum_outcome == "approved" and missing_required:
        if open_required:
            return "open"  # quorum satisfied, but required users still have open tasks
        # Required user exists but has no open task (expired/skipped/reassigned) — reject.
        return "rejected"

    return quorum_outcome


def _evaluate_quorum(
    stage: ApprovalStage,
    approver_tasks: List[ApprovalTask],
    decision_counts: dict[str, int],
) -> str:
    total = len(approver_tasks)
    open_or_claimed = sum(
        1 for t in approver_tasks if t.status in ("open", "claimed")
    )
    approves = decision_counts.get("approve", 0)
    rejects = decision_counts.get("reject", 0)

    # Any reject is a veto — stage (and request) terminates immediately.
    if rejects > 0:
        return "rejected"

    if stage.mode == "all":
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


# ---------------------------------------------------------------------------
# Helpers — parallel groups
# ---------------------------------------------------------------------------
def _group_key(stage: ApprovalStage) -> tuple:
    """Distinct group key: if parallel_group is set, use it; otherwise the
    stage_order (makes the stage its own group)."""
    return ("g", stage.parallel_group) if stage.parallel_group is not None else (
        "s",
        stage.stage_order,
    )


async def _stage_is_terminal(
    session: AsyncSession, stage: ApprovalStage, request_id: str
) -> bool:
    """A stage is terminal when every approver task is out of open/claimed."""
    tasks = await _stage_tasks(session, request_id, stage.stage_order)
    approver_tasks = [t for t in tasks if t.kind == "approver"]
    if not approver_tasks:
        # Stage was skipped → consider terminal.
        return True
    return all(t.status not in ("open", "claimed") for t in approver_tasks)


async def _close_stage_open_tasks(
    session: AsyncSession, request_id: str, stage_order: int
) -> None:
    rows = await session.execute(
        select(ApprovalTask).where(
            ApprovalTask.request_id == request_id,
            ApprovalTask.stage_order == stage_order,
            ApprovalTask.status.in_(("open", "claimed")),
        )
    )
    for t in rows.scalars():
        t.status = "skipped"
        t.completed_at = utcnow()
    await session.flush()


async def _close_group_tasks_skipped(
    session: AsyncSession,
    request: ApprovalRequest,
    policy: ApprovalPolicy,
    rejected_stage_order: int,
) -> None:
    """When any stage in a group rejects, close every open task in the group."""
    stage = _stage_at(policy, rejected_stage_order)
    group_key = _group_key(stage)
    group_stages = [
        s for s in policy.stages if _group_key(s) == group_key
    ]
    for s in group_stages:
        await _close_stage_open_tasks(session, request.id, s.stage_order)


# ---------------------------------------------------------------------------
# Helpers — SoD + delegation + required
# ---------------------------------------------------------------------------
def _apply_sod_filters(
    user_ids: List[str],
    policy: ApprovalPolicy,
    requester: Optional[str],
    approved_in_prior_stages: set[str],
) -> List[str]:
    out = []
    for uid in user_ids:
        if policy.forbid_self_approval and requester and uid == requester:
            continue
        if policy.forbid_repeat_approvers and uid in approved_in_prior_stages:
            continue
        out.append(uid)
    return out


async def _approved_actors_before(
    session: AsyncSession, request_id: str, stage_order: int
) -> set[str]:
    rows = await session.execute(
        select(ApprovalDecision.actor).where(
            ApprovalDecision.request_id == request_id,
            ApprovalDecision.action == "approve",
            ApprovalDecision.stage_order < stage_order,
        )
    )
    return {r for (r,) in rows.all()}


async def _build_tasks_with_delegation(
    session: AsyncSession, user_ids: Sequence[str]
) -> List[tuple[str, Optional[str]]]:
    """For each user, if they have an active delegation, return (delegate, user);
    otherwise (user, None). Preserves input order, deduplicates by final assignee."""
    if not user_ids:
        return []
    now = utcnow()
    rows = await session.execute(
        select(UserDelegation)
        .where(
            UserDelegation.user_id.in_(list(user_ids)),
            UserDelegation.starts_at <= now,
            UserDelegation.ends_at > now,
        )
        .order_by(UserDelegation.created_at.desc())
    )
    # Most-recent delegation wins for each user.
    delegation_for: dict[str, str] = {}
    for d in rows.scalars():
        delegation_for.setdefault(d.user_id, d.delegate_to)

    out: List[tuple[str, Optional[str]]] = []
    seen: set[str] = set()
    for uid in user_ids:
        if uid in delegation_for:
            target, frm = delegation_for[uid], uid
        else:
            target, frm = uid, None
        if target not in seen:
            seen.add(target)
            out.append((target, frm))
    return out


async def _resolve_required(
    stage: ApprovalStage, context: dict, cache: dict
) -> set[str]:
    required_rules = [
        r for r in stage.rules if r.kind == "approver" and r.required
    ]
    if not required_rules:
        return set()
    resolved = await resolver_svc.resolve_stage(required_rules, context, cache)
    return set(resolved)


async def _required_assignees_for_stage(
    session: AsyncSession, stage: ApprovalStage, request_id: str
) -> set[str]:
    """Look up which current assignees on the stage came from `required=True` rules.

    We re-derive this from the task list + the required rule set against the
    request's frozen context, so it survives reassign/delegation (the original
    required user's replacement is still required).
    """
    req = await session.get(ApprovalRequest, request_id)
    if req is None:
        return set()
    required_ids = await _resolve_required(stage, req.context, {})
    if not required_ids:
        return set()
    # For each required original id, find its current task assignee(s) — either
    # themselves, or a delegate/reassignee that replaced them.
    rows = await session.execute(
        select(ApprovalTask).where(
            ApprovalTask.request_id == request_id,
            ApprovalTask.stage_order == stage.stage_order,
            ApprovalTask.kind == "approver",
        )
    )
    tasks = list(rows.scalars())
    effective: set[str] = set()
    for t in tasks:
        if t.assignee in required_ids:
            effective.add(t.assignee)
        elif t.delegated_from and t.delegated_from in required_ids:
            effective.add(t.assignee)
        elif t.reassigned_from and t.reassigned_from in required_ids:
            effective.add(t.assignee)
    return effective


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------
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


def _stage_at(policy: ApprovalPolicy, order: int) -> ApprovalStage:
    for s in policy.stages:
        if s.stage_order == order:
            return s
    raise EngineError(f"Stage order {order} not in policy {policy.id}")


async def _load_policy(session: AsyncSession, policy_id: str) -> ApprovalPolicy:
    # Check cache first
    import time
    now = time.time()
    if policy_id in _policy_cache:
        policy, cached_at = _policy_cache[policy_id]
        if now - cached_at < _POLICY_CACHE_TTL:
            return policy

    # Load from database
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

    # Cache the policy
    _policy_cache[policy_id] = (policy, now)
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


async def _stage_decisions(
    session: AsyncSession, request_id: str, stage_order: int
) -> List[ApprovalDecision]:
    rows = await session.execute(
        select(ApprovalDecision).where(
            ApprovalDecision.request_id == request_id,
            ApprovalDecision.stage_order == stage_order,
        )
    )
    return list(rows.scalars())


# ---------------------------------------------------------------------------
# Event emission + webhook enqueue
# ---------------------------------------------------------------------------
_TERMINAL = {"request_approved", "request_rejected", "request_cancelled"}
_DELIVERABLE = _TERMINAL | {
    "request_created",
    "stage_started",
    "stage_completed",
    "stage_skipped",
    "task_expired",
    "task_reassigned",
    "stage_escalated",
}


async def emit_event(
    session: AsyncSession,
    request: ApprovalRequest,
    event_type: str,
    payload: dict,
) -> ApprovalEvent:
    """Append an event row and, if it's deliverable, queue a webhook attempt."""
    now = utcnow()
    event_payload = {
        **payload,
        "request_id": request.id,
        "artifact_type": request.artifact_type,
        "artifact_id": request.artifact_id,
        "status": request.status,
    }
    # Preserve explicit stage_order from callers (e.g. stage_started for the
    # stage being activated). request.current_stage_order may still reflect the
    # previous stage until _activate_next_group finishes.
    if "stage_order" not in event_payload:
        event_payload["stage_order"] = request.current_stage_order

    event = ApprovalEvent(
        id=new_uuid(),
        request_id=request.id,
        event_type=event_type,
        payload=event_payload,
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


# ---------------------------------------------------------------------------
# Used by the SLA monitor's escalate/auto_approve/auto_reject actions.
# ---------------------------------------------------------------------------
async def escalate_stage(
    session: AsyncSession,
    request: ApprovalRequest,
    stage: ApprovalStage,
    actor: str = "sla-monitor",
) -> int:
    """Resolve escalation_rules and create extra approver tasks on this stage.

    Returns the number of new tasks created. Original expired tasks stay in
    their `expired` state so the audit is preserved.
    """
    rules_json = stage.escalation_rules_json or []
    if not rules_json:
        return 0
    # Inflate dict rules to ApproverRule-like objects for the resolver.
    synthetic_rules: List[ApproverRule] = []
    for r in rules_json:
        synthetic_rules.append(
            ApproverRule(
                id=new_uuid(),
                stage_id=stage.id,
                rule_type=r.get("rule_type", "user"),
                rule_value=r.get("rule_value", {}),
                kind=r.get("kind", "approver"),
                required=bool(r.get("required", False)),
            )
        )
    resolved = await resolver_svc.resolve_stage(synthetic_rules, request.context, {})
    if not resolved:
        return 0
    assignee_pairs = await _build_tasks_with_delegation(session, resolved)
    due_at = (
        utcnow() + timedelta(hours=stage.sla_hours) if stage.sla_hours else None
    )
    # Avoid duplicating tasks for users who already have an open task on the stage.
    existing = {
        t.assignee
        for t in await _stage_tasks(session, request.id, stage.stage_order)
        if t.status in ("open", "claimed")
    }
    task_search = build_task_search_text(request)
    created = 0
    for assignee, delegated_from in assignee_pairs:
        if assignee in existing:
            continue
        session.add(
            ApprovalTask(
                request_id=request.id,
                stage_id=stage.id,
                stage_order=stage.stage_order,
                assignee=assignee,
                kind="approver",
                delegated_from=delegated_from,
                status="open",
                due_at=due_at,
                search_text=task_search,
            )
        )
        created += 1
    await session.flush()
    if created:
        await emit_event(
            session,
            request,
            "stage_escalated",
            {
                "stage_order": stage.stage_order,
                "added_approvers": [a for a, _ in assignee_pairs],
                "actor": actor,
            },
        )
    return created


async def synthesize_decision(
    session: AsyncSession,
    request: ApprovalRequest,
    stage: ApprovalStage,
    action: str,
    actor: str,
    reason: str,
) -> None:
    """Write a synthetic decision for every open approver task on the stage.

    Used by on_breach = auto_approve / auto_reject. Each decision is recorded
    with the system actor so the audit trail is honest. After writing
    decisions the engine's _recompute_stage loop advances as usual.
    """
    tasks = [
        t
        for t in await _stage_tasks(session, request.id, stage.stage_order)
        if t.kind == "approver" and t.status in ("open", "claimed")
    ]
    for t in tasks:
        decision = ApprovalDecision(
            request_id=request.id,
            task_id=t.id,
            stage_order=t.stage_order,
            actor=actor,
            action=action,
            comment=reason,
        )
        session.add(decision)
        await session.flush()
        t.decision_id = decision.id
        t.status = "completed"
        t.completed_at = utcnow()
    await session.flush()

    # Re-drive stage evaluation — the easiest path is to pick any completed
    # task and call apply_decision, but that expects an open task. So we
    # recompute here directly and emit the outcome.
    outcome = await _recompute_stage(session, stage, request.id)
    if outcome == "rejected":
        policy = await _load_policy(session, request.policy_id)
        await _close_group_tasks_skipped(session, request, policy, stage.stage_order)
        request.status = "rejected"
        request.completed_at = utcnow()
        await emit_event(
            session,
            request,
            "stage_completed",
            {"stage_order": stage.stage_order, "outcome": "rejected"},
        )
        await emit_event(session, request, "request_rejected", {"actor": actor})
        return

    if outcome == "approved":
        await _close_stage_open_tasks(session, request.id, stage.stage_order)
        await emit_event(
            session,
            request,
            "stage_completed",
            {"stage_order": stage.stage_order, "outcome": "approved"},
        )
        policy = await _load_policy(session, request.policy_id)
        stages = sorted(policy.stages, key=lambda s: s.stage_order)
        group_key = _group_key(stage)
        group_stages = [s for s in stages if _group_key(s) == group_key]
        all_group_done = True
        for s in group_stages:
            if not await _stage_is_terminal(session, s, request.id):
                all_group_done = False
                break
        if all_group_done:
            await _activate_next_group(
                session,
                request,
                policy,
                stages,
                after_order=max(s.stage_order for s in group_stages),
                actor=actor,
            )
