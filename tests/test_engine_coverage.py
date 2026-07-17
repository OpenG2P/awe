"""Integration tests targeting uncovered paths in `awe.services.engine`."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest
from jose import jwt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from .conftest import auth_header


def _user_token(sub: str) -> str:
    return jwt.encode(
        {
            "sub": sub,
            "preferred_username": sub,
            "realm_access": {"roles": []},
            "email": f"{sub}@test",
        },
        "secret",
        algorithm="HS256",
    )


async def _activate(client, admin_token, policy: dict) -> None:
    resp = await client.post(
        "/v1/awe/policies", json=policy, headers=auth_header(admin_token)
    )
    assert resp.status_code == 201, resp.text
    resp = await client.post(
        f"/v1/awe/policies/{policy['policy_key']}/versions/1/activate",
        headers=auth_header(admin_token),
    )
    assert resp.status_code == 200


async def _create_request(client, service_token, policy_key: str, **kwargs) -> dict:
    payload = {
        "policy_key": policy_key,
        "artifact_type": "test",
        "artifact_id": kwargs.pop("artifact_id", "art-1"),
        "context": kwargs.pop("context", {}),
        **kwargs,
    }
    resp = await client.post(
        "/v1/awe/requests",
        json=payload,
        headers=auth_header(service_token),
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _expire_tasks_for_assignees(
    request_id: str, assignees: set[str] | None = None
) -> None:
    from awe.db import get_engine
    from awe.models import ApprovalTask
    from awe.models.base import utcnow

    engine = get_engine()
    sm = async_sessionmaker(engine, expire_on_commit=False)
    async with sm() as session:
        rows = await session.execute(
            select(ApprovalTask).where(
                ApprovalTask.request_id == request_id,
                ApprovalTask.status.in_(("open", "claimed")),
            )
        )
        for task in rows.scalars():
            if assignees is None or task.assignee in assignees:
                task.due_at = utcnow() - timedelta(hours=2)
        await session.commit()


async def _seed_active_policy(policy_key: str, *, artifact_type: str = "test") -> None:
    """Insert an active zero-stage policy (API audit path breaks on empty stages)."""
    from awe.db import get_engine
    from awe.models import ApprovalPolicy

    engine = get_engine()
    sm = async_sessionmaker(engine, expire_on_commit=False)
    async with sm() as session:
        session.add(
            ApprovalPolicy(
                policy_key=policy_key,
                version=1,
                name="Zero stage seed",
                artifact_type=artifact_type,
                status="active",
            )
        )
        await session.commit()


async def _synthesize_stage_decision(
    request_id: str, stage_order: int, action: str, reason: str
) -> None:
    """Drive engine.synthesize_decision (used by SLA auto_approve/auto_reject)."""
    from awe.db import get_engine
    from awe.models import ApprovalPolicy, ApprovalRequest, ApprovalStage
    from awe.services import engine as engine_svc
    from sqlalchemy.orm import selectinload

    engine = get_engine()
    sm = async_sessionmaker(engine, expire_on_commit=False)
    async with sm() as session:
        request = await session.get(ApprovalRequest, request_id)
        assert request is not None
        row = await session.execute(
            select(ApprovalPolicy)
            .options(
                selectinload(ApprovalPolicy.stages).selectinload(
                    ApprovalStage.rules
                )
            )
            .where(ApprovalPolicy.id == request.policy_id)
        )
        policy = row.scalar_one()
        stage = next(s for s in policy.stages if s.stage_order == stage_order)
        await engine_svc.synthesize_decision(
            session,
            request,
            stage,
            action=action,
            actor="sla-monitor",
            reason=reason,
        )
        await session.commit()


async def _run_sla_tick() -> None:
    from awe.db import get_engine
    from awe.workers.sla_monitor import _tick

    engine = get_engine()
    sm = async_sessionmaker(engine, expire_on_commit=False)
    await _tick(sm)


@pytest.mark.asyncio
async def test_zero_stage_policy_auto_approves(
    client, admin_token, service_token
) -> None:
    await _seed_active_policy("engine.zero_stage")
    req = await _create_request(
        client, service_token, "engine.zero_stage", artifact_id="zero-1"
    )
    assert req["status"] == "approved"
    assert req["tasks"] == []

    resp = await client.get(
        f"/v1/awe/requests/{req['request_id']}/events",
        headers=auth_header(service_token),
    )
    types = [e["event_type"] for e in resp.json()]
    assert types == ["request_created", "request_approved"]


@pytest.mark.asyncio
async def test_on_empty_skip_auto_approves_when_no_approvers(
    client, admin_token, service_token
) -> None:
    policy = {
        "policy_key": "engine.on_empty_skip",
        "name": "Empty skip",
        "artifact_type": "test",
        "forbid_self_approval": True,
        "stages": [
            {
                "name": "Solo",
                "stage_order": 1,
                "mode": "all",
                "on_empty": "skip",
                "rules": [
                    {"rule_type": "user", "rule_value": {"user_id": "u-alice"}}
                ],
            }
        ],
    }
    await _activate(client, admin_token, policy)
    req = await _create_request(
        client,
        service_token,
        "engine.on_empty_skip",
        artifact_id="skip-1",
        requester="u-alice",
    )
    assert req["status"] == "approved"
    assert req["tasks"] == []

    resp = await client.get(
        f"/v1/awe/requests/{req['request_id']}/events",
        headers=auth_header(service_token),
    )
    assert any(e["event_type"] == "stage_skipped" for e in resp.json())


@pytest.mark.asyncio
async def test_on_empty_block_rejects_request(
    client, admin_token, service_token
) -> None:
    policy = {
        "policy_key": "engine.on_empty_block",
        "name": "Empty block",
        "artifact_type": "test",
        "forbid_self_approval": True,
        "stages": [
            {
                "name": "Solo",
                "stage_order": 1,
                "mode": "all",
                "on_empty": "block",
                "rules": [
                    {"rule_type": "user", "rule_value": {"user_id": "u-alice"}}
                ],
            }
        ],
    }
    await _activate(client, admin_token, policy)
    req = await _create_request(
        client,
        service_token,
        "engine.on_empty_block",
        artifact_id="block-1",
        requester="u-alice",
    )
    assert req["status"] == "rejected"
    assert req["tasks"] == []


@pytest.mark.asyncio
async def test_on_empty_block_via_empty_resolver(
    client, admin_token, service_token
) -> None:
    policy = {
        "policy_key": "engine.on_empty_resolver",
        "name": "Resolver empty block",
        "artifact_type": "test",
        "stages": [
            {
                "name": "Solo",
                "stage_order": 1,
                "mode": "all",
                "on_empty": "block",
                "rules": [
                    {"rule_type": "user", "rule_value": {"user_id": "u-alice"}}
                ],
            }
        ],
    }
    await _activate(client, admin_token, policy)
    with patch(
        "awe.services.engine.resolver_svc.resolve_stage",
        new=AsyncMock(return_value=[]),
    ):
        req = await _create_request(
            client,
            service_token,
            "engine.on_empty_resolver",
            artifact_id="block-res-1",
        )
    assert req["status"] == "rejected"


@pytest.mark.asyncio
async def test_skip_if_jsonlogic_skips_stage(
    client, admin_token, service_token
) -> None:
    policy = {
        "policy_key": "engine.skip_if",
        "name": "Skip if",
        "artifact_type": "test",
        "stages": [
            {
                "name": "Conditional",
                "stage_order": 1,
                "mode": "all",
                "skip_if": {"==": [{"var": "skip_me"}, True]},
                "rules": [
                    {"rule_type": "user", "rule_value": {"user_id": "u-alice"}}
                ],
            },
            {
                "name": "Final",
                "stage_order": 2,
                "mode": "all",
                "rules": [
                    {"rule_type": "user", "rule_value": {"user_id": "u-bob"}}
                ],
            },
        ],
    }
    await _activate(client, admin_token, policy)
    req = await _create_request(
        client,
        service_token,
        "engine.skip_if",
        artifact_id="skipif-1",
        context={"skip_me": True},
    )
    assert req["status"] == "in_review"
    assert {t["assignee"] for t in req["tasks"]} == {"u-bob"}

    resp = await client.get(
        f"/v1/awe/requests/{req['request_id']}/events",
        headers=auth_header(service_token),
    )
    skipped = [
        e for e in resp.json() if e["event_type"] == "stage_skipped"
    ]
    assert len(skipped) == 1
    assert skipped[0]["payload"]["reason"] == "skip_if"


@pytest.mark.asyncio
async def test_bad_skip_if_logs_and_stage_still_starts(
    client, admin_token, service_token, caplog
) -> None:
    policy = {
        "policy_key": "engine.bad_skip_if",
        "name": "Bad skip if",
        "artifact_type": "test",
        "stages": [
            {
                "name": "Broken logic",
                "stage_order": 1,
                "mode": "all",
                "skip_if": {"invalid_operator": [1, 2]},
                "rules": [
                    {"rule_type": "user", "rule_value": {"user_id": "u-alice"}}
                ],
            }
        ],
    }
    await _activate(client, admin_token, policy)
    with caplog.at_level("WARNING"):
        req = await _create_request(
            client,
            service_token,
            "engine.bad_skip_if",
            artifact_id="bad-skip-1",
        )
    assert req["status"] == "in_review"
    assert req["tasks"][0]["assignee"] == "u-alice"
    assert any("skip_if evaluation failed" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_all_stages_skipped_auto_approves(
    client, admin_token, service_token
) -> None:
    policy = {
        "policy_key": "engine.all_skipped",
        "name": "All skipped",
        "artifact_type": "test",
        "stages": [
            {
                "name": "Skip A",
                "stage_order": 1,
                "mode": "all",
                "skip_if": {"==": [1, 1]},
                "rules": [
                    {"rule_type": "user", "rule_value": {"user_id": "u-alice"}}
                ],
            },
            {
                "name": "Skip B",
                "stage_order": 2,
                "mode": "all",
                "skip_if": {"==": [1, 1]},
                "rules": [
                    {"rule_type": "user", "rule_value": {"user_id": "u-bob"}}
                ],
            },
        ],
    }
    await _activate(client, admin_token, policy)
    req = await _create_request(
        client,
        service_token,
        "engine.all_skipped",
        artifact_id="all-skip-1",
    )
    assert req["status"] == "approved"
    assert req["tasks"] == []

    resp = await client.get(
        f"/v1/awe/requests/{req['request_id']}/events",
        headers=auth_header(service_token),
    )
    approved = next(
        e for e in resp.json() if e["event_type"] == "request_approved"
    )
    assert approved["payload"].get("reason") == "all_stages_skipped"


@pytest.mark.asyncio
async def test_quorum_mode_approves_when_threshold_met(
    client, admin_token, service_token
) -> None:
    policy = {
        "policy_key": "engine.quorum_mode",
        "name": "Quorum mode",
        "artifact_type": "test",
        "stages": [
            {
                "name": "Quorum",
                "stage_order": 1,
                "mode": "quorum",
                "mode_value": 2,
                "rules": [
                    {"rule_type": "user", "rule_value": {"user_id": "u-alice"}},
                    {"rule_type": "user", "rule_value": {"user_id": "u-bob"}},
                    {"rule_type": "user", "rule_value": {"user_id": "u-charlie"}},
                ],
            }
        ],
    }
    await _activate(client, admin_token, policy)
    req = await _create_request(
        client, service_token, "engine.quorum_mode", artifact_id="quorum-1"
    )
    request_id = req["request_id"]
    tasks = {t["assignee"]: t["id"] for t in req["tasks"]}

    for user in ("u-alice", "u-bob"):
        resp = await client.post(
            f"/v1/awe/tasks/{tasks[user]}/decision",
            json={"action": "approve"},
            headers=auth_header(_user_token(user)),
        )
        assert resp.status_code == 201

    resp = await client.get(
        f"/v1/awe/requests/{request_id}", headers=auth_header(service_token)
    )
    assert resp.json()["status"] == "approved"


@pytest.mark.asyncio
async def test_percentage_mode_approves_at_threshold(
    client, admin_token, service_token
) -> None:
    policy = {
        "policy_key": "engine.percentage_mode",
        "name": "Percentage mode",
        "artifact_type": "test",
        "stages": [
            {
                "name": "Pct",
                "stage_order": 1,
                "mode": "percentage",
                "mode_value": 50,
                "rules": [
                    {"rule_type": "user", "rule_value": {"user_id": "u-alice"}},
                    {"rule_type": "user", "rule_value": {"user_id": "u-bob"}},
                ],
            }
        ],
    }
    await _activate(client, admin_token, policy)
    req = await _create_request(
        client, service_token, "engine.percentage_mode", artifact_id="pct-1"
    )
    alice_id = next(t["id"] for t in req["tasks"] if t["assignee"] == "u-alice")
    resp = await client.post(
        f"/v1/awe/tasks/{alice_id}/decision",
        json={"action": "approve"},
        headers=auth_header(_user_token("u-alice")),
    )
    assert resp.status_code == 201

    resp = await client.get(
        f"/v1/awe/requests/{req['request_id']}",
        headers=auth_header(service_token),
    )
    assert resp.json()["status"] == "approved"


@pytest.mark.asyncio
async def test_percentage_mode_rejects_when_impossible(
    client, admin_token, service_token
) -> None:
    policy = {
        "policy_key": "engine.percentage_fail",
        "name": "Percentage fail",
        "artifact_type": "test",
        "stages": [
            {
                "name": "Pct",
                "stage_order": 1,
                "mode": "percentage",
                "mode_value": 100,
                "rules": [
                    {"rule_type": "user", "rule_value": {"user_id": "u-alice"}},
                    {"rule_type": "user", "rule_value": {"user_id": "u-bob"}},
                    {"rule_type": "user", "rule_value": {"user_id": "u-charlie"}},
                ],
            }
        ],
    }
    await _activate(client, admin_token, policy)
    req = await _create_request(
        client, service_token, "engine.percentage_fail", artifact_id="pct-fail-1"
    )
    request_id = req["request_id"]
    tasks = {t["assignee"]: t["id"] for t in req["tasks"]}

    await client.post(
        f"/v1/awe/tasks/{tasks['u-alice']}/decision",
        json={"action": "approve"},
        headers=auth_header(_user_token("u-alice")),
    )
    resp = await client.post(
        f"/v1/awe/tasks/{tasks['u-bob']}/decision",
        json={"action": "abstain"},
        headers=auth_header(_user_token("u-bob")),
    )
    assert resp.status_code == 201

    resp = await client.get(
        f"/v1/awe/requests/{request_id}", headers=auth_header(service_token)
    )
    assert resp.json()["status"] == "rejected"


@pytest.mark.asyncio
async def test_observer_decision_completes_without_advancing(
    client, admin_token, service_token
) -> None:
    policy = {
        "policy_key": "engine.observer_decide",
        "name": "Observer decision",
        "artifact_type": "test",
        "stages": [
            {
                "name": "Stage",
                "stage_order": 1,
                "mode": "all",
                "rules": [
                    {"rule_type": "user", "rule_value": {"user_id": "u-alice"}},
                    {
                        "rule_type": "user",
                        "rule_value": {"user_id": "u-legal"},
                        "kind": "observer",
                    },
                ],
            }
        ],
    }
    await _activate(client, admin_token, policy)
    req = await _create_request(
        client, service_token, "engine.observer_decide", artifact_id="obs-dec-1"
    )
    request_id = req["request_id"]
    tasks = {t["assignee"]: t for t in req["tasks"]}

    resp = await client.post(
        f"/v1/awe/tasks/{tasks['u-legal']['id']}/decision",
        json={"action": "abstain", "comment": "noted"},
        headers=auth_header(_user_token("u-legal")),
    )
    assert resp.status_code == 201

    resp = await client.get(
        f"/v1/awe/requests/{request_id}", headers=auth_header(service_token)
    )
    assert resp.json()["status"] == "in_review"

    resp = await client.post(
        f"/v1/awe/tasks/{tasks['u-alice']['id']}/decision",
        json={"action": "approve"},
        headers=auth_header(_user_token("u-alice")),
    )
    assert resp.status_code == 201

    resp = await client.get(
        f"/v1/awe/requests/{request_id}", headers=auth_header(service_token)
    )
    assert resp.json()["status"] == "approved"


@pytest.mark.asyncio
async def test_cancel_on_terminal_request_returns_conflict(
    client, admin_token, service_token
) -> None:
    policy = {
        "policy_key": "engine.cancel_terminal",
        "name": "Cancel terminal",
        "artifact_type": "test",
        "stages": [
            {
                "name": "Stage",
                "stage_order": 1,
                "mode": "all",
                "rules": [
                    {"rule_type": "user", "rule_value": {"user_id": "u-alice"}}
                ],
            }
        ],
    }
    await _activate(client, admin_token, policy)
    req = await _create_request(
        client, service_token, "engine.cancel_terminal", artifact_id="cancel-t-1"
    )
    request_id = req["request_id"]
    task_id = req["tasks"][0]["id"]

    await client.post(
        f"/v1/awe/tasks/{task_id}/decision",
        json={"action": "approve"},
        headers=auth_header(_user_token("u-alice")),
    )

    resp = await client.post(
        f"/v1/awe/requests/{request_id}/cancel",
        json={"reason": "too late"},
        headers=auth_header(admin_token),
    )
    assert resp.status_code == 409
    assert "terminal state" in resp.json()["errors"][0]["message"]


@pytest.mark.asyncio
async def test_decision_on_terminal_request_returns_conflict(
    client, admin_token, service_token
) -> None:
    policy = {
        "policy_key": "engine.decide_terminal",
        "name": "Decide terminal",
        "artifact_type": "test",
        "stages": [
            {
                "name": "Stage",
                "stage_order": 1,
                "mode": "all",
                "rules": [
                    {"rule_type": "user", "rule_value": {"user_id": "u-alice"}}
                ],
            }
        ],
    }
    await _activate(client, admin_token, policy)
    req = await _create_request(
        client, service_token, "engine.decide_terminal", artifact_id="dec-t-1"
    )
    task_id = req["tasks"][0]["id"]

    await client.post(
        f"/v1/awe/tasks/{task_id}/decision",
        json={"action": "approve"},
        headers=auth_header(_user_token("u-alice")),
    )

    resp = await client.post(
        f"/v1/awe/tasks/{task_id}/decision",
        json={"action": "reject"},
        headers=auth_header(_user_token("u-alice")),
    )
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_reassign_errors_on_terminal_and_invalid_targets(
    client, admin_token, service_token
) -> None:
    policy = {
        "policy_key": "engine.reassign_errors",
        "name": "Reassign errors",
        "artifact_type": "test",
        "stages": [
            {
                "name": "Stage",
                "stage_order": 1,
                "mode": "all",
                "rules": [
                    {"rule_type": "user", "rule_value": {"user_id": "u-alice"}}
                ],
            }
        ],
    }
    await _activate(client, admin_token, policy)
    req = await _create_request(
        client, service_token, "engine.reassign_errors", artifact_id="re-err-1"
    )
    request_id = req["request_id"]
    task_id = req["tasks"][0]["id"]

    resp = await client.post(
        f"/v1/awe/tasks/{task_id}/reassign",
        json={"new_assignee": "u-alice", "reason": "same person"},
        headers=auth_header(admin_token),
    )
    assert resp.status_code == 409
    assert "equals the current assignee" in resp.json()["errors"][0]["message"]

    await client.post(
        f"/v1/awe/tasks/{task_id}/decision",
        json={"action": "approve"},
        headers=auth_header(_user_token("u-alice")),
    )

    resp = await client.post(
        f"/v1/awe/tasks/{task_id}/reassign",
        json={"new_assignee": "u-bob", "reason": "too late"},
        headers=auth_header(admin_token),
    )
    assert resp.status_code == 409
    assert "terminal state" in resp.json()["errors"][0]["message"]

    resp = await client.post(
        f"/v1/awe/requests/{request_id}/cancel",
        json={"reason": "n/a"},
        headers=auth_header(admin_token),
    )
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_required_approver_missing_after_expiry_rejects(
    client, admin_token, service_token
) -> None:
    policy = {
        "policy_key": "engine.required_expired",
        "name": "Required expired",
        "artifact_type": "test",
        "stages": [
            {
                "name": "Stage",
                "stage_order": 1,
                "mode": "any-n",
                "mode_value": 2,
                "sla_hours": 1,
                "rules": [
                    {"rule_type": "user", "rule_value": {"user_id": "u-alice"}},
                    {"rule_type": "user", "rule_value": {"user_id": "u-bob"}},
                    {
                        "rule_type": "user",
                        "rule_value": {"user_id": "u-dir"},
                        "required": True,
                    },
                ],
            }
        ],
    }
    await _activate(client, admin_token, policy)
    req = await _create_request(
        client, service_token, "engine.required_expired", artifact_id="req-exp-1"
    )
    request_id = req["request_id"]
    tasks = {t["assignee"]: t["id"] for t in req["tasks"]}

    await client.post(
        f"/v1/awe/tasks/{tasks['u-alice']}/decision",
        json={"action": "approve"},
        headers=auth_header(_user_token("u-alice")),
    )

    await _expire_tasks_for_assignees(request_id, {"u-dir"})
    await _run_sla_tick()

    resp = await client.post(
        f"/v1/awe/tasks/{tasks['u-bob']}/decision",
        json={"action": "approve"},
        headers=auth_header(_user_token("u-bob")),
    )
    assert resp.status_code == 201

    resp = await client.get(
        f"/v1/awe/requests/{request_id}", headers=auth_header(service_token)
    )
    assert resp.json()["status"] == "rejected"


@pytest.mark.asyncio
async def test_sla_on_breach_escalate_adds_approvers(
    client, admin_token, service_token
) -> None:
    policy = {
        "policy_key": "engine.sla_escalate",
        "name": "SLA escalate",
        "artifact_type": "test",
        "stages": [
            {
                "name": "Stage",
                "stage_order": 1,
                "mode": "all",
                "sla_hours": 1,
                "on_breach": "escalate",
                "escalation_rules": [
                    {
                        "rule_type": "user",
                        "rule_value": {"user_id": "u-escalate"},
                    }
                ],
                "rules": [
                    {"rule_type": "user", "rule_value": {"user_id": "u-alice"}}
                ],
            }
        ],
    }
    await _activate(client, admin_token, policy)
    req = await _create_request(
        client, service_token, "engine.sla_escalate", artifact_id="esc-1"
    )
    request_id = req["request_id"]

    await _expire_tasks_for_assignees(request_id)
    await _run_sla_tick()

    resp = await client.get(
        f"/v1/awe/tasks?assignee=*&request_id={request_id}",
        headers=auth_header(service_token),
    )
    assignees = {t["assignee"] for t in resp.json()["items"]}
    assert "u-escalate" in assignees

    resp = await client.get(
        f"/v1/awe/requests/{request_id}/events",
        headers=auth_header(service_token),
    )
    assert any(e["event_type"] == "stage_escalated" for e in resp.json())


@pytest.mark.asyncio
async def test_sla_on_breach_auto_approve(
    client, admin_token, service_token
) -> None:
    policy = {
        "policy_key": "engine.sla_auto_approve",
        "name": "SLA auto approve",
        "artifact_type": "test",
        "stages": [
            {
                "name": "Stage",
                "stage_order": 1,
                "mode": "all",
                "sla_hours": 1,
                "on_breach": "auto_approve",
                "rules": [
                    {"rule_type": "user", "rule_value": {"user_id": "u-alice"}}
                ],
            }
        ],
    }
    await _activate(client, admin_token, policy)
    req = await _create_request(
        client, service_token, "engine.sla_auto_approve", artifact_id="auto-ap-1"
    )
    request_id = req["request_id"]

    await _synthesize_stage_decision(
        request_id,
        stage_order=1,
        action="approve",
        reason="SLA breach auto-approve",
    )

    resp = await client.get(
        f"/v1/awe/requests/{request_id}", headers=auth_header(service_token)
    )
    assert resp.json()["status"] == "approved"


@pytest.mark.asyncio
async def test_sla_on_breach_auto_reject(
    client, admin_token, service_token
) -> None:
    policy = {
        "policy_key": "engine.sla_auto_reject",
        "name": "SLA auto reject",
        "artifact_type": "test",
        "stages": [
            {
                "name": "Stage",
                "stage_order": 1,
                "mode": "all",
                "sla_hours": 1,
                "on_breach": "auto_reject",
                "rules": [
                    {"rule_type": "user", "rule_value": {"user_id": "u-alice"}}
                ],
            }
        ],
    }
    await _activate(client, admin_token, policy)
    req = await _create_request(
        client, service_token, "engine.sla_auto_reject", artifact_id="auto-rj-1"
    )
    request_id = req["request_id"]

    await _synthesize_stage_decision(
        request_id,
        stage_order=1,
        action="reject",
        reason="SLA breach auto-reject",
    )

    resp = await client.get(
        f"/v1/awe/requests/{request_id}", headers=auth_header(service_token)
    )
    assert resp.json()["status"] == "rejected"
