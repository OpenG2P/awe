"""Extended health endpoint tests."""

from __future__ import annotations

import importlib.metadata
from unittest.mock import patch

import pytest


@pytest.mark.asyncio
async def test_health_not_ready(client) -> None:
    with patch("awe.main.is_startup_complete", return_value=False):
        resp = await client.get("/v1/awe/health")
    assert resp.status_code == 503
    assert resp.json()["errors"][0]["errorCode"] == "AWE-005"


@pytest.mark.asyncio
async def test_health_db_failure(client) -> None:
    with patch("awe.controllers.health.get_engine", side_effect=RuntimeError("db down")):
        resp = await client.get("/v1/awe/health")
    assert resp.status_code == 503
    assert resp.json()["errors"][0]["errorCode"] == "AWE-006"


@pytest.mark.asyncio
async def test_version_package_not_found(client) -> None:
    with patch(
        "importlib.metadata.version",
        side_effect=importlib.metadata.PackageNotFoundError,
    ):
        resp = await client.get("/v1/awe/version")
    assert resp.status_code == 200
    assert resp.json()["response"]["service_version"] == "0.1.0-dev"
