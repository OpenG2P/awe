"""Staff notification helper: collect inside the transaction, send after commit."""

from __future__ import annotations

import sys
import types
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from jose import jwt
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from awe.services import notification
from awe.services.notification import NotificationWorkflow

from .conftest import auth_header


def _request(**overrides):
    base = dict(
        id="req-1",
        policy_id="pol-1",
        policy_key="registry.cr",
        artifact_type="registry.change_request",
        artifact_id="cr-1",
        source_service="svc-registry",
        requester="staff-req",
        context={},
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _enabled_connector(enabled=True, send_bulk=None):
    factory = MagicMock()
    notifier = MagicMock()
    if send_bulk is not None:
        notifier.send_bulk = send_bulk
    factory.get_notifier.return_value = notifier

    class Recipient:
        def __init__(self, recipient_id, recipient_email=None, recipient_name=None, recipient_phone=None):
            self.recipient_id = recipient_id
            self.recipient_email = recipient_email
            self.recipient_name = recipient_name
            self.recipient_phone = recipient_phone

    class NotificationRequest:
        def __init__(self, event, entity_id, recipient, payload=None, notification_id=None):
            self.event = event
            self.entity_id = entity_id
            self.recipient = recipient
            self.payload = payload or {}
            self.notification_id = notification_id

    def workflow_enabled(event):
        return enabled(event) if callable(enabled) else bool(enabled)

    return (factory, NotificationRequest, Recipient, workflow_enabled), notifier


def _session():
    return SimpleNamespace(info={})


def test_connector_import_success_and_missing():
    fake = types.ModuleType("openg2p_notification")
    fake.NotificationFactory = object()
    fake.NotificationRequest = object()
    fake.Recipient = object()
    fake.workflow_enabled = lambda event: True
    with patch.dict(sys.modules, {"openg2p_notification": fake}):
        factory, request_cls, recipient_cls, enabled = notification._connector()
    assert factory is fake.NotificationFactory
    assert request_cls is fake.NotificationRequest
    assert recipient_cls is fake.Recipient
    assert enabled("approval.stage_started") is True

    with patch.dict(sys.modules, {"openg2p_notification": None}):
        assert notification._connector() == (None, None, None, None)


def test_iso_and_task_paths(monkeypatch):
    assert notification._iso(None) == ""
    stamp = datetime(2026, 1, 2, 3, 4, tzinfo=timezone.utc)
    assert notification._iso(stamp) == stamp.isoformat()
    assert notification._iso("already") == "already"

    monkeypatch.delenv("NOTIFICATION_STAFF_PORTAL_BASE_URL", raising=False)
    assert notification._staff_portal_base_url() == ""
    monkeypatch.setenv("NOTIFICATION_STAFF_PORTAL_BASE_URL", "https://staff.example/")
    assert notification._staff_portal_base_url() == "https://staff.example"

    cr = _request()
    cr.context = {}
    task_path, list_path = notification._staff_task_paths(cr)
    assert task_path == "/tasks/change-request/cr-1"
    assert list_path == "/tasks/change-request"

    intake = _request(artifact_type="registry.intake_form", artifact_id="sub-1")
    intake.context = {"register_mnemonic": "Farmer"}
    task_path, list_path = notification._staff_task_paths(intake)
    assert task_path == "/tasks/intake-form/farmer/sub-1"
    assert list_path == "/tasks/intake-form"

    no_mn = _request(artifact_type="registry.intake_form", artifact_id="sub-1")
    no_mn.context = {}
    task_path, list_path = notification._staff_task_paths(no_mn)
    assert task_path == list_path == "/tasks/intake-form"
    empty = _request(artifact_id="")
    task_path, list_path = notification._staff_task_paths(empty)
    assert task_path == list_path == "/tasks/change-request"


def test_ref_scopes():
    assert notification._ref(NotificationWorkflow.STAGE_STARTED, {"stage_order": 2}) == "stage-2"
    assert notification._ref(NotificationWorkflow.STAGE_QUORUM_SKIPPED, {}) == "stage-None"
    assert notification._ref(
        NotificationWorkflow.TASK_REASSIGNED, {"new_task_id": "new", "task_id": "old"}
    ) == "new"
    assert notification._ref(NotificationWorkflow.TASK_REASSIGNED, {"task_id": "old"}) == "old"
    assert notification._ref(NotificationWorkflow.TASK_REASSIGNED, {}) == "task"
    assert notification._ref(NotificationWorkflow.TASK_EXPIRED, {"task_id": "t1"}) == "t1"
    assert notification._ref(NotificationWorkflow.TASK_EXPIRED, {}) == "task"
    assert notification._ref(NotificationWorkflow.REQUEST_APPROVED, {}) == "request"


def test_recipients_skip_service_identity_and_empty():
    request = _request(requester="svc-registry", source_service="svc-registry")
    assert notification._recipients(NotificationWorkflow.REQUEST_APPROVED, {}, request) == []
    assert notification._recipients(
        NotificationWorkflow.REQUEST_CANCELLED, {}, _request(requester=None)
    ) == []
    assert notification._recipients(NotificationWorkflow.STAGE_STARTED, {}, request) == []
    assert notification._recipients(NotificationWorkflow.TASK_REASSIGNED, {}, request) == []
    assert notification._recipients(NotificationWorkflow.STAGE_ESCALATED, {}, request) == []
    assert notification._recipients(NotificationWorkflow.TASK_EXPIRED, {}, request) == []
    assert notification._recipients(NotificationWorkflow.STAGE_QUORUM_SKIPPED, {}, request) == []
    assert notification._recipients(
        NotificationWorkflow.STAGE_QUORUM_SKIPPED,
        {"skipped_assignees": ["u-bob"]},
        request,
    ) == ["u-bob"]
    assert notification._recipients(
        NotificationWorkflow.REQUEST_APPROVED, {}, _request(requester="staff-req")
    ) == ["staff-req"]


@pytest.mark.asyncio
async def test_collect_skips_silent_unmapped_and_empty():
    session = _session()
    request = _request()
    connector, _ = _enabled_connector()

    await notification.collect(session, "request_created", {}, request)
    assert notification._PENDING_KEY not in session.info

    with patch("awe.services.notification._connector", return_value=(None, None, None, None)):
        await notification.collect(session, "stage_started", {"approvers": ["u-alice"]}, request)
    assert notification._PENDING_KEY not in session.info

    with patch("awe.services.notification._connector", return_value=_enabled_connector(enabled=False)[0]):
        await notification.collect(session, "stage_started", {"approvers": ["u-alice"]}, request)
    assert notification._PENDING_KEY not in session.info

    with patch("awe.services.notification._connector", return_value=connector):
        await notification.collect(session, "stage_started", {"approvers": []}, request)
        await notification.collect(
            session, "request_approved", {"actor": "u-alice"}, _request(requester="svc-registry")
        )
    assert notification._PENDING_KEY not in session.info


@pytest.mark.asyncio
async def test_collect_swallows_errors():
    session = _session()
    connector, _ = _enabled_connector()
    with patch("awe.services.notification._connector", return_value=connector), patch(
        "awe.services.notification._recipients", side_effect=RuntimeError("boom")
    ):
        await notification.collect(session, "stage_started", {"approvers": ["u-alice"]}, _request())
    assert notification._PENDING_KEY not in session.info


@pytest.mark.asyncio
async def test_collect_builds_intents_for_every_workflow():
    session = _session()
    request = _request(requester=None)
    connector, _ = _enabled_connector()
    stage = {"stage_order": 1, "name": "Officers", "due_at": "2026-01-02T00:00:00+00:00"}

    with patch("awe.services.notification._connector", return_value=connector):
        await notification.collect(
            session,
            "stage_started",
            {**stage, "approvers": ["u-alice", "u-bob"], "observers": ["u-obs"]},
            request,
        )
        await notification.collect(
            session,
            "task_reassigned",
            {"to": "u-cara", "from": "u-alice", "actor": "admin", "reason": "ooo",
             "new_task_id": "task-new", "stage_order": 1, "name": "Officers"},
            request,
        )
        await notification.collect(
            session,
            "stage_escalated",
            {"added_approvers": ["u-esc"], "actor": "sla-monitor", "stage_order": 1, "name": "Officers"},
            request,
        )
        await notification.collect(
            session,
            "task_expired",
            {"assignee": "u-alice", "task_id": "task-1", "stage_order": 1, "due_at": None},
            request,
        )
        staff = _request()
        await notification.collect(session, "request_approved", {"actor": "u-alice"}, staff)
        await notification.collect(
            session, "request_rejected", {"actor": "u-bob", "reason": "no"}, staff
        )
        await notification.collect(
            session, "request_cancelled", {"actor": "admin", "reason": "withdrawn"}, staff
        )
        await notification.collect(
            session,
            "stage_quorum_skipped",
            {"skipped_assignees": ["u-dana"], "stage_order": 1, "name": "Officers"},
            request,
        )

    intents = session.info[notification._PENDING_KEY]
    by_workflow = {}
    for intent in intents:
        by_workflow.setdefault(intent.workflow, []).append(intent.recipient_id)

    assert by_workflow[NotificationWorkflow.STAGE_STARTED] == ["u-alice", "u-bob"]
    assert "u-obs" not in by_workflow[NotificationWorkflow.STAGE_STARTED]
    started = intents[0]
    assert started.ref == "stage-1"
    assert started.payload["task_path"] == "/tasks/change-request/cr-1"
    assert started.payload["tasks_list_path"] == "/tasks/change-request"
    assert started.payload["due_at"] == "2026-01-02T00:00:00+00:00"
    assert started.payload["requester"] == ""
    assert started.payload["stage_name"] == "Officers"
    assert by_workflow[NotificationWorkflow.TASK_REASSIGNED] == ["u-cara"]
    assert by_workflow[NotificationWorkflow.STAGE_ESCALATED] == ["u-esc"]
    assert by_workflow[NotificationWorkflow.TASK_EXPIRED] == ["u-alice"]
    assert by_workflow[NotificationWorkflow.REQUEST_APPROVED] == ["staff-req"]
    assert by_workflow[NotificationWorkflow.REQUEST_REJECTED] == ["staff-req"]
    assert by_workflow[NotificationWorkflow.STAGE_QUORUM_SKIPPED] == ["u-dana"]
    skipped = next(i for i in intents if i.workflow == NotificationWorkflow.STAGE_QUORUM_SKIPPED)
    assert skipped.ref == "stage-1"
    assert skipped.payload["stage_name"] == "Officers"
    rejected = next(i for i in intents if i.workflow == NotificationWorkflow.REQUEST_REJECTED)
    assert rejected.payload["reason"] == "no"
    assert rejected.ref == "request"


@pytest.mark.asyncio
async def test_stage_name_and_rejection_reason_lookups(client):
    from awe.db import get_sessionmaker
    from awe.models import (
        ApprovalDecision,
        ApprovalPolicy,
        ApprovalRequest,
        ApprovalStage,
        ApprovalTask,
    )

    sm = get_sessionmaker()
    async with sm() as session:
        policy = ApprovalPolicy(
            policy_key="notify.lookup",
            version=1,
            name="Lookup",
            artifact_type="registry.change_request",
            status="active",
        )
        policy.stages = [ApprovalStage(stage_order=1, name="Officers", mode="all")]
        session.add(policy)
        await session.flush()
        request = ApprovalRequest(
            policy_id=policy.id,
            policy_key=policy.policy_key,
            policy_version=1,
            artifact_type="registry.change_request",
            artifact_id="cr-lookup",
            source_service="svc",
            requester="staff-req",
            context={},
            status="in_review",
        )
        session.add(request)
        await session.flush()
        task = ApprovalTask(
            request_id=request.id,
            stage_id=policy.stages[0].id,
            stage_order=1,
            assignee="u-bob",
            status="completed",
        )
        session.add(task)
        await session.flush()
        session.add(
            ApprovalDecision(
                request_id=request.id,
                task_id=task.id,
                stage_order=1,
                actor="u-bob",
                action="reject",
                comment="missing docs",
            )
        )
        await session.flush()

        assert await notification._stage_name(session, request, 1) == "Officers"
        assert await notification._stage_name(session, request, 1) == "Officers"
        assert await notification._stage_name(session, request, 9) == ""
        assert await notification._rejection_reason(session, request.id) == "missing docs"
        assert await notification._rejection_reason(session, "missing") == ""

        payload = await notification._payload(
            session,
            NotificationWorkflow.TASK_EXPIRED,
            {"stage_order": 1, "task_id": "t", "due_at": None},
            request,
        )
        assert payload["stage_name"] == "Officers"
        payload = await notification._payload(
            session,
            NotificationWorkflow.REQUEST_REJECTED,
            {"actor": "u-bob"},
            request,
        )
        assert payload["reason"] == "missing docs"

    broken = SimpleNamespace(info={})
    broken.execute = AsyncMock(side_effect=RuntimeError("db down"))
    assert await notification._stage_name(broken, request, 3) == ""
    assert await notification._stage_name(broken, request, 3) == ""
    assert await notification._rejection_reason(broken, request.id) == ""


@pytest.mark.asyncio
async def test_flush_sends_after_profiles_and_swallows_failures():
    session = _session()
    connector, notifier = _enabled_connector()
    request = _request()

    await notification.flush(session)
    notifier.send_bulk.assert_not_called()

    with patch("awe.services.notification._connector", return_value=connector):
        await notification.collect(
            session,
            "stage_started",
            {"approvers": ["u-alice", "u-bob"], "name": "Officers", "stage_order": 1, "due_at": None},
            request,
        )
        notification.discard(session)
        await notification.flush(session)
    notifier.send_bulk.assert_not_called()

    with patch("awe.services.notification._connector", return_value=connector):
        await notification.collect(
            session,
            "stage_started",
            {"approvers": ["u-alice", "u-bob"], "name": "Officers", "stage_order": 1},
            request,
        )
    with patch("awe.services.notification._connector", return_value=(None, None, None, None)):
        await notification.flush(session)
    notifier.send_bulk.assert_not_called()
    assert notification._PENDING_KEY not in session.info

    async def profiles(username):
        if username == "u-alice":
            return {"email": "alice@x.org", "name": "Alice"}
        return None

    with patch("awe.services.notification._connector", return_value=connector), patch(
        "awe.services.notification.keycloak_admin_svc.get_user_by_username", new=AsyncMock(side_effect=profiles)
    ):
        await notification.collect(
            session,
            "stage_started",
            {"approvers": ["u-alice", "u-alice", "u-bob"], "name": "Officers", "stage_order": 1},
            request,
        )
        await notification.flush(session)

    batch = notifier.send_bulk.call_args.args[0]
    assert [item.recipient.recipient_id for item in batch] == ["u-alice", "u-alice", "u-bob"]
    assert batch[0].recipient.recipient_email == "alice@x.org"
    assert batch[0].recipient.recipient_name == "Alice"
    assert batch[2].recipient.recipient_email is None
    assert batch[0].notification_id == "approval.stage_started:req-1:stage-1:u-alice"
    assert batch[0].payload["task_path"] == "/tasks/change-request/cr-1"
    assert batch[0].payload["tasks_list_path"] == "/tasks/change-request"

    notifier.send_bulk.reset_mock()
    notifier.send_bulk.side_effect = RuntimeError("novu down")
    with patch("awe.services.notification._connector", return_value=connector), patch(
        "awe.services.notification._BULK_LIMIT", 1
    ), patch(
        "awe.services.notification.keycloak_admin_svc.get_user_by_username",
        new=AsyncMock(side_effect=RuntimeError("keycloak down")),
    ):
        await notification.collect(
            session,
            "stage_started",
            {"approvers": ["u-alice", "u-bob"], "name": "Officers", "stage_order": 1},
            request,
        )
        await notification.flush(session)
    assert notifier.send_bulk.call_count == 1


def _token(sub: str, *, roles: list[str] | None = None) -> str:
    return jwt.encode(
        {
            "sub": sub,
            "preferred_username": sub,
            "realm_access": {"roles": roles or []},
            "email": f"{sub}@test",
        },
        "secret",
        algorithm="HS256",
    )


def _policy(key: str) -> dict:
    return {
        "policy_key": key,
        "name": key,
        "artifact_type": "registry.change_request",
        "stages": [
            {
                "name": "Officers",
                "stage_order": 1,
                "mode": "any-n",
                "mode_value": 1,
                "sla_hours": 24,
                "rules": [
                    {"rule_type": "user", "rule_value": {"user_id": "u-alice"}},
                    {"rule_type": "user", "rule_value": {"user_id": "u-obs"}, "kind": "observer"},
                ],
            }
        ],
    }


@pytest.mark.asyncio
async def test_create_request_sends_after_commit_approvers_only(
    client, admin_token, service_token
):
    h = auth_header(admin_token)
    await client.post("/v1/awe/policies", json=_policy("notify.stage"), headers=h)
    await client.post("/v1/awe/policies/notify.stage/versions/1/activate", headers=h)

    connector, notifier = _enabled_connector(enabled=lambda event: event == "approval.stage_started")

    async def profiles(username):
        return {"email": f"{username}@x.org", "name": username}

    with patch("awe.services.notification._connector", return_value=connector), patch(
        "awe.services.notification.keycloak_admin_svc.get_user_by_username",
        new=AsyncMock(side_effect=profiles),
    ):
        resp = await client.post(
            "/v1/awe/requests",
            json={
                "policy_key": "notify.stage",
                "artifact_type": "registry.change_request",
                "artifact_id": "cr-notify",
                "requester": "staff-req",
                "context": {},
            },
            headers=auth_header(service_token),
        )
    assert resp.status_code == 201, resp.text
    request_id = resp.json()["request_id"]
    batch = notifier.send_bulk.call_args.args[0]
    assert [item.recipient.recipient_id for item in batch] == ["u-alice"]
    assert batch[0].recipient.recipient_email == "u-alice@x.org"
    assert batch[0].notification_id == f"approval.stage_started:{request_id}:stage-1:u-alice"
    assert batch[0].payload["stage_name"] == "Officers"
    assert batch[0].payload["task_path"].endswith("/tasks/change-request/cr-notify")
    assert batch[0].payload["tasks_list_path"].endswith("/tasks/change-request")
    assert batch[0].payload["due_at"]


@pytest.mark.asyncio
async def test_send_failure_does_not_fail_the_request(client, admin_token, service_token):
    h = auth_header(admin_token)
    await client.post("/v1/awe/policies", json=_policy("notify.fail"), headers=h)
    await client.post("/v1/awe/policies/notify.fail/versions/1/activate", headers=h)
    connector, notifier = _enabled_connector()
    notifier.send_bulk.side_effect = RuntimeError("novu down")
    with patch("awe.services.notification._connector", return_value=connector), patch(
        "awe.services.notification.keycloak_admin_svc.get_user_by_username",
        new=AsyncMock(return_value=None),
    ):
        resp = await client.post(
            "/v1/awe/requests",
            json={
                "policy_key": "notify.fail",
                "artifact_type": "registry.change_request",
                "artifact_id": "cr-fail",
                "context": {},
            },
            headers=auth_header(service_token),
        )
    assert resp.status_code == 201, resp.text
    notifier.send_bulk.assert_called_once()


@pytest.mark.asyncio
async def test_later_events_notify_new_assignee_and_requester(
    client, admin_token, service_token
):
    h = auth_header(admin_token)
    await client.post("/v1/awe/policies", json=_policy("notify.later"), headers=h)
    await client.post("/v1/awe/policies/notify.later/versions/1/activate", headers=h)
    connector, notifier = _enabled_connector()

    with patch("awe.services.notification._connector", return_value=connector), patch(
        "awe.services.notification.keycloak_admin_svc.get_user_by_username",
        new=AsyncMock(return_value=None),
    ):
        resp = await client.post(
            "/v1/awe/requests",
            json={
                "policy_key": "notify.later",
                "artifact_type": "registry.change_request",
                "artifact_id": "cr-later",
                "requester": "staff-req",
                "context": {},
            },
            headers=auth_header(service_token),
        )
        assert resp.status_code == 201, resp.text
        request_id = resp.json()["request_id"]
        task_id = resp.json()["tasks"][0]["id"]
        # observer task is also returned; pick the approver
        task_id = next(t["id"] for t in resp.json()["tasks"] if t["assignee"] == "u-alice")

        resp = await client.post(
            f"/v1/awe/tasks/{task_id}/reassign",
            json={"new_assignee": "u-cara", "reason": "out of office"},
            headers=h,
        )
        assert resp.status_code == 200, resp.text
        new_task_id = resp.json()["id"]

        resp = await client.post(
            f"/v1/awe/tasks/{new_task_id}/decision",
            json={"action": "reject", "comment": "missing docs"},
            headers=auth_header(_token("u-cara")),
        )
        assert resp.status_code == 201, resp.text

    events = [call.args[0][0].event for call in notifier.send_bulk.call_args_list]
    assert NotificationWorkflow.TASK_REASSIGNED in events
    assert NotificationWorkflow.REQUEST_REJECTED in events
    reassigned = next(
        call.args[0][0]
        for call in notifier.send_bulk.call_args_list
        if call.args[0][0].event == NotificationWorkflow.TASK_REASSIGNED
    )
    assert reassigned.recipient.recipient_id == "u-cara"
    assert reassigned.notification_id.endswith(f":{new_task_id}:u-cara")
    assert reassigned.payload["reason"] == "out of office"
    assert reassigned.payload["request_id"] == request_id
    rejected = next(
        call.args[0][0]
        for call in notifier.send_bulk.call_args_list
        if call.args[0][0].event == NotificationWorkflow.REQUEST_REJECTED
    )
    assert rejected.recipient.recipient_id == "staff-req"
    assert rejected.payload["reason"] == "missing docs"


@pytest.mark.asyncio
async def test_quorum_skip_and_request_approved_notify_staff(
    client, admin_token, service_token
):
    h = auth_header(admin_token)
    policy = _policy("notify.quorum")
    policy["stages"][0]["rules"] = [
        {"rule_type": "user", "rule_value": {"user_id": "u-alice"}},
        {"rule_type": "user", "rule_value": {"user_id": "u-bob"}},
        {"rule_type": "user", "rule_value": {"user_id": "u-obs"}, "kind": "observer"},
    ]
    await client.post("/v1/awe/policies", json=policy, headers=h)
    await client.post("/v1/awe/policies/notify.quorum/versions/1/activate", headers=h)
    connector, notifier = _enabled_connector()

    with patch("awe.services.notification._connector", return_value=connector), patch(
        "awe.services.notification.keycloak_admin_svc.get_user_by_username",
        new=AsyncMock(return_value=None),
    ):
        resp = await client.post(
            "/v1/awe/requests",
            json={
                "policy_key": "notify.quorum",
                "artifact_type": "registry.change_request",
                "artifact_id": "cr-quorum",
                "requester": "staff-req",
                "context": {},
            },
            headers=auth_header(service_token),
        )
        assert resp.status_code == 201, resp.text
        alice_task = next(t["id"] for t in resp.json()["tasks"] if t["assignee"] == "u-alice")
        notifier.send_bulk.reset_mock()
        resp = await client.post(
            f"/v1/awe/tasks/{alice_task}/decision",
            json={"action": "approve"},
            headers=auth_header(_token("u-alice")),
        )
        assert resp.status_code == 201, resp.text

    events = [call.args[0][0].event for call in notifier.send_bulk.call_args_list]
    assert NotificationWorkflow.STAGE_QUORUM_SKIPPED in events
    assert NotificationWorkflow.REQUEST_APPROVED in events
    skipped = next(
        call.args[0][0]
        for call in notifier.send_bulk.call_args_list
        if call.args[0][0].event == NotificationWorkflow.STAGE_QUORUM_SKIPPED
    )
    assert skipped.recipient.recipient_id == "u-bob"
    assert skipped.payload["stage_name"] == "Officers"
    approved = next(
        call.args[0][0]
        for call in notifier.send_bulk.call_args_list
        if call.args[0][0].event == NotificationWorkflow.REQUEST_APPROVED
    )
    assert approved.recipient.recipient_id == "staff-req"


@pytest.mark.asyncio
async def test_cancel_notifies_requester_and_rollback_discards(
    client, admin_token, service_token
):
    h = auth_header(admin_token)
    await client.post("/v1/awe/policies", json=_policy("notify.cancel"), headers=h)
    await client.post("/v1/awe/policies/notify.cancel/versions/1/activate", headers=h)
    connector, notifier = _enabled_connector()

    with patch("awe.services.notification._connector", return_value=connector), patch(
        "awe.services.notification.keycloak_admin_svc.get_user_by_username",
        new=AsyncMock(return_value={"email": "req@x.org", "name": "Req"}),
    ):
        resp = await client.post(
            "/v1/awe/requests",
            json={
                "policy_key": "notify.cancel",
                "artifact_type": "registry.change_request",
                "artifact_id": "cr-cancel",
                "requester": "staff-req",
                "context": {},
            },
            headers=auth_header(service_token),
        )
        request_id = resp.json()["request_id"]
        notifier.send_bulk.reset_mock()
        resp = await client.post(
            f"/v1/awe/requests/{request_id}/cancel",
            json={"reason": "withdrawn"},
            headers=h,
        )
        assert resp.status_code == 200, resp.text

    cancelled = notifier.send_bulk.call_args.args[0][0]
    assert cancelled.event == NotificationWorkflow.REQUEST_CANCELLED
    assert cancelled.recipient.recipient_id == "staff-req"
    assert cancelled.recipient.recipient_email == "req@x.org"
    assert cancelled.payload["reason"] == "withdrawn"

    from awe.models import IdempotencyKey

    real_flush = AsyncSession.flush

    async def flush_fail_on_idempotency_insert(self, *args, **kwargs):
        if any(isinstance(obj, IdempotencyKey) for obj in self.new):
            raise IntegrityError("INSERT", {}, Exception("dup"))
        return await real_flush(self, *args, **kwargs)

    notifier.send_bulk.reset_mock()
    with patch("awe.services.notification._connector", return_value=connector), patch(
        "awe.services.notification.keycloak_admin_svc.get_user_by_username",
        new=AsyncMock(return_value=None),
    ), patch.object(AsyncSession, "flush", flush_fail_on_idempotency_insert):
        resp = await client.post(
            "/v1/awe/requests",
            json={
                "policy_key": "notify.cancel",
                "artifact_type": "registry.change_request",
                "artifact_id": "cr-rolled-back",
                "requester": "staff-req",
                "context": {},
            },
            headers={**auth_header(service_token), "Idempotency-Key": "notify-rollback"},
        )
    assert resp.status_code == 201
    notifier.send_bulk.assert_not_called()


@pytest.mark.asyncio
async def test_sla_tick_notifies_expired_assignee(client, admin_token, service_token):
    from datetime import timedelta

    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from awe.db import get_engine
    from awe.models import ApprovalTask
    from awe.models.base import utcnow
    from awe.workers.sla_monitor import _tick

    h = auth_header(admin_token)
    await client.post("/v1/awe/policies", json=_policy("notify.sla"), headers=h)
    await client.post("/v1/awe/policies/notify.sla/versions/1/activate", headers=h)
    resp = await client.post(
        "/v1/awe/requests",
        json={
            "policy_key": "notify.sla",
            "artifact_type": "registry.change_request",
            "artifact_id": "cr-sla",
            "requester": "staff-req",
            "context": {},
        },
        headers=auth_header(service_token),
    )
    request_id = resp.json()["request_id"]
    engine = get_engine()
    sm = async_sessionmaker(engine, expire_on_commit=False)
    async with sm() as session:
        row = await session.execute(
            select(ApprovalTask).where(
                ApprovalTask.request_id == request_id, ApprovalTask.kind == "approver"
            )
        )
        task = row.scalar_one()
        task.due_at = utcnow() - timedelta(hours=2)
        await session.commit()

    connector, notifier = _enabled_connector(enabled=lambda event: event == "approval.task_expired")
    with patch("awe.services.notification._connector", return_value=connector), patch(
        "awe.services.notification.keycloak_admin_svc.get_user_by_username",
        new=AsyncMock(return_value=None),
    ):
        await _tick(sm)

    batch = notifier.send_bulk.call_args.args[0]
    assert len(batch) == 1
    assert batch[0].event == NotificationWorkflow.TASK_EXPIRED
    assert batch[0].recipient.recipient_id == "u-alice"
    assert batch[0].payload["request_id"] == request_id
    assert batch[0].payload["stage_name"] == "Officers"


@pytest.mark.asyncio
async def test_session_scope_flushes_only_after_commit(client):
    from awe.db import session_scope

    sent = []

    async def fake_flush(session):
        sent.append(session.in_transaction())

    with patch("awe.services.notification.flush", fake_flush):
        async with session_scope():
            pass
    assert sent == [False]

    sent.clear()
    with patch("awe.services.notification.flush", fake_flush):
        with pytest.raises(RuntimeError):
            async with session_scope():
                raise RuntimeError("rolled back")
    assert sent == []
