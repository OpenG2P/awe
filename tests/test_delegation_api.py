"""Integration tests for delegation endpoints."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from .conftest import auth_header


def _window():
    now = datetime.now(timezone.utc)
    return now, now + timedelta(days=7)


@pytest.mark.asyncio
async def test_delegation_crud(client, admin_token, viewer_token) -> None:
    starts, ends = _window()
    create = await client.post(
        "/v1/awe/delegations",
        json={
            "user_id": "u-alice",
            "delegate_to": "u-bob",
            "starts_at": starts.isoformat(),
            "ends_at": ends.isoformat(),
            "reason": "leave",
        },
        headers=auth_header(admin_token),
    )
    assert create.status_code == 201
    body = create.json()
    delegation_id = body["id"]

    listed = await client.get(
        "/v1/awe/delegations?user_id=u-alice",
        headers=auth_header(viewer_token),
    )
    assert listed.status_code == 200
    assert any(d["id"] == delegation_id for d in listed.json())

    deleted = await client.delete(
        f"/v1/awe/delegations/{delegation_id}",
        headers=auth_header(admin_token),
    )
    assert deleted.status_code == 204


@pytest.mark.asyncio
async def test_delegation_delete_not_found(client, admin_token) -> None:
    resp = await client.delete(
        "/v1/awe/delegations/does-not-exist",
        headers=auth_header(admin_token),
    )
    assert resp.status_code == 404
    assert resp.json()["errors"][0]["errorCode"] == "AWE-004"
