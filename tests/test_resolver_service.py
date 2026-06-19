"""Unit tests for awe.services.resolver."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from awe.models import ApproverRule
from awe.services import resolver as resolver_svc
from awe.services.keycloak_admin import KeycloakAdminError


def _rule(rule_type: str, rule_value: dict) -> ApproverRule:
    r = MagicMock(spec=ApproverRule)
    r.rule_type = rule_type
    r.rule_value = rule_value
    return r


@pytest.mark.asyncio
async def test_resolve_user_rule():
    rules = [_rule("user", {"user_id": "alice"})]
    assert await resolver_svc.resolve_stage(rules, {}) == ["alice"]


@pytest.mark.asyncio
async def test_resolve_unknown_rule_type():
    with pytest.raises(resolver_svc.ResolutionError, match="Unknown"):
        await resolver_svc._resolve_one(_rule("bogus", {}), {}, {})


@pytest.mark.asyncio
async def test_keycloak_assignee_id_missing():
    with pytest.raises(resolver_svc.ResolutionError):
        resolver_svc._keycloak_assignee_id({})


@pytest.mark.asyncio
async def test_resolve_expression_variants():
    assert resolver_svc._resolve_expression(None, {}) == []
    assert resolver_svc._resolve_expression({"var": "head"}, {"head": "u1"}) == ["u1"]
    assert resolver_svc._resolve_expression({"var": "ids"}, {"ids": ["a", "b"]}) == ["a", "b"]
    assert resolver_svc._resolve_expression({"var": "n"}, {"n": 0}) == []
    with pytest.raises(resolver_svc.ResolutionError):
        resolver_svc._resolve_expression({"!!!": True}, {})


@pytest.mark.asyncio
async def test_resolve_http_success():
    transport = httpx.MockTransport(
        lambda req: httpx.Response(200, json={"user_ids": ["x", "y"]})
    )
    with patch("httpx.AsyncClient", return_value=httpx.AsyncClient(transport=transport)):
        ids = await resolver_svc._resolve_http("https://example/resolver", {"k": "v"})
    assert ids == ["x", "y"]


@pytest.mark.asyncio
async def test_resolve_http_failure():
    transport = httpx.MockTransport(lambda req: httpx.Response(500))
    with patch("httpx.AsyncClient", return_value=httpx.AsyncClient(transport=transport)):
        with pytest.raises(resolver_svc.ResolutionError):
            await resolver_svc._resolve_http("https://example/resolver", {})


@pytest.mark.asyncio
async def test_resolve_keycloak_role_realm():
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/users"):
            return httpx.Response(200, json=[{"username": "bob"}])
        return httpx.Response(404)

    with patch(
        "awe.services.resolver.keycloak_admin_token",
        new=AsyncMock(return_value="tok"),
    ), patch("awe.services.resolver.get_settings") as gs:
        gs.return_value.awe.keycloak.base_url = "https://kc"
        gs.return_value.awe.keycloak.realm = "staff"
        transport = httpx.MockTransport(handler)
        with patch("httpx.AsyncClient", return_value=httpx.AsyncClient(transport=transport)):
            ids = await resolver_svc._resolve_keycloak_role("OFFICER")
    assert ids == ["bob"]


@pytest.mark.asyncio
async def test_resolve_keycloak_role_client_not_found():
    transport = httpx.MockTransport(lambda req: httpx.Response(200, json=[]))
    with patch(
        "awe.services.resolver.keycloak_admin_token",
        new=AsyncMock(return_value="tok"),
    ), patch("awe.services.resolver.get_settings") as gs:
        gs.return_value.awe.keycloak.base_url = "https://kc"
        gs.return_value.awe.keycloak.realm = "staff"
        with patch("httpx.AsyncClient", return_value=httpx.AsyncClient(transport=transport)):
            with pytest.raises(resolver_svc.ResolutionError, match="client not found"):
                await resolver_svc._resolve_keycloak_role("R", "missing")


@pytest.mark.asyncio
async def test_resolve_keycloak_role_admin_error():
    with patch(
        "awe.services.resolver.keycloak_admin_token",
        new=AsyncMock(side_effect=KeycloakAdminError("down")),
    ):
        with pytest.raises(resolver_svc.ResolutionError, match="down"):
            await resolver_svc._resolve_keycloak_role("R")


@pytest.mark.asyncio
async def test_resolve_keycloak_group():
    async def handler(request: httpx.Request) -> httpx.Response:
        if "group-by-path" in request.url.path:
            return httpx.Response(200, json={"id": "g1"})
        if request.url.path.endswith("/members"):
            return httpx.Response(200, json=[{"username": "carol"}])
        return httpx.Response(404)

    with patch(
        "awe.services.resolver.keycloak_admin_token",
        new=AsyncMock(return_value="tok"),
    ), patch("awe.services.resolver.get_settings") as gs:
        gs.return_value.awe.keycloak.base_url = "https://kc"
        gs.return_value.awe.keycloak.realm = "staff"
        transport = httpx.MockTransport(handler)
        with patch("httpx.AsyncClient", return_value=httpx.AsyncClient(transport=transport)):
            ids = await resolver_svc._resolve_keycloak_group("/team")
    assert ids == ["carol"]


@pytest.mark.asyncio
async def test_resolve_stage_dedup_and_default_cache():
    rules = [
        _rule("user", {"user_id": "alice"}),
        _rule("user", {"user_id": "alice"}),
        _rule("user", {"user_id": "bob"}),
    ]
    assert await resolver_svc.resolve_stage(rules, {}, cache=None) == ["alice", "bob"]
