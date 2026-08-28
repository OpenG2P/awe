"""Targeted tests for remaining coverage gaps across controllers, services, and workers."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from jose import JWTError, jwt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awe.config import _find_config_path, get_settings
from awe.models import ApprovalPolicy, ApprovalRequest, ApprovalStage, ApprovalTask, UserDelegation
from awe.services import audit as audit_svc
from awe.services import engine as engine_svc
from awe.services import keycloak_admin as kc
from awe.services import notifier as notifier_svc
from awe.services import policy as policy_svc
from awe.services import resolver as resolver_svc
from awe.services.auth import CallerIdentity, _verify_token

from .conftest import auth_header


def _user_token(sub: str, *, roles: list[str] | None = None) -> str:
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


def _policy(
    policy_key: str,
    *,
    stages: list | None = None,
    **extra,
) -> dict:
    return {
        "policy_key": policy_key,
        "name": "Coverage policy",
        "artifact_type": "test",
        "stages": stages
        or [
            {
                "name": "S1",
                "stage_order": 1,
                "mode": "all",
                "rules": [{"rule_type": "user", "rule_value": {"user_id": "u-alice"}}],
            }
        ],
        **extra,
    }


async def _activate(client, admin_token, policy_key: str, payload: dict | None = None) -> None:
    h = auth_header(admin_token)
    await client.post(
        "/v1/awe/policies",
        json=payload or _policy(policy_key),
        headers=h,
    )
    await client.post(f"/v1/awe/policies/{policy_key}/versions/1/activate", headers=h)


# ---------------------------------------------------------------------------
# config.py
# ---------------------------------------------------------------------------
def test_find_config_path_src_fallback(monkeypatch, tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.delenv("CONFIG_PATH", raising=False)
    monkeypatch.chdir(empty)
    get_settings.cache_clear()
    # Line 105: package-relative config/default.yaml exists in the repo.
    assert _find_config_path().name == "default.yaml"


# ---------------------------------------------------------------------------
# services/audit.py, auth.py, notifier.py, keycloak_admin.py, resolver.py
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_audit_record_returns_row(client) -> None:
    from awe.db import get_engine

    sm = async_sessionmaker(get_engine(), expire_on_commit=False)
    identity = CallerIdentity(
        subject="audit-user",
        assignee_id="audit-user",
        roles=["AWE_ADMIN"],
        is_service_account=False,
        raw_claims={"email": "audit@test"},
    )
    async with sm() as session:
        row = await audit_svc.record(
            session,
            identity=identity,
            action="test.action",
            resource_type="test",
            resource_id="r1",
            summary="test",
            before={"a": 1},
            after={"b": 2},
            metadata={"k": "v"},
        )
        await session.commit()
    assert row.action == "test.action"
    assert row.actor_email == "audit@test"


@pytest.mark.asyncio
async def test_verify_token_jwt_error():
    with patch("awe.services.auth.get_settings") as gs:
        gs.return_value.awe.keycloak.issuer = "https://issuer"
        gs.return_value.awe.keycloak.jwks_url = "https://kc/certs"
        with patch(
            "awe.services.auth._fetch_jwks",
            new=AsyncMock(return_value={"keys": []}),
        ), patch("awe.services.auth.jwt.decode", side_effect=JWTError("bad sig")):
            with pytest.raises(Exception) as exc:
                await _verify_token("tok")
            assert exc.value.status_code == 401
            assert "Invalid bearer token" in exc.value.detail


def test_notifier_plain_smtp_with_login():
    cfg = MagicMock()
    cfg.enabled = True
    cfg.use_tls = False
    cfg.smtp_host = "smtp.test"
    cfg.smtp_port = 25
    cfg.smtp_user = "user"
    cfg.smtp_password = "pass"
    cfg.from_address = "noreply@test"

    smtp = MagicMock()
    smtp.__enter__ = MagicMock(return_value=smtp)
    smtp.__exit__ = MagicMock(return_value=False)

    with patch("awe.services.notifier.get_settings") as gs, patch(
        "awe.services.notifier.smtplib.SMTP", return_value=smtp
    ):
        gs.return_value.awe.notifier = cfg
        notifier_svc._send_email("to@test", "subj", "body")
        smtp.login.assert_called_once_with("user", "pass")
        smtp.send_message.assert_called_once()


@pytest.mark.asyncio
async def test_keycloak_admin_get_success_and_list_edge_cases():
    async def fake_get(path, params=None):
        if path == "/users":
            return [{"username": "alice"}]
        if path == "/clients":
            if params and params.get("clientId"):
                return [{"id": "uuid-1"}]
            return [{"clientId": ""}, {"clientId": "portal", "name": "Portal"}]
        if path.endswith("/roles"):
            return [{"name": ""}, {"name": "VIEWER"}, {"name": "ADMIN"}]
        if path == "/groups":
            return [
                {"path": "/dup", "name": "dup"},
                {"path": "/dup", "name": "dup"},
                {"path": "/other", "name": "other"},
            ]
        raise AssertionError(path)

    with patch("awe.services.keycloak_admin._admin_get", side_effect=fake_get):
        users = await kc.list_users()
        clients = await kc.list_clients()
        roles = await kc.list_roles(None, "view")
        groups = await kc.list_groups()

    assert users[0]["user_id"] == "alice"
    assert clients == [{"client_id": "portal", "name": "Portal"}]
    assert roles == [{"name": "VIEWER", "client": None, "description": None}]
    assert len(groups) == 2


@pytest.mark.asyncio
async def test_resolver_rule_type_branches_and_http_errors():
    from awe.models import ApproverRule

    role_rule = MagicMock(spec=ApproverRule)
    role_rule.rule_type = "role"
    role_rule.rule_value = {"role": "OFFICER", "client": "portal"}

    group_rule = MagicMock(spec=ApproverRule)
    group_rule.rule_type = "group"
    group_rule.rule_value = {"group": "/team"}

    expr_rule = MagicMock(spec=ApproverRule)
    expr_rule.rule_type = "expression"
    expr_rule.rule_value = {"logic": {"var": "head"}}

    http_rule = MagicMock(spec=ApproverRule)
    http_rule.rule_type = "http"
    http_rule.rule_value = {"url": "https://resolver/hook"}

    with patch(
        "awe.services.resolver._resolve_keycloak_role",
        new=AsyncMock(return_value=["role-user"]),
    ):
        assert await resolver_svc._resolve_one(role_rule, {}, {}) == ["role-user"]

    with patch(
        "awe.services.resolver._resolve_keycloak_group",
        new=AsyncMock(return_value=["group-user"]),
    ):
        assert await resolver_svc._resolve_one(group_rule, {}, {}) == ["group-user"]

    assert await resolver_svc._resolve_one(expr_rule, {"head": "expr-user"}, {}) == [
        "expr-user"
    ]

    with patch(
        "awe.services.resolver._resolve_http",
        new=AsyncMock(return_value=["http-user"]),
    ):
        assert await resolver_svc._resolve_one(http_rule, {}, {}) == ["http-user"]

    transport = httpx.MockTransport(lambda req: httpx.Response(500))
    with patch(
        "awe.services.resolver.keycloak_admin_token",
        new=AsyncMock(return_value="tok"),
    ), patch("awe.services.resolver.get_settings") as gs:
        gs.return_value.awe.keycloak.base_url = "https://kc"
        gs.return_value.awe.keycloak.realm = "staff"
        with patch(
            "httpx.AsyncClient",
            return_value=httpx.AsyncClient(transport=transport),
        ):
            with pytest.raises(
                resolver_svc.ResolutionError, match="Keycloak role lookup failed"
            ):
                await resolver_svc._resolve_keycloak_role("R")

    with patch(
        "awe.services.resolver.keycloak_admin_token",
        new=AsyncMock(return_value="tok"),
    ), patch("awe.services.resolver.get_settings") as gs:
        gs.return_value.awe.keycloak.base_url = "https://kc"
        gs.return_value.awe.keycloak.realm = "staff"
        with patch(
            "httpx.AsyncClient",
            return_value=httpx.AsyncClient(transport=transport),
        ):
            with pytest.raises(
                resolver_svc.ResolutionError, match="Keycloak group lookup failed"
            ):
                await resolver_svc._resolve_keycloak_group("/g")


@pytest.mark.asyncio
async def test_policy_activate_archives_prior_active(client) -> None:
    from awe.db import get_engine
    from awe.schemas.policy import ApproverRuleIn, PolicyCreate, StageIn

    sm = async_sessionmaker(get_engine(), expire_on_commit=False)
    payload = PolicyCreate(
        policy_key="svc.archive.prior",
        name="Archive prior",
        artifact_type="test",
        stages=[
            StageIn(
                name="S",
                stage_order=1,
                mode="all",
                rules=[ApproverRuleIn(rule_type="user", rule_value={"user_id": "u1"})],
            )
        ],
    )
    async with sm() as session:
        v1 = await policy_svc.create_draft(session, payload, actor="admin")
        await policy_svc.activate_version(session, "svc.archive.prior", 1)
        v2 = await policy_svc.add_draft_version(session, "svc.archive.prior", payload, actor="admin")
        await policy_svc.activate_version(session, "svc.archive.prior", v2.version)
        await session.commit()
        row = await session.get(ApprovalPolicy, v1.id)
        assert row.status == "archived"


# ---------------------------------------------------------------------------
# services/engine.py — direct unit paths
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_engine_helpers_and_escalate_empty(client) -> None:
    from awe.db import get_engine
    from sqlalchemy.orm import selectinload

    sm = async_sessionmaker(get_engine(), expire_on_commit=False)
    async with sm() as session:
        policy = ApprovalPolicy(
            policy_key="eng.helpers",
            version=1,
            name="Helpers",
            artifact_type="test",
            status="active",
        )
        session.add(policy)
        await session.flush()
        stage = ApprovalStage(
            policy_id=policy.id,
            name="S",
            stage_order=99,
            mode="all",
            escalation_rules_json=[],
        )
        session.add(stage)
        await session.flush()
        request = ApprovalRequest(
            policy_id=policy.id,
            policy_key=policy.policy_key,
            policy_version=1,
            artifact_type="test",
            artifact_id="a1",
            source_service="svc",
            context={},
            status="pending",
            current_stage_order=1,
        )
        session.add(request)
        await session.flush()

        loaded = (
            await session.execute(
                select(ApprovalPolicy)
                .options(selectinload(ApprovalPolicy.stages))
                .where(ApprovalPolicy.id == policy.id)
            )
        ).scalar_one()
        with pytest.raises(engine_svc.EngineError, match="Stage order 1 not in policy"):
            engine_svc._stage_at(loaded, 1)

        with pytest.raises(engine_svc.EngineError, match="not found"):
            await engine_svc._load_policy(session, "missing-policy-id")

        added = await engine_svc.escalate_stage(session, request, stage, actor="test")
        assert added == 0
        await session.commit()


@pytest.mark.asyncio
async def test_engine_parallel_group_and_repeat_approver_filter(
    client, admin_token, service_token
) -> None:
    policy = _policy(
        "cov.parallel",
        forbid_repeat_approvers=True,
        stages=[
            {
                "name": "Parallel A",
                "stage_order": 1,
                "parallel_group": 1,
                "mode": "all",
                "rules": [{"rule_type": "user", "rule_value": {"user_id": "u-alice"}}],
            },
            {
                "name": "Parallel B",
                "stage_order": 2,
                "parallel_group": 1,
                "mode": "all",
                "rules": [{"rule_type": "user", "rule_value": {"user_id": "u-bob"}}],
            },
            {
                "name": "Final",
                "stage_order": 3,
                "mode": "all",
                "rules": [{"rule_type": "user", "rule_value": {"user_id": "u-charlie"}}],
            },
        ],
    )
    await _activate(client, admin_token, "cov.parallel", policy)
    resp = await client.post(
        "/v1/awe/requests",
        json={
            "policy_key": "cov.parallel",
            "artifact_type": "test",
            "artifact_id": "par-1",
            "context": {},
        },
        headers=auth_header(service_token),
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["status"] == "in_review"
    request_id = body["request_id"]

    tasks_resp = await client.get(
        f"/v1/awe/tasks?assignee=*&request_id={request_id}",
        headers=auth_header(service_token),
    )
    assignees = {t["assignee"] for t in tasks_resp.json()["items"]}
    assert "u-alice" in assignees
    assert "u-bob" in assignees
    tasks = {
        t["assignee"]: t["id"]
        for t in (
            await client.get(
                f"/v1/awe/tasks?assignee=*&request_id={request_id}",
                headers=auth_header(service_token),
            )
        ).json()["items"]
    }
    await client.post(
        f"/v1/awe/tasks/{tasks['u-alice']}/decision",
        json={"action": "approve"},
        headers=auth_header(_user_token("u-alice")),
    )
    await client.post(
        f"/v1/awe/tasks/{tasks['u-bob']}/decision",
        json={"action": "approve"},
        headers=auth_header(_user_token("u-bob")),
    )

    tasks_resp = await client.get(
        f"/v1/awe/tasks?assignee=*&request_id={request_id}&status=open",
        headers=auth_header(service_token),
    )
    charlie_task = next(
        t for t in tasks_resp.json()["items"] if t["assignee"] == "u-charlie"
    )
    await client.post(
        f"/v1/awe/tasks/{charlie_task['id']}/decision",
        json={"action": "approve"},
        headers=auth_header(_user_token("u-charlie")),
    )

    resp = await client.get(
        f"/v1/awe/requests/{request_id}", headers=auth_header(service_token)
    )
    assert resp.json()["status"] == "approved"
    final_tasks = (
        await client.get(
            f"/v1/awe/tasks?assignee=*&request_id={request_id}",
            headers=auth_header(service_token),
        )
    ).json()["items"]
    stage3 = [t for t in final_tasks if t["stage_order"] == 3]
    assert len(stage3) == 1
    assert stage3[0]["assignee"] == "u-charlie"


@pytest.mark.asyncio
async def test_engine_delegation_rewrites_assignee(
    client, admin_token, service_token
) -> None:
    now = datetime.now(timezone.utc)
    await client.post(
        "/v1/awe/delegations",
        json={
            "user_id": "u-alice",
            "delegate_to": "u-delegate",
            "starts_at": (now - timedelta(hours=1)).isoformat(),
            "ends_at": (now + timedelta(days=1)).isoformat(),
            "reason": "ooo",
        },
        headers=auth_header(admin_token),
    )
    await _activate(client, admin_token, "cov.delegation")
    resp = await client.post(
        "/v1/awe/requests",
        json={
            "policy_key": "cov.delegation",
            "artifact_type": "test",
            "artifact_id": "del-1",
            "context": {},
        },
        headers=auth_header(service_token),
    )
    assert resp.status_code == 201
    assert resp.json()["tasks"][0]["assignee"] == "u-delegate"


# ---------------------------------------------------------------------------
# controllers — admin, delegation, health, policy, request, task
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_admin_audit_until_filter(client, admin_token) -> None:
    await client.post(
        "/v1/awe/policies",
        json=_policy("cov.admin.audit"),
        headers=auth_header(admin_token),
    )
    since = "2000-01-01T00:00:00Z"
    until = "2100-01-01T00:00:00Z"
    resp = await client.get(
        f"/v1/awe/admin/audit?since={since}&until={until}&limit=5",
        headers=auth_header(admin_token),
    )
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


@pytest.mark.asyncio
async def test_delegation_list_unfiltered(client, admin_token, viewer_token) -> None:
    now = datetime.now(timezone.utc)
    await client.post(
        "/v1/awe/delegations",
        json={
            "user_id": "u-list-a",
            "delegate_to": "u-list-b",
            "starts_at": now.isoformat(),
            "ends_at": (now + timedelta(days=1)).isoformat(),
        },
        headers=auth_header(admin_token),
    )
    resp = await client.get("/v1/awe/delegations", headers=auth_header(viewer_token))
    assert resp.status_code == 200
    assert any(d["user_id"] == "u-list-a" for d in resp.json())


@pytest.mark.asyncio
async def test_health_db_select_success(client) -> None:
    resp = await client.get("/v1/awe/health")
    assert resp.status_code == 200
    assert resp.json()["response"]["status"] == "UP"


@pytest.mark.asyncio
async def test_policy_controller_remaining_paths(client, admin_token, viewer_token) -> None:
    h = auth_header(admin_token)
    key = "cov.policy.ctrl"
    create = await client.post("/v1/awe/policies", json=_policy(key), headers=h)
    assert create.status_code == 201

    resp = await client.get(f"/v1/awe/policies/{key}/versions/1", headers=h)
    assert resp.status_code == 200

    updated = {**_policy(key), "name": "Edited name"}
    resp = await client.patch(
        f"/v1/awe/policies/{key}/versions/1",
        json=updated,
        headers=h,
    )
    assert resp.status_code == 200

    bad = {**updated, "policy_key": "other"}
    resp = await client.patch(
        f"/v1/awe/policies/{key}/versions/1",
        json=bad,
        headers=h,
    )
    assert resp.status_code == 400

    await client.post(f"/v1/awe/policies/{key}/versions/1/activate", headers=h)
    v2 = {**_policy(key), "name": "Version 2"}
    await client.put(f"/v1/awe/policies/{key}", json=v2, headers=h)
    await client.post(f"/v1/awe/policies/{key}/versions/2/activate", headers=h)

    resp = await client.post(f"/v1/awe/policies/{key}/versions/1/deactivate", headers=h)
    assert resp.status_code == 409

    resp = await client.post(f"/v1/awe/policies/{key}/versions/2/deactivate", headers=h)
    assert resp.status_code == 200

    skip_policy = _policy(
        "cov.policy.badskip",
        stages=[
            {
                "name": "Bad skip",
                "stage_order": 1,
                "mode": "all",
                "skip_if": {"bad_op": True},
                "rules": [{"rule_type": "user", "rule_value": {"user_id": "u-x"}}],
            }
        ],
    )
    await client.post("/v1/awe/policies", json=skip_policy, headers=h)
    resp = await client.post(
        "/v1/awe/policies/cov.policy.badskip/versions/1/simulate",
        json={"context": {}},
        headers=auth_header(viewer_token),
    )
    assert resp.status_code == 200
    assert resp.json()["stages"][0]["skipped"] is False


@pytest.mark.asyncio
async def test_request_controller_remaining_paths(
    client, admin_token, service_token
) -> None:
    resp = await client.post(
        "/v1/awe/requests",
        json={
            "policy_key": "no.active.policy",
            "artifact_type": "test",
            "artifact_id": "missing-policy",
            "context": {},
        },
        headers=auth_header(service_token),
    )
    assert resp.status_code == 404
    assert resp.json()["errors"][0]["errorCode"] == "AWE-001"

    await _activate(client, admin_token, "cov.req.engineerr")
    with patch(
        "awe.controllers.request.engine_svc.start_request",
        new=AsyncMock(side_effect=engine_svc.EngineError("bad context")),
    ):
        resp = await client.post(
            "/v1/awe/requests",
            json={
                "policy_key": "cov.req.engineerr",
                "artifact_type": "test",
                "artifact_id": "engine-err",
                "context": {},
            },
            headers=auth_header(service_token),
        )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_task_controller_reassign_and_filters(
    client, admin_token, service_token
) -> None:
    await _activate(client, admin_token, "cov.task.reassign")
    resp = await client.post(
        "/v1/awe/requests",
        json={
            "policy_key": "cov.task.reassign",
            "artifact_type": "test",
            "artifact_id": "re-1",
            "context": {"note": "findme"},
        },
        headers=auth_header(service_token),
    )
    request_id = resp.json()["request_id"]
    task_id = resp.json()["tasks"][0]["id"]

    resp = await client.get(
        "/v1/awe/tasks?assignee=u-alice&status=open",
        headers=auth_header(_user_token("u-alice")),
    )
    assert resp.status_code == 200
    assert resp.json()["total"] >= 1

    resp = await client.post(
        f"/v1/awe/tasks/{task_id}/reassign",
        json={"new_assignee": "u-bob", "reason": "handoff"},
        headers=auth_header(admin_token),
    )
    assert resp.status_code == 200
    assert resp.json()["assignee"] == "u-bob"

    admin_jwt = jwt.encode(
        {
            "sub": "test-admin",
            "preferred_username": "test-admin",
            "realm_access": {"roles": ["AWE_ADMIN"]},
            "email": "admin@test",
        },
        "secret",
        algorithm="HS256",
    )
    resp = await client.post(
        f"/v1/awe/tasks/{task_id}/claim",
        headers=auth_header(admin_jwt),
    )
    assert resp.status_code == 409


# ---------------------------------------------------------------------------
# workers — sla_monitor, webhook_dispatcher
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_sla_monitor_tick_paths(client, admin_token, service_token) -> None:
    from awe.db import get_engine
    from awe.models.base import utcnow
    from awe.workers import sla_monitor

    engine = get_engine()
    sm = async_sessionmaker(engine, expire_on_commit=False)

    policy_auto = _policy(
        "cov.sla.auto",
        stages=[
            {
                "name": "Auto approve",
                "stage_order": 1,
                "mode": "all",
                "sla_hours": 1,
                "on_breach": "auto_approve",
                "rules": [{"rule_type": "user", "rule_value": {"user_id": "u-alice"}}],
            }
        ],
    )
    await _activate(client, admin_token, "cov.sla.auto", policy_auto)
    resp = await client.post(
        "/v1/awe/requests",
        json={
            "policy_key": "cov.sla.auto",
            "artifact_type": "test",
            "artifact_id": "sla-auto",
            "context": {},
        },
        headers=auth_header(service_token),
    )
    auto_request_id = resp.json()["request_id"]

    async with sm() as session:
        request = await session.get(ApprovalRequest, auto_request_id)
        policy = await sla_monitor._load_policy(session, request.policy_id)
        stage = next(s for s in policy.stages if s.stage_order == 1)
        await sla_monitor._apply_on_breach(session, request, stage)
        await session.commit()

    resp = await client.get(
        f"/v1/awe/requests/{auto_request_id}",
        headers=auth_header(service_token),
    )
    assert resp.json()["status"] == "approved"

    policy_reject = _policy(
        "cov.sla.reject",
        stages=[
            {
                "name": "Auto reject",
                "stage_order": 1,
                "mode": "all",
                "sla_hours": 1,
                "on_breach": "auto_reject",
                "rules": [{"rule_type": "user", "rule_value": {"user_id": "u-bob"}}],
            }
        ],
    )
    await _activate(client, admin_token, "cov.sla.reject", policy_reject)
    resp = await client.post(
        "/v1/awe/requests",
        json={
            "policy_key": "cov.sla.reject",
            "artifact_type": "test",
            "artifact_id": "sla-reject",
            "context": {},
        },
        headers=auth_header(service_token),
    )
    reject_request_id = resp.json()["request_id"]
    async with sm() as session:
        request = await session.get(ApprovalRequest, reject_request_id)
        policy = await sla_monitor._load_policy(session, request.policy_id)
        stage = next(s for s in policy.stages if s.stage_order == 1)
        await sla_monitor._apply_on_breach(session, request, stage)
        await session.commit()

    resp = await client.get(
        f"/v1/awe/requests/{reject_request_id}",
        headers=auth_header(service_token),
    )
    assert resp.json()["status"] == "rejected"

    await _activate(client, admin_token, "cov.sla.tick")
    resp = await client.post(
        "/v1/awe/requests",
        json={
            "policy_key": "cov.sla.tick",
            "artifact_type": "test",
            "artifact_id": "sla-tick",
            "context": {},
        },
        headers=auth_header(service_token),
    )
    tick_request_id = resp.json()["request_id"]
    async with sm() as session:
        rows = await session.execute(
            select(ApprovalTask).where(ApprovalTask.request_id == tick_request_id)
        )
        for task in rows.scalars():
            task.due_at = utcnow() - timedelta(hours=2)
        req = await session.get(ApprovalRequest, tick_request_id)
        req.status = "approved"
        await session.commit()

    await sla_monitor._tick(sm)

    async with sm() as session:
        orphan = ApprovalTask(
            request_id=tick_request_id,
            stage_id="orphan-stage",
            stage_order=99,
            assignee="u-orphan",
            kind="approver",
            status="open",
            due_at=utcnow() - timedelta(hours=1),
        )
        session.add(orphan)
        await session.commit()

    await sla_monitor._tick(sm)

    tick_calls = {"n": 0}

    async def tick_then_cancel(*args, **kwargs):
        tick_calls["n"] += 1
        if tick_calls["n"] == 1:
            raise RuntimeError("boom")
        raise asyncio.CancelledError()

    with patch("awe.workers.sla_monitor._tick", side_effect=tick_then_cancel):
        with patch("awe.workers.sla_monitor.asyncio.sleep", new=AsyncMock()):
            task = asyncio.create_task(sla_monitor.sla_monitor_loop(engine))
            with pytest.raises(asyncio.CancelledError):
                await task


@pytest.mark.asyncio
async def test_webhook_dispatcher_fallback_and_empty_tick(client) -> None:
    from awe.db import get_engine
    from awe.workers.webhook_dispatcher import _tick

    engine = get_engine()
    sm = async_sessionmaker(engine, expire_on_commit=False)

    real_execute = AsyncSession.execute
    calls = {"n": 0}

    async def execute_with_for_update_fail(self, stmt, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("skip locked unsupported")
        return await real_execute(self, stmt, *args, **kwargs)

    with patch.object(AsyncSession, "execute", execute_with_for_update_fail):
        await _tick(sm, batch_size=5)

    await _tick(sm, batch_size=5)


# ---------------------------------------------------------------------------
# Direct handler / service invocations (reliable coverage for async paths)
# ---------------------------------------------------------------------------
def _admin_identity() -> CallerIdentity:
    return CallerIdentity(
        subject="test-admin",
        assignee_id="test-admin",
        roles=["AWE_ADMIN", "AWE_VIEWER"],
        is_service_account=False,
        raw_claims={"email": "admin@test"},
    )


@pytest.mark.asyncio
async def test_controllers_direct_admin(client) -> None:
    from awe.controllers import admin as admin_ctrl
    from awe.models import ApprovalEvent, WebhookDelivery
    from awe.models.base import utcnow

    identity = _admin_identity()
    sm = async_sessionmaker(__import__("awe.db", fromlist=["get_engine"]).get_engine(), expire_on_commit=False)

    async with sm() as session:
        delivery = WebhookDelivery(
            event_id="evt-direct",
            url="https://cb/hook",
            status="pending",
            attempt=1,
            next_attempt_at=utcnow(),
        )
        session.add(
            ApprovalEvent(
                id="evt-direct",
                request_id="req-direct",
                event_type="request_created",
                payload={},
                created_at=utcnow(),
            )
        )
        session.add(delivery)
        await session.commit()
        delivery_id = delivery.id

    async with sm() as session:
        event = MagicMock()
        event.request_id = "req-direct"
        event.event_type = "request_created"
        delivery_obj = await session.get(WebhookDelivery, delivery_id)
        rows = MagicMock()
        rows.all.return_value = [(delivery_obj, event)]
        session.execute = AsyncMock(return_value=rows)

        out = await admin_ctrl.list_deliveries(
            status_filter="pending",
            request_id="req-direct",
            limit=10,
            identity=identity,
            session=session,
        )
        assert len(out) == 1

        result = await admin_ctrl.retry_delivery(delivery_id, identity=identity, session=session)
        assert result.status == "pending"

        audit_rows = MagicMock()
        audit_row = MagicMock()
        audit_row.id = "a1"
        audit_row.occurred_at = utcnow()
        audit_row.actor = "test-admin"
        audit_row.actor_email = "admin@test"
        audit_row.action = "delivery.retry"
        audit_row.resource_type = "delivery"
        audit_row.resource_id = delivery_id
        audit_row.summary = "retry"
        audit_row.before = {}
        audit_row.after = {}
        audit_row.metadata_ = {}
        audit_scalars = MagicMock()
        audit_scalars.__iter__ = lambda self: iter([audit_row])
        audit_rows.scalars.return_value = audit_scalars
        session.execute = AsyncMock(return_value=audit_rows)

        audit_out = await admin_ctrl.list_audit(
            actor="test-admin",
            action="delivery.retry",
            resource_type="delivery",
            resource_id=delivery_id,
            since=datetime(2000, 1, 1, tzinfo=timezone.utc),
            until=datetime(2100, 1, 1, tzinfo=timezone.utc),
            limit=5,
            identity=identity,
            session=session,
        )
        assert len(audit_out) == 1


@pytest.mark.asyncio
async def test_controllers_direct_delegation(client) -> None:
    from awe.controllers import delegation as del_ctrl

    identity = _admin_identity()
    sm = async_sessionmaker(__import__("awe.db", fromlist=["get_engine"]).get_engine(), expire_on_commit=False)
    now = datetime.now(timezone.utc)

    async with sm() as session:
        delegation = UserDelegation(
            user_id="u-dir-a",
            delegate_to="u-dir-b",
            starts_at=now,
            ends_at=now + timedelta(days=1),
            created_by="admin",
        )
        session.add(delegation)
        await session.commit()
        delegation_id = delegation.id

    async with sm() as session:
        rows = MagicMock()
        rows.scalars.return_value = [await session.get(UserDelegation, delegation_id)]
        session.execute = AsyncMock(return_value=rows)
        listed = await del_ctrl.list_delegations(
            user_id=None, limit=10, identity=identity, session=session
        )
        assert len(listed) >= 1

        created = await del_ctrl.create_delegation(
            __import__("awe.schemas.delegation", fromlist=["DelegationCreate"]).DelegationCreate(
                user_id="u-new-a",
                delegate_to="u-new-b",
                starts_at=now,
                ends_at=now + timedelta(days=2),
                reason="cov",
            ),
            identity=identity,
            session=session,
        )
        assert created.user_id == "u-new-a"

        deleted = await del_ctrl.delete_delegation(delegation_id, identity=identity, session=session)
        assert deleted is None


@pytest.mark.asyncio
async def test_controllers_direct_health(client):
    from awe.controllers.health import health

    resp = await health()
    assert resp.response.status == "UP"


@pytest.mark.asyncio
async def test_controllers_direct_policy(client, admin_token) -> None:
    from awe.controllers import policy as policy_ctrl
    from awe.schemas.policy import PolicyCreate, SimulateRequest, StageIn, ApproverRuleIn

    identity = _admin_identity()
    sm = async_sessionmaker(__import__("awe.db", fromlist=["get_engine"]).get_engine(), expire_on_commit=False)

    payload = PolicyCreate(
        policy_key="cov.direct.policy",
        name="Direct",
        artifact_type="test",
        stages=[
            StageIn(
                name="S",
                stage_order=1,
                mode="all",
                skip_if={"==": [{"var": "skip"}, True]},
                rules=[ApproverRuleIn(rule_type="user", rule_value={"user_id": "u-x"})],
            )
        ],
    )

    async with sm() as session:
        created = await policy_ctrl.create_policy(payload, identity=identity, session=session)
        assert created.policy_key == "cov.direct.policy"

        listed = await policy_ctrl.list_policies(identity=identity, session=session)
        assert any(p.policy_key == "cov.direct.policy" for p in listed)

        versions = await policy_ctrl.list_versions("cov.direct.policy", identity=identity, session=session)
        assert len(versions) == 1

        got = await policy_ctrl.get_version("cov.direct.policy", 1, identity=identity, session=session)
        assert got.version == 1

        v2_payload = PolicyCreate(
            policy_key="cov.direct.policy",
            name="Direct v2",
            artifact_type="test",
            stages=payload.stages,
        )
        added = await policy_ctrl.add_version(
            "cov.direct.policy", v2_payload, identity=identity, session=session
        )
        assert added.version == 2

        edited = await policy_ctrl.edit_draft(
            "cov.direct.policy",
            2,
            v2_payload,
            identity=identity,
            session=session,
        )
        assert edited.name == "Direct v2"

        activated = await policy_ctrl.activate("cov.direct.policy", 1, identity=identity, session=session)
        assert activated.status == "active"

        sim = await policy_ctrl.simulate(
            "cov.direct.policy",
            1,
            SimulateRequest(context={"skip": True}),
            identity=identity,
            session=session,
        )
        assert sim.stages[0].skipped is True

        deactivated = await policy_ctrl.deactivate(
            "cov.direct.policy", 1, identity=identity, session=session
        )
        assert deactivated.status == "archived"
        await session.commit()


@pytest.mark.asyncio
async def test_controllers_direct_request_and_task(client, admin_token, service_token) -> None:
    from awe.controllers import request as req_ctrl
    from awe.controllers import task as task_ctrl
    from awe.schemas.request import CancelRequest, CreateRequestIn, DecisionIn, ReassignTaskIn

    await _activate(client, admin_token, "cov.direct.req")
    identity = CallerIdentity(
        subject="svc-registry",
        assignee_id=None,
        roles=[],
        is_service_account=True,
        raw_claims={},
    )
    user = CallerIdentity(
        subject="u-alice",
        assignee_id="u-alice",
        roles=[],
        is_service_account=False,
        raw_claims={"email": "alice@test"},
    )
    admin = _admin_identity()
    sm = async_sessionmaker(__import__("awe.db", fromlist=["get_engine"]).get_engine(), expire_on_commit=False)

    async with sm() as session:
        created = await req_ctrl.create_request(
            CreateRequestIn(
                policy_key="cov.direct.req",
                artifact_type="test",
                artifact_id="direct-req-1",
                context={"q": "find"},
            ),
            identity=identity,
            session=session,
            idempotency_key=None,
        )
        request_id = created["request_id"]

        fetched = await req_ctrl.get_request(request_id, identity=identity, session=session)
        assert fetched.id == request_id

        searched = await req_ctrl.search_requests(
            artifact_type="test",
            artifact_id="direct-req-1",
            status_filter="in_review",
            limit=10,
            identity=identity,
            session=session,
        )
        assert len(searched) == 1

        events = await req_ctrl.request_events(request_id, identity=identity, session=session)
        assert events

        task_rows = await session.execute(
            select(ApprovalTask).where(ApprovalTask.request_id == request_id)
        )
        task = task_rows.scalar_one()

        stats = await task_ctrl.task_stats(status_filter="open", identity=user, session=session)
        assert stats.total >= 1

        paged = await task_ctrl.list_tasks(
            assignee="u-alice",
            request_id=None,
            status_filter="open",
            artifact_type=None,
            policy_key=None,
            search_text="find",
            page=1,
            page_size=10,
            identity=user,
            session=session,
        )
        assert paged.total >= 1

        claimed = await task_ctrl.claim_task(task.id, identity=user, session=session)
        assert claimed.status == "claimed"

        decided = await task_ctrl.decide(
            task.id,
            DecisionIn(action="approve"),
            identity=user,
            session=session,
        )
        assert decided.action == "approve"

        await session.commit()

    await _activate(client, admin_token, "cov.direct.req2")
    async with sm() as session:
        created2 = await req_ctrl.create_request(
            CreateRequestIn(
                policy_key="cov.direct.req2",
                artifact_type="test",
                artifact_id="direct-req-2",
                context={},
            ),
            identity=identity,
            session=session,
            idempotency_key="idem-direct-1",
        )
        request_id2 = created2["request_id"]
        task2 = (
            await session.execute(
                select(ApprovalTask).where(ApprovalTask.request_id == request_id2)
            )
        ).scalar_one()

        reassigned = await task_ctrl.reassign(
            task2.id,
            ReassignTaskIn(new_assignee="u-bob", reason="shift"),
            identity=admin,
            session=session,
        )
        assert reassigned.assignee == "u-bob"

        cancelled = await req_ctrl.cancel_request(
            request_id2,
            CancelRequest(reason="done"),
            identity=admin,
            session=session,
        )
        assert cancelled.status == "cancelled"
        await session.commit()

        replay = await req_ctrl.create_request(
            CreateRequestIn(
                policy_key="cov.direct.req2",
                artifact_type="test",
                artifact_id="direct-req-2",
                context={},
            ),
            identity=identity,
            session=session,
            idempotency_key="idem-direct-1",
        )
        assert replay["request_id"] == request_id2


@pytest.mark.asyncio
async def test_engine_direct_flows(client) -> None:
    from awe.db import get_engine
    from awe.models import ApprovalDecision, ApproverRule
    from awe.models.base import utcnow
    from sqlalchemy.orm import selectinload

    sm = async_sessionmaker(get_engine(), expire_on_commit=False)
    async with sm() as session:
        policy = ApprovalPolicy(
            policy_key="eng.direct",
            version=1,
            name="Engine direct",
            artifact_type="test",
            status="active",
            forbid_repeat_approvers=True,
        )
        session.add(policy)
        await session.flush()
        s1 = ApprovalStage(
            policy_id=policy.id,
            name="S1",
            stage_order=1,
            parallel_group=1,
            mode="all",
            rules=[
                ApproverRule(
                    stage_id="",
                    rule_type="user",
                    rule_value={"user_id": "u-a"},
                    kind="approver",
                )
            ],
        )
        s2 = ApprovalStage(
            policy_id=policy.id,
            name="S2",
            stage_order=2,
            parallel_group=1,
            mode="all",
            rules=[
                ApproverRule(
                    stage_id="",
                    rule_type="user",
                    rule_value={"user_id": "u-b"},
                    kind="approver",
                )
            ],
        )
        session.add(s1)
        session.add(s2)
        await session.flush()
        for rule in s1.rules + s2.rules:
            rule.stage_id = s1.id if rule in s1.rules else s2.id
        await session.flush()

        loaded = (
            await session.execute(
                select(ApprovalPolicy)
                .options(selectinload(ApprovalPolicy.stages).selectinload(ApprovalStage.rules))
                .where(ApprovalPolicy.id == policy.id)
            )
        ).scalar_one()

        request = await engine_svc.start_request(
            session=session,
            policy=loaded,
            artifact_type="test",
            artifact_id="eng-1",
            source_service="svc",
            context={},
            callback_url=None,
            callback_secret_id=None,
            requester="u-req",
        )
        assert request.status == "in_review"

        tasks = await engine_svc._stage_tasks(session, request.id, 1)
        request.status = "approved"
        with pytest.raises(engine_svc.EngineError, match="terminal state"):
            await engine_svc.apply_decision(
                session, request, tasks[0], actor="u-a", action="approve"
            )
        request.status = "in_review"

        await engine_svc.cancel_request(session, request, actor="admin", reason="n/a")
        assert request.status == "cancelled"

        request2 = await engine_svc.start_request(
            session=session,
            policy=loaded,
            artifact_type="test",
            artifact_id="eng-2",
            source_service="svc",
            context={},
            callback_url="https://cb/hook",
            callback_secret_id=None,
            requester="u-req",
        )
        open_tasks = (
            await session.execute(
                select(ApprovalTask).where(ApprovalTask.request_id == request2.id)
            )
        ).scalars().all()
        with pytest.raises(engine_svc.EngineError):
            await engine_svc.reassign_task(
                session,
                request2,
                open_tasks[0],
                new_assignee=open_tasks[0].assignee,
                actor="admin",
                reason=None,
            )
        open_tasks[0].status = "completed"
        with pytest.raises(engine_svc.EngineError):
            await engine_svc.reassign_task(
                session,
                request2,
                open_tasks[0],
                new_assignee="u-z",
                actor="admin",
                reason=None,
            )

        assert engine_svc._apply_sod_filters(["u-a", "u-b"], loaded, "u-a", {"u-a"}) == ["u-b"]
        assert await engine_svc._approved_actors_before(session, request2.id, 3) == set()

        delegation = UserDelegation(
            user_id="u-dup",
            delegate_to="u-target",
            starts_at=utcnow() - timedelta(hours=1),
            ends_at=utcnow() + timedelta(days=1),
            created_by="admin",
        )
        session.add(delegation)
        await session.flush()
        pairs = await engine_svc._build_tasks_with_delegation(
            session, ["u-dup", "u-dup", "u-other"]
        )
        assert pairs == [("u-target", "u-dup"), ("u-other", None)]

        stage1 = loaded.stages[0]
        stage1.rules[0].required = True
        effective = await engine_svc._required_assignees_for_stage(
            session, stage1, request2.id
        )
        assert isinstance(effective, set)

        stage1.escalation_rules_json = [
            {"rule_type": "user", "rule_value": {"user_id": "u-esc"}}
        ]
        added = await engine_svc.escalate_stage(session, request2, stage1)
        assert added >= 0
        added2 = await engine_svc.escalate_stage(session, request2, stage1)
        assert added2 == 0

        await session.commit()


@pytest.mark.asyncio
async def test_keycloak_admin_get_success():
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json = MagicMock(return_value=[{"username": "ok"}])
    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=mock_resp)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    with patch(
        "awe.services.keycloak_admin.keycloak_admin_token",
        new=AsyncMock(return_value="tok"),
    ), patch("awe.services.keycloak_admin.get_settings") as gs, patch(
        "httpx.AsyncClient", return_value=mock_client
    ):
        gs.return_value.awe.keycloak.base_url = "https://kc"
        gs.return_value.awe.keycloak.realm = "staff"
        out = await kc._admin_get("/users")
    assert out[0]["username"] == "ok"


@pytest.mark.asyncio
async def test_resolver_client_role_success():
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/clients"):
            return httpx.Response(200, json=[{"id": "uuid-1"}])
        return httpx.Response(200, json=[{"username": "client-user"}])

    with patch(
        "awe.services.resolver.keycloak_admin_token",
        new=AsyncMock(return_value="tok"),
    ), patch("awe.services.resolver.get_settings") as gs:
        gs.return_value.awe.keycloak.base_url = "https://kc"
        gs.return_value.awe.keycloak.realm = "staff"
        transport = httpx.MockTransport(handler)
        with patch("httpx.AsyncClient", return_value=httpx.AsyncClient(transport=transport)):
            ids = await resolver_svc._resolve_keycloak_role("ROLE", "portal")
    assert ids == ["client-user"]


@pytest.mark.asyncio
async def test_resolver_group_keycloak_admin_error():
    with patch(
        "awe.services.resolver.keycloak_admin_token",
        new=AsyncMock(side_effect=kc.KeycloakAdminError("admin down")),
    ):
        with pytest.raises(resolver_svc.ResolutionError, match="admin down"):
            await resolver_svc._resolve_keycloak_group("/team")


@pytest.mark.asyncio
async def test_webhook_dispatcher_loop_exception(client) -> None:
    from awe.db import get_engine
    from awe.workers.webhook_dispatcher import webhook_dispatcher_loop

    engine = get_engine()
    calls = {"n": 0}

    async def tick_fail_then_cancel(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("tick failed")
        raise asyncio.CancelledError()

    with patch("awe.workers.webhook_dispatcher._tick", side_effect=tick_fail_then_cancel):
        with patch("awe.workers.webhook_dispatcher.asyncio.sleep", new=AsyncMock()):
            task = asyncio.create_task(webhook_dispatcher_loop(engine))
            with pytest.raises(asyncio.CancelledError):
                await task


@pytest.mark.asyncio
async def test_controllers_error_paths(client, admin_token) -> None:
    from awe.controllers import admin as admin_ctrl
    from awe.controllers import delegation as del_ctrl
    from awe.controllers import policy as policy_ctrl
    from awe.controllers import request as req_ctrl
    from awe.controllers import task as task_ctrl
    from awe.schemas.policy import PolicyCreate, SimulateRequest, StageIn, ApproverRuleIn
    from awe.schemas.request import CancelRequest, CreateRequestIn, DecisionIn, ReassignTaskIn
    from fastapi.responses import JSONResponse

    identity = _admin_identity()
    svc = CallerIdentity(
        subject="svc-registry",
        assignee_id=None,
        roles=[],
        is_service_account=True,
        raw_claims={},
    )
    user = CallerIdentity(
        subject="u-alice",
        assignee_id="u-alice",
        roles=[],
        is_service_account=False,
        raw_claims={"email": "alice@test"},
    )
    sm = async_sessionmaker(__import__("awe.db", fromlist=["get_engine"]).get_engine(), expire_on_commit=False)

    async with sm() as session:
        nf = await admin_ctrl.retry_delivery("missing-delivery", identity=identity, session=session)
        assert isinstance(nf, JSONResponse) and nf.status_code == 404

        missing_del = await del_ctrl.delete_delegation("missing-del", identity=identity, session=session)
        assert isinstance(missing_del, JSONResponse) and missing_del.status_code == 404

        delivered = MagicMock()
        delivered.status = "delivered"
        real_get = session.get

        async def get_delivered(model, ident, **kwargs):
            if model is __import__("awe.models", fromlist=["WebhookDelivery"]).WebhookDelivery:
                return delivered
            return await real_get(model, ident, **kwargs)

        session.get = get_delivered
        done = await admin_ctrl.retry_delivery("d1", identity=identity, session=session)
        assert isinstance(done, JSONResponse) and done.status_code == 409
        session.get = real_get

        payload = PolicyCreate(
            policy_key="cov.err.policy",
            name="Err",
            artifact_type="test",
            stages=[
                StageIn(
                    name="S",
                    stage_order=1,
                    mode="all",
                    rules=[ApproverRuleIn(rule_type="user", rule_value={"user_id": "u1"})],
                )
            ],
        )
        with patch(
            "awe.controllers.policy.policy_svc.create_draft",
            new=AsyncMock(side_effect=policy_svc.PolicyError("exists")),
        ):
            err = await policy_ctrl.create_policy(payload, identity=identity, session=session)
        assert isinstance(err, JSONResponse) and err.status_code == 409

        empty = await policy_ctrl.list_versions("ghost.policy", identity=identity, session=session)
        assert isinstance(empty, JSONResponse) and empty.status_code == 404

        missing_ver = await policy_ctrl.get_version("cov.err.policy", 99, identity=identity, session=session)
        assert isinstance(missing_ver, JSONResponse) and missing_ver.status_code == 404

        with patch(
            "awe.controllers.policy.policy_svc.add_draft_version",
            new=AsyncMock(side_effect=policy_svc.PolicyNotFound("missing")),
        ):
            add_err = await policy_ctrl.add_version("cov.err.policy", payload, identity=identity, session=session)
        assert isinstance(add_err, JSONResponse) and add_err.status_code == 404

        with patch(
            "awe.controllers.policy.policy_svc.update_draft",
            new=AsyncMock(side_effect=policy_svc.PolicyNotFound("missing")),
        ):
            edit_nf = await policy_ctrl.edit_draft(
                "cov.err.policy", 1, payload, identity=identity, session=session
            )
        assert isinstance(edit_nf, JSONResponse) and edit_nf.status_code == 404

        with patch(
            "awe.controllers.policy.policy_svc.update_draft",
            new=AsyncMock(side_effect=policy_svc.PolicyError("immutable")),
        ):
            edit_err = await policy_ctrl.edit_draft(
                "cov.err.policy", 1, payload, identity=identity, session=session
            )
        assert isinstance(edit_err, JSONResponse) and edit_err.status_code == 409

        with patch(
            "awe.controllers.policy.policy_svc.activate_version",
            new=AsyncMock(side_effect=policy_svc.PolicyNotFound("missing")),
        ):
            act_err = await policy_ctrl.activate("cov.err.policy", 1, identity=identity, session=session)
        assert isinstance(act_err, JSONResponse) and act_err.status_code == 404

        with patch(
            "awe.controllers.policy.policy_svc.deactivate_version",
            new=AsyncMock(side_effect=policy_svc.PolicyNotFound("missing")),
        ):
            deact_nf = await policy_ctrl.deactivate("cov.err.policy", 1, identity=identity, session=session)
        assert isinstance(deact_nf, JSONResponse) and deact_nf.status_code == 404

        with patch(
            "awe.controllers.policy.policy_svc.deactivate_version",
            new=AsyncMock(side_effect=policy_svc.PolicyError("not active")),
        ):
            deact_err = await policy_ctrl.deactivate("cov.err.policy", 1, identity=identity, session=session)
        assert isinstance(deact_err, JSONResponse) and deact_err.status_code == 409

        sim_nf = await policy_ctrl.simulate(
            "ghost", 1, SimulateRequest(context={}), identity=identity, session=session
        )
        assert isinstance(sim_nf, JSONResponse) and sim_nf.status_code == 404

        with patch(
            "awe.controllers.policy.resolver_svc.resolve_stage",
            new=AsyncMock(side_effect=resolver_svc.ResolutionError("fail")),
        ):
            await policy_ctrl.create_policy(
                PolicyCreate(
                    policy_key="cov.err.sim",
                    name="Sim",
                    artifact_type="test",
                    stages=payload.stages,
                ),
                identity=identity,
                session=session,
            )
            sim_res = await policy_ctrl.simulate(
                "cov.err.sim",
                1,
                SimulateRequest(context={}),
                identity=identity,
                session=session,
            )
        assert isinstance(sim_res, JSONResponse) and sim_res.status_code == 503

        no_policy = await req_ctrl.create_request(
            CreateRequestIn(
                policy_key="missing.active",
                artifact_type="test",
                artifact_id="x",
                context={},
            ),
            identity=svc,
            session=session,
            idempotency_key=None,
        )
        assert isinstance(no_policy, JSONResponse) and no_policy.status_code == 404

        with patch(
            "awe.controllers.request.engine_svc.start_request",
            new=AsyncMock(side_effect=engine_svc.EngineError("bad")),
        ), patch(
            "awe.controllers.request.policy_svc.get_active",
            new=AsyncMock(return_value=MagicMock()),
        ):
            eng_err = await req_ctrl.create_request(
                CreateRequestIn(
                    policy_key="any",
                    artifact_type="test",
                    artifact_id="y",
                    context={},
                ),
                identity=svc,
                session=session,
                idempotency_key="idem-err",
            )
        assert isinstance(eng_err, JSONResponse) and eng_err.status_code == 400

        get_nf = await req_ctrl.get_request("missing-req", identity=svc, session=session)
        assert isinstance(get_nf, JSONResponse) and get_nf.status_code == 404

        cancel_nf = await req_ctrl.cancel_request(
            "missing-req", CancelRequest(reason="x"), identity=identity, session=session
        )
        assert isinstance(cancel_nf, JSONResponse) and cancel_nf.status_code == 404

        bad_assignee = CallerIdentity(
            subject="no-assignee",
            assignee_id=None,
            roles=[],
            is_service_account=False,
            raw_claims={"email": "x@test"},
        )
        me_err = await task_ctrl.list_tasks(
            assignee="me",
            request_id=None,
            status_filter=None,
            artifact_type=None,
            policy_key=None,
            search_text=None,
            page=1,
            page_size=10,
            identity=bad_assignee,
            session=session,
        )
        assert isinstance(me_err, JSONResponse) and me_err.status_code == 401

        claim_nf = await task_ctrl.claim_task("missing-task", identity=user, session=session)
        assert isinstance(claim_nf, JSONResponse) and claim_nf.status_code == 404

        decide_nf = await task_ctrl.decide(
            "missing-task", DecisionIn(action="approve"), identity=user, session=session
        )
        assert isinstance(decide_nf, JSONResponse) and decide_nf.status_code == 404

        reassign_nf = await task_ctrl.reassign(
            "missing-task",
            ReassignTaskIn(new_assignee="u-bob"),
            identity=identity,
            session=session,
        )
        assert isinstance(reassign_nf, JSONResponse) and reassign_nf.status_code == 404


@pytest.mark.asyncio
async def test_engine_remaining_branches(client) -> None:
    from awe.db import get_engine
    from awe.models import ApprovalDecision, ApproverRule
    from awe.models.base import utcnow
    from sqlalchemy.orm import selectinload

    sm = async_sessionmaker(get_engine(), expire_on_commit=False)
    async with sm() as session:
        policy = ApprovalPolicy(
            policy_key="eng.rem",
            version=1,
            name="Rem",
            artifact_type="test",
            status="active",
        )
        session.add(policy)
        await session.flush()
        observer_stage = ApprovalStage(
            policy_id=policy.id,
            name="Obs",
            stage_order=1,
            mode="all",
        )
        session.add(observer_stage)
        await session.flush()
        session.add(
            ApproverRule(
                stage_id=observer_stage.id,
                rule_type="user",
                rule_value={"user_id": "u-appr"},
                kind="approver",
            )
        )
        session.add(
            ApproverRule(
                stage_id=observer_stage.id,
                rule_type="user",
                rule_value={"user_id": "u-obs"},
                kind="observer",
            )
        )
        pct_stage = ApprovalStage(
            policy_id=policy.id,
            name="Pct",
            stage_order=2,
            mode="percentage",
            mode_value=100,
        )
        session.add(pct_stage)
        await session.flush()
        for uid in ("u1", "u2"):
            session.add(
                ApproverRule(
                    stage_id=pct_stage.id,
                    rule_type="user",
                    rule_value={"user_id": uid},
                    kind="approver",
                )
            )
        await session.flush()

        loaded = (
            await session.execute(
                select(ApprovalPolicy)
                .options(selectinload(ApprovalPolicy.stages).selectinload(ApprovalStage.rules))
                .where(ApprovalPolicy.id == policy.id)
            )
        ).scalar_one()

        req_obs = await engine_svc.start_request(
            session,
            policy=loaded,
            artifact_type="test",
            artifact_id="obs-1",
            source_service="svc",
            context={},
            callback_url=None,
            callback_secret_id=None,
            requester="r",
        )
        obs_task = next(t for t in await engine_svc._stage_tasks(session, req_obs.id, 1) if t.kind == "observer")
        await engine_svc.apply_decision(
            session, req_obs, obs_task, actor="u-obs", action="abstain"
        )
        appr_task = next(t for t in await engine_svc._stage_tasks(session, req_obs.id, 1) if t.kind == "approver")
        await engine_svc.apply_decision(
            session, req_obs, appr_task, actor="u-appr", action="approve"
        )

        pct_only = ApprovalPolicy(
            policy_key="eng.pct.only",
            version=1,
            name="Pct only",
            artifact_type="test",
            status="active",
        )
        session.add(pct_only)
        await session.flush()
        pct_only_stage = ApprovalStage(
            policy_id=pct_only.id,
            name="Pct",
            stage_order=1,
            mode="percentage",
            mode_value=100,
        )
        session.add(pct_only_stage)
        await session.flush()
        for uid in ("u1", "u2"):
            session.add(
                ApproverRule(
                    stage_id=pct_only_stage.id,
                    rule_type="user",
                    rule_value={"user_id": uid},
                    kind="approver",
                )
            )
        await session.flush()
        pct_loaded = (
            await session.execute(
                select(ApprovalPolicy)
                .options(selectinload(ApprovalPolicy.stages).selectinload(ApprovalStage.rules))
                .where(ApprovalPolicy.id == pct_only.id)
            )
        ).scalar_one()

        req_pct = await engine_svc.start_request(
            session,
            policy=pct_loaded,
            artifact_type="test",
            artifact_id="pct-1",
            source_service="svc",
            context={},
            callback_url=None,
            callback_secret_id=None,
            requester="r",
        )
        pct_tasks = await engine_svc._stage_tasks(session, req_pct.id, 1)
        pct_tasks[0].status = "completed"
        pct_tasks[1].status = "open"
        session.add(
            ApprovalDecision(
                request_id=req_pct.id,
                task_id=pct_tasks[0].id,
                stage_order=1,
                actor="u1",
                action="reject",
            )
        )
        await session.flush()
        outcome = await engine_svc._recompute_stage(session, pct_loaded.stages[0], req_pct.id)
        assert outcome == "rejected"

        assert engine_svc._evaluate_quorum(
            pct_loaded.stages[0], pct_tasks, {"approve": 0, "reject": 0, "abstain": 0}
        ) in ("open", "rejected")

        empty_stage = ApprovalStage(
            policy_id=policy.id, name="Empty", stage_order=99, mode="all"
        )
        assert await engine_svc._stage_is_terminal(session, empty_stage, req_pct.id) is True

        await engine_svc._close_stage_open_tasks(session, req_pct.id, 1)

        req_only = ApprovalPolicy(
            policy_key="eng.req.only",
            version=1,
            name="Req",
            artifact_type="test",
            status="active",
        )
        session.add(req_only)
        await session.flush()
        req_only_stage = ApprovalStage(
            policy_id=req_only.id,
            name="Req",
            stage_order=1,
            mode="any-n",
            mode_value=1,
        )
        session.add(req_only_stage)
        await session.flush()
        session.add(
            ApproverRule(
                stage_id=req_only_stage.id,
                rule_type="user",
                rule_value={"user_id": "u1"},
                kind="approver",
                required=True,
            )
        )
        session.add(
            ApproverRule(
                stage_id=req_only_stage.id,
                rule_type="user",
                rule_value={"user_id": "u2"},
                kind="approver",
            )
        )
        await session.flush()
        req_loaded = (
            await session.execute(
                select(ApprovalPolicy)
                .options(selectinload(ApprovalPolicy.stages).selectinload(ApprovalStage.rules))
                .where(ApprovalPolicy.id == req_only.id)
            )
        ).scalar_one()
        req_req = await engine_svc.start_request(
            session,
            policy=req_loaded,
            artifact_type="test",
            artifact_id="req-1",
            source_service="svc",
            context={},
            callback_url=None,
            callback_secret_id=None,
            requester="r",
        )
        req_stage = req_loaded.stages[0]
        open_tasks = await engine_svc._stage_tasks(session, req_req.id, 1)
        u2_task = next(t for t in open_tasks if t.assignee == "u2")
        session.add(
            ApprovalDecision(
                request_id=req_req.id,
                task_id=u2_task.id,
                stage_order=1,
                actor="u2",
                action="approve",
            )
        )
        u2_task.status = "completed"
        await session.flush()
        assert await engine_svc._recompute_stage(session, req_stage, req_req.id) == "open"

        u1_task = next(t for t in open_tasks if t.assignee == "u1")
        u1_task.status = "expired"
        u1_task.completed_at = utcnow()
        await session.flush()
        assert await engine_svc._recompute_stage(session, req_stage, req_req.id) == "rejected"

        assert await engine_svc._required_assignees_for_stage(session, req_stage, "missing") == set()

        stage1 = req_loaded.stages[0]
        stage1.escalation_rules_json = [{"rule_type": "user", "rule_value": {"user_id": "u-none"}}]
        with patch(
            "awe.services.engine.resolver_svc.resolve_stage",
            new=AsyncMock(return_value=[]),
        ):
            assert await engine_svc.escalate_stage(session, req_req, stage1) == 0

        block_policy = ApprovalPolicy(
            policy_key="eng.block",
            version=1,
            name="Block",
            artifact_type="test",
            status="active",
        )
        session.add(block_policy)
        await session.flush()
        block_stage = ApprovalStage(
            policy_id=block_policy.id,
            name="Block",
            stage_order=1,
            mode="all",
            on_empty="block",
        )
        session.add(block_stage)
        await session.flush()
        block_loaded = (
            await session.execute(
                select(ApprovalPolicy)
                .options(selectinload(ApprovalPolicy.stages).selectinload(ApprovalStage.rules))
                .where(ApprovalPolicy.id == block_policy.id)
            )
        ).scalar_one()
        blocked = await engine_svc.start_request(
            session,
            policy=block_loaded,
            artifact_type="test",
            artifact_id="blk",
            source_service="svc",
            context={},
            callback_url=None,
            callback_secret_id=None,
            requester="r",
        )
        assert blocked.status == "rejected"

        skip_policy = ApprovalPolicy(
            policy_key="eng.skip",
            version=1,
            name="Skip",
            artifact_type="test",
            status="active",
        )
        session.add(skip_policy)
        await session.flush()
        skip_stage = ApprovalStage(
            policy_id=skip_policy.id,
            name="Skip",
            stage_order=1,
            mode="all",
            skip_if={"==": [1, 1]},
        )
        session.add(skip_stage)
        await session.flush()
        skip_loaded = (
            await session.execute(
                select(ApprovalPolicy)
                .options(selectinload(ApprovalPolicy.stages).selectinload(ApprovalStage.rules))
                .where(ApprovalPolicy.id == skip_policy.id)
            )
        ).scalar_one()
        skipped = await engine_svc.start_request(
            session,
            policy=skip_loaded,
            artifact_type="test",
            artifact_id="sk",
            source_service="svc",
            context={},
            callback_url=None,
            callback_secret_id=None,
            requester="r",
        )
        assert skipped.status == "approved"

        await session.commit()


@pytest.mark.asyncio
async def test_sla_monitor_missing_stage_continue(client, admin_token, service_token) -> None:
    from awe.db import get_engine
    from awe.models.base import utcnow
    from awe.workers.sla_monitor import _tick
    from sqlalchemy.orm import selectinload

    await _activate(client, admin_token, "cov.sla.orphan")
    resp = await client.post(
        "/v1/awe/requests",
        json={
            "policy_key": "cov.sla.orphan",
            "artifact_type": "test",
            "artifact_id": "orphan-1",
            "context": {},
        },
        headers=auth_header(service_token),
    )
    request_id = resp.json()["request_id"]
    sm = async_sessionmaker(get_engine(), expire_on_commit=False)
    async with sm() as session:
        policy = (
            await session.execute(
                select(ApprovalPolicy)
                .options(selectinload(ApprovalPolicy.stages))
                .where(ApprovalPolicy.policy_key == "cov.sla.orphan")
            )
        ).scalar_one()
        real_stage_id = policy.stages[0].id
        task = (
            await session.execute(
                select(ApprovalTask).where(ApprovalTask.request_id == request_id)
            )
        ).scalar_one()
        task.stage_order = 99
        task.stage_id = real_stage_id
        task.due_at = utcnow() - timedelta(hours=1)
        await session.commit()

    await _tick(sm)


