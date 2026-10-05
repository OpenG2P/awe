"""
Novu notifications through the openg2p-notification connector.

AWE notifies staff — approvers and requesters. Registry notifies registrants
on terminal events through the caller webhook; that flow is untouched.

Three layers, same as registry:
  * NotificationWorkflow — dotted event keys, one per business moment.
  * NOTIFICATION_WORKFLOWS — env JSON map, event key -> Novu trigger id. The
    map is also the allow-list: a key not in the map is never sent, so ops
    turns a notification off by removing its key.
  * Novu workflow templates (root novu-setup.py) — hold the channel steps
    (in-app + email) and the title/body text with {{payload.*}} variables.
    Python only passes a payload dict; it never renders titles or bodies.

Timing: collect() runs inside the engine transaction — no HTTP, never
raises — and stashes intents on the session. flush() sends them only after
the transaction commits, so a rolled-back request never notifies. A failed
send is logged and swallowed: a notification must never fail an approval.
The connector is an optional dependency — ImportError disables all of this.

Related display fields (record_name, policy_name, stage config, …) are
resolved by small soft-fail helpers so templates stay human-readable while
IDs remain in the payload for deep links.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import (
    ApprovalDecision,
    ApprovalPolicy,
    ApprovalRequest,
    ApprovalStage,
    ApprovalTask,
)
from . import keycloak_admin as keycloak_admin_svc

_logger = logging.getLogger("awe-notification")

# session.info keys — intents collected inside the transaction.
_PENDING_KEY = "_awe_pending_notifications"
_STAGE_CACHE_KEY = "_awe_notification_stage_display"
_POLICY_CACHE_KEY = "_awe_notification_policy_display"

# novu-py rejects a bulk trigger with more than 100 events.
_BULK_LIMIT = 100

_INTAKE_FORM_ARTIFACT = "registry.intake_form"
_CHANGE_REQUEST_ARTIFACT = "registry.change_request"

_ARTIFACT_TYPE_LABELS = {
    _CHANGE_REQUEST_ARTIFACT: "Change request",
    _INTAKE_FORM_ARTIFACT: "Intake form",
}

# Friendly keys already stamped onto ApprovalRequest.context by the caller.
_CONTEXT_DISPLAY_KEYS = (
    "record_name",
    "register_mnemonic",
    "register_subject",
    "section_mnemonic",
    "intake_form_mnemonic",
    "change_request_id",
    "submission_id",
    "application_reference",
)


class NotificationWorkflow(StrEnum):
    """Dotted event keys — one per business moment.

    Novu trigger ids cannot contain dots, so `approval.stage_started` maps
    to `approval-stage-started` in NOTIFICATION_WORKFLOWS / novu-setup.py.
    """

    STAGE_STARTED = "approval.stage_started"
    TASK_REASSIGNED = "approval.task_reassigned"
    STAGE_ESCALATED = "approval.stage_escalated"
    TASK_EXPIRED = "approval.task_expired"
    REQUEST_APPROVED = "approval.request_approved"
    REQUEST_REJECTED = "approval.request_rejected"
    REQUEST_CANCELLED = "approval.request_cancelled"
    STAGE_QUORUM_SKIPPED = "approval.stage_quorum_skipped"


# Engine event_type -> workflow. Events not listed are silent by design:
# request_created (a stage_started follows), stage_skipped, plain
# stage_completed, and observers (never notified).
_WORKFLOW_BY_EVENT = {
    "stage_started": NotificationWorkflow.STAGE_STARTED,
    "task_reassigned": NotificationWorkflow.TASK_REASSIGNED,
    "stage_escalated": NotificationWorkflow.STAGE_ESCALATED,
    "task_expired": NotificationWorkflow.TASK_EXPIRED,
    "request_approved": NotificationWorkflow.REQUEST_APPROVED,
    "request_rejected": NotificationWorkflow.REQUEST_REJECTED,
    "request_cancelled": NotificationWorkflow.REQUEST_CANCELLED,
    "stage_quorum_skipped": NotificationWorkflow.STAGE_QUORUM_SKIPPED,
}

# Workflows whose Novu template carries stage fields.
_STAGE_EVENTS = (
    NotificationWorkflow.STAGE_STARTED,
    NotificationWorkflow.TASK_REASSIGNED,
    NotificationWorkflow.STAGE_ESCALATED,
    NotificationWorkflow.TASK_EXPIRED,
    NotificationWorkflow.STAGE_QUORUM_SKIPPED,
)


def _connector():
    """Soft import — the connector is optional; ImportError disables notifications."""
    try:
        from openg2p_notification import (
            NotificationFactory,
            NotificationRequest,
            Recipient,
            workflow_enabled,
        )

        return NotificationFactory, NotificationRequest, Recipient, workflow_enabled
    except ImportError:
        return None, None, None, None


@dataclass
class _Intent:
    """One recipient's pending send, built inside the engine transaction."""

    workflow: str
    request_id: str
    # Scope for the notification id: a stage, a task, or the request itself.
    ref: str
    recipient_id: str
    payload: dict


def _iso(value: Any) -> str:
    """Template-safe datetime: isoformat string or ""."""
    if value is None:
        return ""
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _staff_portal_base_url() -> str:
    """Staff-ui origin from Helm. Locale is hardcoded in the Novu workflow."""
    return os.environ.get("NOTIFICATION_STAFF_PORTAL_BASE_URL", "").rstrip("/")


def _staff_task_paths(request: ApprovalRequest) -> tuple[str, str]:
    """Staff-ui paths only. The workflow prefixes the origin and /en."""
    context = request.context if isinstance(getattr(request, "context", None), dict) else {}
    mnemonic = str(context.get("register_mnemonic") or "").strip().lower()
    artifact_type = request.artifact_type or ""
    artifact_id = request.artifact_id or ""
    if artifact_type == _INTAKE_FORM_ARTIFACT:
        list_path = "/tasks/intake-form"
        if mnemonic and artifact_id:
            return f"/tasks/intake-form/{mnemonic}/{artifact_id}", list_path
        return list_path, list_path
    list_path = "/tasks/change-request"
    if artifact_id:
        return f"/tasks/change-request/{artifact_id}", list_path
    return list_path, list_path


# ---------------------------------------------------------------------------
# Related-model / context resolvers — soft-fail; no HTTP in collect().
# ---------------------------------------------------------------------------


def resolve_artifact_type_label(artifact_type: Optional[str]) -> str:
    """Human label for the machine artifact_type (fallback: raw type or "")."""
    if not artifact_type:
        return ""
    return _ARTIFACT_TYPE_LABELS.get(artifact_type, artifact_type)


def resolve_request_context_fields(request: ApprovalRequest) -> dict[str, Any]:
    """Copy caller-stamped context keys already on the ApprovalRequest.

    Does not hit the DB — registry stamps record_name / mnemonics / ids when
    it opens the request. Missing keys become empty strings for templates.
    """
    context = (
        request.context if isinstance(getattr(request, "context", None), dict) else {}
    )
    out: dict[str, Any] = {}
    for key in _CONTEXT_DISPLAY_KEYS:
        value = context.get(key)
        out[key] = "" if value is None else value
    # Prefer explicit context ids; fall back to artifact_id for the matching kind.
    artifact_type = request.artifact_type or ""
    artifact_id = request.artifact_id or ""
    if not out.get("change_request_id") and artifact_type == _CHANGE_REQUEST_ARTIFACT:
        out["change_request_id"] = artifact_id
    if not out.get("submission_id") and artifact_type == _INTAKE_FORM_ARTIFACT:
        out["submission_id"] = artifact_id
    return out


def resolve_awe_display_context(request: ApprovalRequest) -> dict[str, Any]:
    """Request-level display fields available without extra lookups."""
    fields = resolve_request_context_fields(request)
    fields.update(
        artifact_type_label=resolve_artifact_type_label(
            getattr(request, "artifact_type", None)
        ),
        policy_key=getattr(request, "policy_key", None) or "",
        policy_id=getattr(request, "policy_id", None) or "",
        policy_version=getattr(request, "policy_version", None),
        source_service=getattr(request, "source_service", None) or "",
        requester=getattr(request, "requester", None) or "",
        request_status=getattr(request, "status", None) or "",
        current_stage_order=getattr(request, "current_stage_order", None),
        request_created_at=_iso(getattr(request, "created_at", None)),
        completed_at=_iso(getattr(request, "completed_at", None)),
    )
    return fields


async def resolve_policy_display(
    session: AsyncSession, policy_id: Optional[str]
) -> dict[str, Any]:
    """Soft-load ApprovalPolicy.name / description. Cached per session."""
    empty = {"policy_name": "", "policy_description": ""}
    if not policy_id:
        return empty
    cache = session.info.setdefault(_POLICY_CACHE_KEY, {})
    if policy_id in cache:
        return cache[policy_id]
    try:
        policy = await session.get(ApprovalPolicy, policy_id)
        if policy is None:
            cache[policy_id] = empty
            return empty
        result = {
            "policy_name": policy.name or "",
            "policy_description": policy.description or "",
        }
        cache[policy_id] = result
        return result
    except Exception:  # noqa: BLE001 — degrade to empty labels
        _logger.exception("resolve_policy_display failed for policy_id=%s", policy_id)
        cache[policy_id] = empty
        return empty


async def resolve_stage_display(
    session: AsyncSession, request: ApprovalRequest, stage_order
) -> dict[str, Any]:
    """Soft-load stage config for the given order. Cached per session."""
    empty = {
        "stage_id": "",
        "stage_name": "",
        "stage_order": stage_order,
        "stage_mode": "",
        "stage_mode_value": None,
        "sla_hours": None,
        "on_breach": "",
        "on_empty": "",
        "parallel_group": None,
    }
    if stage_order is None:
        return empty
    cache = session.info.setdefault(_STAGE_CACHE_KEY, {})
    key = (request.policy_id, stage_order)
    if key in cache:
        return cache[key]
    try:
        row = await session.execute(
            select(ApprovalStage).where(
                ApprovalStage.policy_id == request.policy_id,
                ApprovalStage.stage_order == stage_order,
            )
        )
        stage = row.scalar_one_or_none()
        if stage is None:
            cache[key] = empty
            return empty
        result = {
            "stage_id": stage.id or "",
            "stage_name": stage.name or "",
            "stage_order": stage.stage_order,
            "stage_mode": stage.mode or "",
            "stage_mode_value": stage.mode_value,
            "sla_hours": stage.sla_hours,
            "on_breach": stage.on_breach or "",
            "on_empty": stage.on_empty or "",
            "parallel_group": stage.parallel_group,
        }
        cache[key] = result
        return result
    except Exception:  # noqa: BLE001 — degrade to empty stage labels
        _logger.exception(
            "resolve_stage_display failed for policy_id=%s stage_order=%s",
            request.policy_id,
            stage_order,
        )
        cache[key] = empty
        return empty


async def resolve_task_display(
    session: AsyncSession, task_id: Optional[str]
) -> dict[str, Any]:
    """Soft-load task fields when the event carries a task_id."""
    empty = {
        "task_id": task_id or "",
        "assignee": "",
        "assignee_name": "",
        "task_kind": "",
        "task_status": "",
        "delegated_from": "",
        "claimed_at": "",
    }
    if not task_id:
        return empty
    try:
        task = await session.get(ApprovalTask, task_id)
        if task is None:
            return empty
        return {
            "task_id": task.id,
            "assignee": task.assignee or "",
            "assignee_name": task.assignee_name or "",
            "task_kind": task.kind or "",
            "task_status": task.status or "",
            "delegated_from": task.delegated_from or "",
            "claimed_at": _iso(task.claimed_at),
        }
    except Exception:  # noqa: BLE001
        _logger.exception("resolve_task_display failed for task_id=%s", task_id)
        return empty


async def collect(
    session: AsyncSession,
    event_type: str,
    event_payload: dict,
    request: ApprovalRequest,
) -> None:
    """Stash in-memory send intents for an engine event.

    Called from emit_event() inside the transaction — no HTTP, never raises.
    flush() sends the intents only after the transaction commits.
    """
    try:
        workflow = _WORKFLOW_BY_EVENT.get(event_type)
        if workflow is None:
            return
        _, _, _, enabled = _connector()
        if enabled is None or not enabled(str(workflow)):
            return
        recipients = _recipients(workflow, event_payload, request)
        if not recipients:
            return
        payload = await _payload(session, workflow, event_payload, request)
        intents = session.info.setdefault(_PENDING_KEY, [])
        for recipient_id in recipients:
            intents.append(
                _Intent(
                    workflow=str(workflow),
                    request_id=request.id,
                    ref=_ref(workflow, event_payload),
                    recipient_id=recipient_id,
                    payload=payload,
                )
            )
    except Exception:  # noqa: BLE001 — never fail the approval
        _logger.exception("notification collect failed for %s", event_type)


def _recipients(
    workflow: NotificationWorkflow,
    event_payload: dict,
    request: ApprovalRequest,
) -> List[str]:
    """Who to notify, purely from the event payload + request."""
    if workflow is NotificationWorkflow.STAGE_STARTED:
        # Open approver tasks on the stage — observers are not notified.
        return list(event_payload.get("approvers") or [])
    if workflow is NotificationWorkflow.TASK_REASSIGNED:
        return [event_payload["to"]] if event_payload.get("to") else []
    if workflow is NotificationWorkflow.STAGE_ESCALATED:
        return list(event_payload.get("added_approvers") or [])
    if workflow is NotificationWorkflow.TASK_EXPIRED:
        return [event_payload["assignee"]] if event_payload.get("assignee") else []
    if workflow is NotificationWorkflow.STAGE_QUORUM_SKIPPED:
        return list(event_payload.get("skipped_assignees") or [])
    # Terminal events — the staff requester, not the caller's service
    # identity. A request that never carried a real requester is skipped.
    requester = request.requester or ""
    if not requester or requester == (request.source_service or ""):
        return []
    return [requester]


def _ref(workflow: NotificationWorkflow, event_payload: dict) -> str:
    """Scope for the notification id: a stage, a task, or the request itself."""
    if workflow in (
        NotificationWorkflow.STAGE_STARTED,
        NotificationWorkflow.STAGE_ESCALATED,
        NotificationWorkflow.STAGE_QUORUM_SKIPPED,
    ):
        return f"stage-{event_payload.get('stage_order')}"
    if workflow is NotificationWorkflow.TASK_REASSIGNED:
        # The NEW task's id — a later reassignment is a new notification.
        return str(
            event_payload.get("new_task_id") or event_payload.get("task_id") or "task"
        )
    if workflow is NotificationWorkflow.TASK_EXPIRED:
        return str(event_payload.get("task_id") or "task")
    return "request"


async def _payload(
    session: AsyncSession,
    workflow: NotificationWorkflow,
    event_payload: dict,
    request: ApprovalRequest,
) -> dict:
    """Template variables — see the approval.* specs in the root novu-setup.py."""
    task_path, tasks_list_path = _staff_task_paths(request)
    payload: Dict[str, Any] = {
        "request_id": request.id,
        "artifact_type": request.artifact_type,
        "artifact_id": request.artifact_id,
        "staff_portal_base_url": _staff_portal_base_url(),
        "task_path": task_path,
        "tasks_list_path": tasks_list_path,
    }
    payload.update(resolve_awe_display_context(request))
    payload.update(await resolve_policy_display(session, request.policy_id))

    if workflow in _STAGE_EVENTS:
        stage_order = event_payload.get("stage_order")
        stage = await resolve_stage_display(session, request, stage_order)
        # Prefer the name already on the engine event when present.
        if event_payload.get("name"):
            stage = {**stage, "stage_name": event_payload["name"]}
        if event_payload.get("mode") and not stage.get("stage_mode"):
            stage = {
                **stage,
                "stage_mode": event_payload.get("mode") or "",
                "stage_mode_value": event_payload.get("mode_value"),
            }
        payload.update(stage)
        payload["due_at"] = _iso(event_payload.get("due_at"))

        task_id = event_payload.get("task_id") or event_payload.get("new_task_id")
        if task_id:
            payload.update(await resolve_task_display(session, task_id))

    if workflow is NotificationWorkflow.STAGE_STARTED:
        payload.update(
            requester=request.requester or "",
            due_at=_iso(event_payload.get("due_at")),
            approvers=list(event_payload.get("approvers") or []),
        )
    elif workflow is NotificationWorkflow.TASK_REASSIGNED:
        payload.update(
            reassigned_from=event_payload.get("from") or "",
            actor=event_payload.get("actor") or "",
            reason=event_payload.get("reason") or "",
            new_task_id=event_payload.get("new_task_id") or "",
            task_id=event_payload.get("task_id") or payload.get("task_id") or "",
        )
    elif workflow is NotificationWorkflow.STAGE_ESCALATED:
        payload.update(
            actor=event_payload.get("actor") or "",
            due_at=_iso(event_payload.get("due_at")),
            added_approvers=list(event_payload.get("added_approvers") or []),
        )
    elif workflow is NotificationWorkflow.TASK_EXPIRED:
        payload["due_at"] = _iso(event_payload.get("due_at"))
        if event_payload.get("assignee") and not payload.get("assignee"):
            payload["assignee"] = event_payload.get("assignee") or ""
    elif workflow is NotificationWorkflow.REQUEST_APPROVED:
        payload.update(
            actor=event_payload.get("actor") or "",
        )
    elif workflow is NotificationWorkflow.REQUEST_REJECTED:
        payload.update(
            actor=event_payload.get("actor") or "",
            reason=event_payload.get("reason")
            or await _rejection_reason(session, request.id),
        )
    elif workflow is NotificationWorkflow.REQUEST_CANCELLED:
        payload.update(
            actor=event_payload.get("actor") or "",
            reason=event_payload.get("reason") or "",
        )
    elif workflow is NotificationWorkflow.STAGE_QUORUM_SKIPPED:
        skipped = list(event_payload.get("skipped_assignees") or [])
        payload["skipped_assignees"] = skipped

    return payload


async def _stage_name(
    session: AsyncSession, request: ApprovalRequest, stage_order
) -> str:
    """Stage name for task-level events (stage_started carries it in the payload)."""
    stage = await resolve_stage_display(session, request, stage_order)
    return stage.get("stage_name") or ""


async def _rejection_reason(session: AsyncSession, request_id: str) -> str:
    """The rejecting decision's comment — the veto case carries no reason."""
    try:
        row = await session.execute(
            select(ApprovalDecision.comment)
            .where(
                ApprovalDecision.request_id == request_id,
                ApprovalDecision.action == "reject",
            )
            .order_by(ApprovalDecision.created_at.desc())
            .limit(1)
        )
        return row.scalar_one_or_none() or ""
    except Exception:  # noqa: BLE001 — degrade to an empty reason
        return ""


async def flush(session: AsyncSession) -> None:
    """Send the session's intents. Only ever called AFTER the transaction commits.

    One Keycloak email/name lookup per recipient (cached per flush; a missing
    profile still sends so the in-app step lands), then one send_bulk per
    event. Every failure is logged and swallowed — a notification must never
    fail the business request.
    """
    intents = session.info.pop(_PENDING_KEY, None)
    if not intents:
        return
    factory_cls, request_cls, recipient_cls, _ = _connector()
    if factory_cls is None:
        return
    profiles: Dict[str, Optional[dict]] = {}
    for recipient_id in {intent.recipient_id for intent in intents}:
        profiles[recipient_id] = await _user_profile(recipient_id)
    groups: Dict[tuple, List[_Intent]] = {}
    for intent in intents:
        groups.setdefault((intent.workflow, intent.ref), []).append(intent)
    for (workflow, ref), group in groups.items():
        requests = [
            request_cls(
                event=workflow,
                entity_id=intent.request_id,
                recipient=recipient_cls(
                    recipient_id=intent.recipient_id,
                    recipient_email=(profiles[intent.recipient_id] or {}).get("email"),
                    recipient_name=(profiles[intent.recipient_id] or {}).get("name"),
                ),
                payload=intent.payload,
                notification_id=(
                    f"{workflow}:{intent.request_id}:{ref}:{intent.recipient_id}"
                ),
            )
            for intent in group
        ]
        try:
            for offset in range(0, len(requests), _BULK_LIMIT):
                await asyncio.to_thread(
                    factory_cls.get_notifier().send_bulk,
                    requests[offset : offset + _BULK_LIMIT],
                )
        except Exception:  # noqa: BLE001 — never fail the business request
            _logger.exception(
                "notification send failed for workflow %s request %s ref %s",
                workflow,
                group[0].request_id,
                ref,
            )


async def _user_profile(recipient_id: str) -> Optional[dict]:
    """Email/name by Keycloak username; None still sends (in-app only)."""
    try:
        return await keycloak_admin_svc.get_user_by_username(recipient_id)
    except Exception as e:  # noqa: BLE001 — degrade, don't block the send
        _logger.warning("Keycloak lookup failed for %s: %s", recipient_id, e)
        return None


def discard(session: AsyncSession) -> None:
    """Drop pending intents — used when a transaction rolls back after collect()."""
    session.info.pop(_PENDING_KEY, None)
