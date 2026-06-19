"""Unit and integration tests for webhook delivery."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from awe.db import get_engine
from awe.models import ApprovalEvent, ApprovalRequest, CallbackSecret, WebhookDelivery
from awe.models.base import utcnow
from awe.services import webhook as webhook_svc

from .conftest import auth_header


@pytest.mark.asyncio
async def test_load_secret(client):
    engine = get_engine()
    sm = async_sessionmaker(engine, expire_on_commit=False)
    async with sm() as session:
        assert await webhook_svc._load_secret(session, None) is None
        assert await webhook_svc._load_secret(session, "missing") is None

        sec = CallbackSecret(
            id="sec-1",
            caller_service="svc",
            secret_hash="hash123",
        )
        session.add(sec)
        await session.commit()
        assert await webhook_svc._load_secret(session, "sec-1") == "hash123"


@pytest.mark.asyncio
async def test_deliver_one_via_api_flow(client, admin_token, service_token):
    policy = {
        "policy_key": "webhook.deliver.flow",
        "name": "P",
        "artifact_type": "test",
        "stages": [
            {
                "name": "S",
                "stage_order": 1,
                "mode": "all",
                "rules": [{"rule_type": "user", "rule_value": {"user_id": "u-alice"}}],
            }
        ],
    }
    await client.post("/v1/awe/policies", json=policy, headers=auth_header(admin_token))
    await client.post(
        "/v1/awe/policies/webhook.deliver.flow/versions/1/activate",
        headers=auth_header(admin_token),
    )
    await client.post(
        "/v1/awe/requests",
        json={
            "policy_key": "webhook.deliver.flow",
            "artifact_type": "test",
            "artifact_id": "wh-art",
            "callback_url": "https://example/cb",
        },
        headers=auth_header(service_token),
    )
    listed = await client.get(
        "/v1/awe/admin/deliveries",
        headers=auth_header(admin_token),
    )
    assert listed.status_code == 200
    assert listed.json()
    delivery_id = listed.json()[0]["id"]

    engine = get_engine()
    sm = async_sessionmaker(engine, expire_on_commit=False)
    async with sm() as session:
        delivery = await session.get(WebhookDelivery, delivery_id)
        transport = httpx.MockTransport(lambda req: httpx.Response(200))
        with patch("httpx.AsyncClient", return_value=httpx.AsyncClient(transport=transport)):
            ok = await webhook_svc.deliver_one(session, delivery)
        assert ok is True
        assert delivery.status == "delivered"


@pytest.mark.asyncio
async def test_deliver_one_missing_event():
    session = AsyncMock()
    delivery = WebhookDelivery(
        event_id="missing",
        url="https://cb/hook",
        status="pending",
        attempt=0,
        next_attempt_at=utcnow(),
    )
    session.get = AsyncMock(return_value=None)
    assert await webhook_svc.deliver_one(session, delivery) is False
    assert delivery.status == "exhausted"


@pytest.mark.asyncio
async def test_deliver_one_missing_request():
    session = AsyncMock()
    event = ApprovalEvent(
        request_id="missing-req",
        event_type="stage_started",
        payload={},
    )
    delivery = WebhookDelivery(
        event_id=event.id,
        url="https://cb/hook",
        status="pending",
        attempt=0,
        next_attempt_at=utcnow(),
    )

    async def fake_get(model, pk):
        if model is ApprovalEvent:
            return event
        return None

    session.get = AsyncMock(side_effect=fake_get)
    assert await webhook_svc.deliver_one(session, delivery) is False


@pytest.mark.asyncio
async def test_deliver_one_non_2xx_retries(client, admin_token, service_token):
    policy = {
        "policy_key": "webhook.deliver.retry",
        "name": "P",
        "artifact_type": "test",
        "stages": [
            {
                "name": "S",
                "stage_order": 1,
                "mode": "all",
                "rules": [{"rule_type": "user", "rule_value": {"user_id": "u-alice"}}],
            }
        ],
    }
    await client.post("/v1/awe/policies", json=policy, headers=auth_header(admin_token))
    await client.post(
        "/v1/awe/policies/webhook.deliver.retry/versions/1/activate",
        headers=auth_header(admin_token),
    )
    await client.post(
        "/v1/awe/requests",
        json={
            "policy_key": "webhook.deliver.retry",
            "artifact_type": "test",
            "artifact_id": "wh-retry",
            "callback_url": "https://example/cb",
        },
        headers=auth_header(service_token),
    )
    delivery_id = (
        await client.get("/v1/awe/admin/deliveries", headers=auth_header(admin_token))
    ).json()[0]["id"]

    engine = get_engine()
    sm = async_sessionmaker(engine, expire_on_commit=False)
    async with sm() as session:
        delivery = await session.get(WebhookDelivery, delivery_id)
        transport = httpx.MockTransport(lambda req: httpx.Response(500, text="fail"))
        with patch("httpx.AsyncClient", return_value=httpx.AsyncClient(transport=transport)):
            with patch("awe.services.webhook.get_settings") as gs:
                cfg = gs.return_value.awe.webhook
                cfg.max_attempts = 1
                cfg.timeout_seconds = 1
                cfg.backoff_seconds = [60]
                ok = await webhook_svc.deliver_one(session, delivery)
        assert ok is False
        assert delivery.status == "exhausted"


@pytest.mark.asyncio
async def test_deliver_one_http_error_schedules_retry():
    session = AsyncMock()
    event = MagicMock()
    event.id = "ev-1"
    event.event_type = "stage_started"
    event.payload = {}
    event.created_at = utcnow()
    request = MagicMock()
    request.id = "req-1"
    request.artifact_type = "t"
    request.artifact_id = "a"
    request.status = "pending"
    request.current_stage_order = 1
    request.callback_secret_id = None

    async def fake_get(model, pk):
        if model is ApprovalEvent:
            return event
        if model is ApprovalRequest:
            return request
        return None

    session.get = AsyncMock(side_effect=fake_get)
    delivery = WebhookDelivery(
        event_id="ev-1",
        url="https://cb/hook",
        status="pending",
        attempt=0,
        next_attempt_at=utcnow(),
    )

    with patch("httpx.AsyncClient") as client_cls:
        client_cls.return_value.__aenter__.side_effect = httpx.HTTPError("down")
        with patch("awe.services.webhook.get_settings") as gs:
            gs.return_value.awe.webhook.max_attempts = 3
            gs.return_value.awe.webhook.timeout_seconds = 1
            gs.return_value.awe.webhook.backoff_seconds = [30, 60]
            ok = await webhook_svc.deliver_one(session, delivery)
    assert ok is False
    assert delivery.status == "pending"
    assert delivery.attempt == 1


@pytest.mark.asyncio
async def test_deliver_one_with_secret_signs():
    session = AsyncMock()
    event = MagicMock()
    event.id = "ev-1"
    event.event_type = "stage_started"
    event.payload = {"stage_order": 2}
    event.created_at = utcnow()
    request = MagicMock()
    request.id = "req-1"
    request.artifact_type = "t"
    request.artifact_id = "a"
    request.status = "pending"
    request.current_stage_order = 1
    request.callback_secret_id = "sec-1"

    async def fake_get(model, pk):
        if model is ApprovalEvent:
            return event
        if model is ApprovalRequest:
            return request
        return None

    session.get = AsyncMock(side_effect=fake_get)
    session.execute = AsyncMock()
    delivery = WebhookDelivery(
        event_id="ev-1",
        url="https://cb/hook",
        status="pending",
        attempt=0,
        next_attempt_at=utcnow(),
    )

    transport = httpx.MockTransport(lambda req: httpx.Response(200))
    with patch("httpx.AsyncClient", return_value=httpx.AsyncClient(transport=transport)):
        with patch(
            "awe.services.webhook._load_secret",
            new=AsyncMock(return_value="secret"),
        ):
            ok = await webhook_svc.deliver_one(session, delivery)
    assert ok is True
