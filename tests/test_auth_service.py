"""Unit tests for awe.services.auth."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi import HTTPException
from jose import jwt

from awe.services.auth import (
    _extract_roles,
    _fetch_jwks,
    _verify_token,
    current_identity,
)


def test_extract_roles_resource_access():
    claims = {
        "realm_access": {"roles": ["R1"]},
        "resource_access": {"portal": {"roles": ["R2"]}, "bad": "x"},
    }
    assert _extract_roles(claims) == ["R1", "R2"]


@pytest.mark.asyncio
async def test_fetch_jwks_caches():
    import awe.services.auth as auth_mod

    auth_mod._jwks_cache = None
    transport = httpx.MockTransport(
        lambda req: httpx.Response(200, json={"keys": []})
    )
    with patch("httpx.AsyncClient", return_value=httpx.AsyncClient(transport=transport)):
        jwks1 = await _fetch_jwks("https://kc/certs")
        jwks2 = await _fetch_jwks("https://kc/certs")
    assert jwks1 is jwks2
    auth_mod._jwks_cache = None


@pytest.mark.asyncio
async def test_verify_token_dev_mode_invalid():
    with patch("awe.services.auth.get_settings") as gs:
        gs.return_value.awe.keycloak.issuer = ""
        with pytest.raises(HTTPException) as exc:
            await _verify_token("not-a-jwt")
        assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_verify_token_dev_mode_valid():
    token = jwt.encode({"sub": "u1"}, "secret", algorithm="HS256")
    with patch("awe.services.auth.get_settings") as gs:
        gs.return_value.awe.keycloak.issuer = ""
        claims = await _verify_token(token)
    assert claims["sub"] == "u1"


@pytest.mark.asyncio
async def test_verify_token_jwks_fetch_failure():
    with patch("awe.services.auth.get_settings") as gs:
        gs.return_value.awe.keycloak.issuer = "https://issuer"
        gs.return_value.awe.keycloak.jwks_url = "https://kc/certs"
        with patch(
            "awe.services.auth._fetch_jwks",
            new=AsyncMock(side_effect=httpx.HTTPError("timeout")),
        ):
            with pytest.raises(HTTPException) as exc:
                await _verify_token("tok")
            assert exc.value.status_code == 503


@pytest.mark.asyncio
async def test_verify_token_jwks_unexpected_failure():
    with patch("awe.services.auth.get_settings") as gs:
        gs.return_value.awe.keycloak.issuer = "https://issuer"
        gs.return_value.awe.keycloak.jwks_url = "https://kc/certs"
        with patch(
            "awe.services.auth._fetch_jwks",
            new=AsyncMock(side_effect=ValueError("bad jwks")),
        ):
            with pytest.raises(HTTPException) as exc:
                await _verify_token("tok")
            assert exc.value.status_code == 503


@pytest.mark.asyncio
async def test_verify_token_decode_failure():
    with patch("awe.services.auth.get_settings") as gs:
        gs.return_value.awe.keycloak.issuer = "https://issuer"
        gs.return_value.awe.keycloak.jwks_url = "https://kc/certs"
        with patch(
            "awe.services.auth._fetch_jwks",
            new=AsyncMock(return_value={"keys": []}),
        ), patch("awe.services.auth.jwt.decode", side_effect=Exception("boom")):
            with pytest.raises(HTTPException) as exc:
                await _verify_token("tok")
            assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_current_identity_missing_sub():
    creds = MagicMock()
    creds.credentials = jwt.encode({"email": "a@b"}, "s", algorithm="HS256")
    with patch("awe.services.auth.get_settings") as gs:
        gs.return_value.awe.keycloak.issuer = ""
        with pytest.raises(HTTPException) as exc:
            await current_identity(creds)
        assert exc.value.status_code == 401
