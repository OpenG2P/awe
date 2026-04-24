"""Audit log: mutations leave a trail; viewers can read, non-admins can't."""

from __future__ import annotations

import pytest

from .conftest import auth_header


def _sample_policy() -> dict:
    return {
        "policy_key": "registry.cr.audit",
        "name": "CR for audit tests",
        "artifact_type": "registry.change_request",
        "stages": [
            {
                "name": "Officers",
                "stage_order": 1,
                "mode": "any-n",
                "mode_value": 1,
                "rules": [
                    {"rule_type": "user", "rule_value": {"user_id": "u-alice"}}
                ],
            }
        ],
    }


@pytest.mark.asyncio
async def test_policy_lifecycle_writes_audit_rows(
    client, admin_token, viewer_token
) -> None:
    h = auth_header(admin_token)

    # Create → activate → deactivate should each leave one audit row.
    await client.post("/v1/awe/policies", json=_sample_policy(), headers=h)
    await client.post(
        "/v1/awe/policies/registry.cr.audit/versions/1/activate", headers=h
    )
    await client.post(
        "/v1/awe/policies/registry.cr.audit/versions/1/deactivate", headers=h
    )

    resp = await client.get(
        "/v1/awe/admin/audit?resource_id=registry.cr.audit:v1",
        headers=auth_header(viewer_token),   # VIEWER can read the log
    )
    assert resp.status_code == 200, resp.text
    rows = resp.json()
    actions = [r["action"] for r in rows]
    assert "policy.create" in actions
    assert "policy.activate" in actions
    assert "policy.deactivate" in actions
    # Most recent first
    assert rows[0]["action"] == "policy.deactivate"
    # Actor metadata flows through
    assert all(r["actor"] == "test-admin" for r in rows)
    assert all(r["actor_email"] == "admin@test" for r in rows)


@pytest.mark.asyncio
async def test_audit_captures_before_and_after(client, admin_token) -> None:
    h = auth_header(admin_token)
    payload = _sample_policy()
    payload["policy_key"] = "registry.cr.diff"
    await client.post("/v1/awe/policies", json=payload, headers=h)

    # Edit the draft — audit row should carry before + after.
    updated = {**payload, "name": "Renamed policy"}
    await client.patch(
        "/v1/awe/policies/registry.cr.diff/versions/1",
        json=updated,
        headers=h,
    )

    resp = await client.get(
        "/v1/awe/admin/audit?action=policy.update&resource_id=registry.cr.diff:v1",
        headers=h,
    )
    assert resp.status_code == 200
    rows = resp.json()
    assert len(rows) == 1
    row = rows[0]
    assert row["before"]["name"] == "CR for audit tests"
    assert row["after"]["name"] == "Renamed policy"


@pytest.mark.asyncio
async def test_viewer_cannot_mutate_policies(client, viewer_token) -> None:
    resp = await client.post(
        "/v1/awe/policies", json=_sample_policy(), headers=auth_header(viewer_token)
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_viewer_can_read_policies_and_audit(
    client, admin_token, viewer_token
) -> None:
    # Admin sets up
    await client.post(
        "/v1/awe/policies", json=_sample_policy(), headers=auth_header(admin_token)
    )

    # Viewer reads — should succeed
    resp = await client.get(
        "/v1/awe/policies", headers=auth_header(viewer_token)
    )
    assert resp.status_code == 200
    assert any(p["policy_key"] == "registry.cr.audit" for p in resp.json())

    resp = await client.get(
        "/v1/awe/admin/audit", headers=auth_header(viewer_token)
    )
    assert resp.status_code == 200
    assert len(resp.json()) >= 1


@pytest.mark.asyncio
async def test_audit_filters_by_date_and_action(
    client, admin_token
) -> None:
    h = auth_header(admin_token)
    payload = _sample_policy()
    payload["policy_key"] = "registry.cr.filter"
    await client.post("/v1/awe/policies", json=payload, headers=h)

    # Action filter — only `policy.create` for this key
    resp = await client.get(
        "/v1/awe/admin/audit"
        "?action=policy.create&resource_id=registry.cr.filter:v1",
        headers=h,
    )
    assert resp.status_code == 200
    rows = resp.json()
    assert all(r["action"] == "policy.create" for r in rows)
    assert len(rows) == 1

    # Wildly-past `since` — should return nothing
    resp = await client.get(
        "/v1/awe/admin/audit?since=2100-01-01T00:00:00Z", headers=h
    )
    assert resp.status_code == 200
    assert resp.json() == []
