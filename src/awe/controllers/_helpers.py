"""Controller helpers — model→dict mappers, common error responses."""

from __future__ import annotations

from typing import List, Optional

from fastapi.responses import JSONResponse

from ..models import (
    ApprovalDecision,
    ApprovalEvent,
    ApprovalPolicy,
    ApprovalRequest,
    ApprovalStage,
    ApprovalTask,
    ApproverRule,
)
from ..schemas.common import make_error_response
from ..schemas.policy import (
    ApproverRuleOut,
    PolicyOut,
    PolicyVersionOut,
    StageOut,
)
from ..schemas.request import DecisionOut, EventOut, RequestOut, TaskOut


def policy_to_out(policy: ApprovalPolicy) -> PolicyOut:
    return PolicyOut(
        id=policy.id,
        policy_key=policy.policy_key,
        version=policy.version,
        name=policy.name,
        description=policy.description,
        status=policy.status,
        artifact_type=policy.artifact_type,
        forbid_self_approval=policy.forbid_self_approval,
        forbid_repeat_approvers=policy.forbid_repeat_approvers,
        created_by=policy.created_by,
        created_at=policy.created_at,
        updated_at=policy.updated_at,
        stages=[stage_to_out(s) for s in sorted(policy.stages, key=lambda s: s.stage_order)],
    )


def policy_version_to_out(policy: ApprovalPolicy) -> PolicyVersionOut:
    return PolicyVersionOut(
        id=policy.id,
        policy_key=policy.policy_key,
        version=policy.version,
        status=policy.status,
        created_at=policy.created_at,
    )


def stage_to_out(stage: ApprovalStage) -> StageOut:
    esc = [
        ApproverRuleOut(
            id=f"esc-{stage.id}-{i}",
            rule_type=r.get("rule_type", "user"),
            rule_value=r.get("rule_value", {}),
            kind=r.get("kind", "approver"),
            required=r.get("required", False),
        )
        for i, r in enumerate(stage.escalation_rules_json or [])
    ]
    return StageOut(
        id=stage.id,
        stage_order=stage.stage_order,
        name=stage.name,
        mode=stage.mode,
        mode_value=stage.mode_value,
        sla_hours=stage.sla_hours,
        skip_if=stage.skip_if,
        on_empty=stage.on_empty,
        parallel_group=stage.parallel_group,
        on_breach=stage.on_breach,
        escalation_rules=esc,
        rules=[rule_to_out(r) for r in stage.rules],
    )


def rule_to_out(rule: ApproverRule) -> ApproverRuleOut:
    return ApproverRuleOut(
        id=rule.id,
        rule_type=rule.rule_type,
        rule_value=rule.rule_value,
        kind=rule.kind,
        required=rule.required,
    )


def request_to_out(req: ApprovalRequest) -> RequestOut:
    return RequestOut(
        id=req.id,
        policy_id=req.policy_id,
        policy_key=req.policy_key,
        policy_version=req.policy_version,
        artifact_type=req.artifact_type,
        artifact_id=req.artifact_id,
        source_service=req.source_service,
        requester=req.requester,
        context=req.context,
        status=req.status,
        current_stage_order=req.current_stage_order,
        callback_url=req.callback_url,
        completed_at=req.completed_at,
        created_at=req.created_at,
        updated_at=req.updated_at,
    )


def task_to_out(
    task: ApprovalTask,
    request: Optional[ApprovalRequest] = None,
    decision: Optional[ApprovalDecision] = None,
) -> TaskOut:
    return TaskOut(
        id=task.id,
        request_id=task.request_id,
        stage_id=task.stage_id,
        stage_order=task.stage_order,
        assignee=task.assignee,
        kind=task.kind,
        delegated_from=task.delegated_from,
        reassigned_from=task.reassigned_from,
        status=task.status,
        claimed_at=task.claimed_at,
        completed_at=task.completed_at,
        due_at=task.due_at,
        decision_id=task.decision_id,
        decision_action=decision.action if decision else None,
        decision_comment=decision.comment if decision else None,
        created_at=task.created_at,
        context=request.context if request else None,
        artifact_type=request.artifact_type if request else None,
        policy_key=request.policy_key if request else None,
    )


def decision_to_out(d: ApprovalDecision) -> DecisionOut:
    return DecisionOut(
        id=d.id,
        request_id=d.request_id,
        task_id=d.task_id,
        stage_order=d.stage_order,
        actor=d.actor,
        action=d.action,
        comment=d.comment,
        attachments_ref=d.attachments_ref,
        created_at=d.created_at,
    )


def event_to_out(e: ApprovalEvent) -> EventOut:
    return EventOut(
        id=e.id,
        request_id=e.request_id,
        event_type=e.event_type,
        payload=e.payload,
        created_at=e.created_at,
    )


def tasks_to_out(tasks: List[ApprovalTask]) -> List[TaskOut]:
    return [task_to_out(t) for t in tasks]


def error(status_code: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code, content=make_error_response(code, message)
    )
