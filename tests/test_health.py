"""Health, version, config endpoints."""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_health_returns_up(client) -> None:
    resp = await client.get("/v1/awe/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["response"]["status"] == "UP"
    assert body["errors"] == []


@pytest.mark.asyncio
async def test_version_payload(client) -> None:
    resp = await client.get("/v1/awe/version")
    assert resp.status_code == 200
    body = resp.json()
    assert body["response"]["service_version"]
    assert "build_time" in body["response"]
    assert "git_commit" in body["response"]


@pytest.mark.asyncio
async def test_config_excludes_secrets(client) -> None:
    resp = await client.get("/v1/awe/config")
    assert resp.status_code == 200
    body = resp.json()
    cfg = body["response"]
    assert cfg["service_id"] == "openg2p.awe.test"
    assert "webhook" in cfg
    assert "max_attempts" in cfg["webhook"]
    # Make sure we don't accidentally leak Keycloak secrets via /config.
    assert "keycloak" not in cfg or "admin_client_secret" not in cfg.get("keycloak", {})
