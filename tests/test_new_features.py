"""End-to-end tests covering the new engine features.

Exercises:
  * parallel_group — two stages approve in parallel before the flow advances
  * forbid_self_approval — requester is filtered out of approver list
  * forbid_repeat_approvers — prior-stage approver filtered from later stage
  * required=True — quorum met but required user hasn't approved → still open
  * observer rule — observer gets a task but does not gate completion
  * delegation — alice on leave → bob gets alice's task
  * reassign — admin reassigns a task
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from jose import jwt

from .conftest import auth_header


def _user_token(sub: str) -> str:
    return jwt.encode(
        {"sub": sub, "realm_access": {"roles": []}, "email": f"{sub}@test"},
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


@pytest.mark.asyncio
async def test_parallel_stages_need_both_before_advancing(
    client, admin_token, service_token
) -> None:
    # Two stages at orders 1 and 2, same parallel_group — both must approve
    # before anything advances. Stage 3 runs only after group completes.
    policy = {
        "policy_key": "test.parallel",
        "name": "Parallel smoke",
        "artifact_type": "test",
        "stages": [
            {
                "name": "Legal",
                "stage_order": 1,
                "parallel_group": 1,
                "mode": "all",
                "rules": [
                    {"rule_type": "user", "rule_value": {"user_id": "u-legal"}}
                ],
            },
            {
                "name": "Finance",
                "stage_order": 2,
                "parallel_group": 1,
                "mode": "all",
                "rules": [
                    {"rule_type": "user", "rule_value": {"user_id": "u-fin"}}
                ],
            },
            {
                "name": "Director",
                "stage_order": 3,
                "mode": "all",
                "rules": [
                    {"rule_type": "user", "rule_value": {"user_id": "u-dir"}}
                ],
            },
        ],
    }
    await _activate(client, admin_token, policy)
    resp = await client.post(
        "/v1/awe/requests",
        json={
            "policy_key": "test.parallel",
            "artifact_type": "test",
            "artifact_id": "p-1",
            "context": {},
        },
        headers=auth_header(service_token),
    )
    assert resp.status_code == 201
    request_id = resp.json()["request_id"]

    # Both legal and finance should have tasks immediately.
    resp = await client.get(
        "/v1/awe/tasks?assignee=*&request_id=" + request_id,
        headers=auth_header(service_token),
    )
    assignees = {t["assignee"] for t in resp.json()}
    assert {"u-legal", "u-fin"}.issubset(assignees)

    # Legal approves — finance still pending, so no director task yet.
    legal_task = next(t for t in resp.json() if t["assignee"] == "u-legal")
    await client.post(
        f"/v1/awe/tasks/{legal_task['id']}/decision",
        json={"action": "approve"},
        headers=auth_header(_user_token("u-legal")),
    )
    resp2 = await client.get(
        "/v1/awe/tasks?assignee=*&request_id=" + request_id,
        headers=auth_header(service_token),
    )
    # Director not yet assigned.
    assert "u-dir" not in {t["assignee"] for t in resp2.json()}

    # Finance approves — group completes, director task arrives.
    fin_task = next(t for t in resp.json() if t["assignee"] == "u-fin")
    await client.post(
        f"/v1/awe/tasks/{fin_task['id']}/decision",
        json={"action": "approve"},
        headers=auth_header(_user_token("u-fin")),
    )
    resp3 = await client.get(
        "/v1/awe/tasks?assignee=*&request_id=" + request_id,
        headers=auth_header(service_token),
    )
    assert "u-dir" in {t["assignee"] for t in resp3.json()}


@pytest.mark.asyncio
async def test_self_approval_guard_filters_requester(
    client, admin_token, service_token
) -> None:
    policy = {
        "policy_key": "test.self",
        "name": "Self-approval filter",
        "artifact_type": "test",
        "forbid_self_approval": True,
        "stages": [
            {
                "name": "Peers",
                "stage_order": 1,
                "mode": "any-n",
                "mode_value": 1,
                "rules": [
                    {"rule_type": "user", "rule_value": {"user_id": "u-alice"}},
                    {"rule_type": "user", "rule_value": {"user_id": "u-bob"}},
                ],
            }
        ],
    }
    await _activate(client, admin_token, policy)
    resp = await client.post(
        "/v1/awe/requests",
        json={
            "policy_key": "test.self",
            "artifact_type": "test",
            "artifact_id": "s-1",
            "context": {},
            "requester": "u-alice",
        },
        headers=auth_header(service_token),
    )
    assignees = {t["assignee"] for t in resp.json()["tasks"]}
    assert "u-alice" not in assignees
    assert "u-bob" in assignees


@pytest.mark.asyncio
async def test_repeat_approver_filtered_from_later_stage(
    client, admin_token, service_token
) -> None:
    policy = {
        "policy_key": "test.repeat",
        "name": "No repeat approvers",
        "artifact_type": "test",
        "forbid_repeat_approvers": True,
        "stages": [
            {
                "name": "Stage 1",
                "stage_order": 1,
                "mode": "any-n",
                "mode_value": 1,
                "rules": [
                    {"rule_type": "user", "rule_value": {"user_id": "u-alice"}}
                ],
            },
            {
                "name": "Stage 2",
                "stage_order": 2,
                "mode": "any-n",
                "mode_value": 1,
                "on_empty": "skip",
                "rules": [
                    {"rule_type": "user", "rule_value": {"user_id": "u-alice"}},
                    {"rule_type": "user", "rule_value": {"user_id": "u-bob"}},
                ],
            },
        ],
    }
    await _activate(client, admin_token, policy)
    resp = await client.post(
        "/v1/awe/requests",
        json={
            "policy_key": "test.repeat",
            "artifact_type": "test",
            "artifact_id": "r-1",
            "context": {},
        },
        headers=auth_header(service_token),
    )
    request_id = resp.json()["request_id"]
    # Alice approves stage 1.
    alice_task_id = next(
        t["id"] for t in resp.json()["tasks"] if t["assignee"] == "u-alice"
    )
    await client.post(
        f"/v1/awe/tasks/{alice_task_id}/decision",
        json={"action": "approve"},
        headers=auth_header(_user_token("u-alice")),
    )
    # Stage 2 should not give alice a task — only bob.
    resp = await client.get(
        "/v1/awe/tasks?assignee=*&request_id=" + request_id,
        headers=auth_header(service_token),
    )
    stage2 = [t for t in resp.json() if t["stage_order"] == 2]
    assert {t["assignee"] for t in stage2} == {"u-bob"}


@pytest.mark.asyncio
async def test_observer_does_not_block_stage(
    client, admin_token, service_token
) -> None:
    policy = {
        "policy_key": "test.obs",
        "name": "Observer flow",
        "artifact_type": "test",
        "stages": [
            {
                "name": "Stage",
                "stage_order": 1,
                "mode": "any-n",
                "mode_value": 1,
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
    resp = await client.post(
        "/v1/awe/requests",
        json={
            "policy_key": "test.obs",
            "artifact_type": "test",
            "artifact_id": "o-1",
            "context": {},
        },
        headers=auth_header(service_token),
    )
    request_id = resp.json()["request_id"]
    # Alice approves — flow finishes even though legal (observer) never acts.
    alice_task_id = next(
        t["id"] for t in resp.json()["tasks"] if t["assignee"] == "u-alice"
    )
    r2 = await client.post(
        f"/v1/awe/tasks/{alice_task_id}/decision",
        json={"action": "approve"},
        headers=auth_header(_user_token("u-alice")),
    )
    assert r2.status_code == 201
    resp = await client.get(
        f"/v1/awe/requests/{request_id}", headers=auth_header(service_token)
    )
    assert resp.json()["status"] == "approved"


@pytest.mark.asyncio
async def test_required_approver_gates_quorum(
    client, admin_token, service_token
) -> None:
    # 3 approvers; quorum = 2; but required = u-dir. Two peers approving is
    # not enough if the director hasn't.
    policy = {
        "policy_key": "test.req",
        "name": "Required gates quorum",
        "artifact_type": "test",
        "stages": [
            {
                "name": "Stage",
                "stage_order": 1,
                "mode": "any-n",
                "mode_value": 2,
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
    resp = await client.post(
        "/v1/awe/requests",
        json={
            "policy_key": "test.req",
            "artifact_type": "test",
            "artifact_id": "rq-1",
            "context": {},
        },
        headers=auth_header(service_token),
    )
    request_id = resp.json()["request_id"]
    tasks = resp.json()["tasks"]

    alice_id = next(t["id"] for t in tasks if t["assignee"] == "u-alice")
    bob_id = next(t["id"] for t in tasks if t["assignee"] == "u-bob")
    dir_id = next(t["id"] for t in tasks if t["assignee"] == "u-dir")

    # Alice + Bob approve → quorum of 2 met, but required (dir) hasn't acted.
    for tid, user in [(alice_id, "u-alice"), (bob_id, "u-bob")]:
        await client.post(
            f"/v1/awe/tasks/{tid}/decision",
            json={"action": "approve"},
            headers=auth_header(_user_token(user)),
        )
    resp = await client.get(
        f"/v1/awe/requests/{request_id}", headers=auth_header(service_token)
    )
    assert resp.json()["status"] == "in_review"  # required hasn't approved

    # Dir approves → request approved.
    await client.post(
        f"/v1/awe/tasks/{dir_id}/decision",
        json={"action": "approve"},
        headers=auth_header(_user_token("u-dir")),
    )
    resp = await client.get(
        f"/v1/awe/requests/{request_id}", headers=auth_header(service_token)
    )
    assert resp.json()["status"] == "approved"


@pytest.mark.asyncio
async def test_delegation_redirects_task(
    client, admin_token, service_token
) -> None:
    now = datetime.now(timezone.utc)
    # Alice is on leave for the next day — bob covers.
    delegation = {
        "user_id": "u-alice",
        "delegate_to": "u-bob",
        "starts_at": (now - timedelta(hours=1)).isoformat(),
        "ends_at": (now + timedelta(days=1)).isoformat(),
        "reason": "on leave",
    }
    r = await client.post(
        "/v1/awe/delegations",
        json=delegation,
        headers=auth_header(admin_token),
    )
    assert r.status_code == 201, r.text

    policy = {
        "policy_key": "test.del",
        "name": "Delegation",
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
    resp = await client.post(
        "/v1/awe/requests",
        json={
            "policy_key": "test.del",
            "artifact_type": "test",
            "artifact_id": "d-1",
            "context": {},
        },
        headers=auth_header(service_token),
    )
    tasks = resp.json()["tasks"]
    assert len(tasks) == 1
    assert tasks[0]["assignee"] == "u-bob"
    assert tasks[0]["delegated_from"] == "u-alice"


@pytest.mark.asyncio
async def test_admin_can_reassign_task(
    client, admin_token, service_token
) -> None:
    policy = {
        "policy_key": "test.re",
        "name": "Reassign",
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
    resp = await client.post(
        "/v1/awe/requests",
        json={
            "policy_key": "test.re",
            "artifact_type": "test",
            "artifact_id": "re-1",
            "context": {},
        },
        headers=auth_header(service_token),
    )
    task = resp.json()["tasks"][0]

    r = await client.post(
        f"/v1/awe/tasks/{task['id']}/reassign",
        json={"new_assignee": "u-bob", "reason": "alice unavailable"},
        headers=auth_header(admin_token),
    )
    assert r.status_code == 200, r.text
    new_task = r.json()
    assert new_task["assignee"] == "u-bob"
    assert new_task["reassigned_from"] == "u-alice"
    assert new_task["status"] == "open"
