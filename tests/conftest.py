"""
Test fixtures.

Smoke tests run against an in-process FastAPI app with an aiosqlite database —
fast, hermetic, no external services. Production uses Postgres; the SQLAlchemy
schema is portable and exercised on both via the same `Base.metadata.create_all`
call from `awe.models.create_schema`.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Make `awe` importable without installing the package.
SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

# Point the app at an ephemeral in-memory SQLite DB before any awe.* imports
# trigger config/engine init.
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
os.environ.setdefault("CONFIG_PATH", str(Path(__file__).parent / "fixtures" / "test-config.yaml"))

import pytest_asyncio  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402


@pytest_asyncio.fixture
async def client():
    """An AsyncClient bound to the FastAPI app, with the lifespan running."""
    from awe.main import app  # imported here so env vars take effect first

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as c:
        # Trigger startup
        async with app.router.lifespan_context(app):
            yield c


@pytest_asyncio.fixture
async def admin_token() -> str:
    """A bearer token with the `awe-admin` realm role.

    Dev-mode auth (issuer empty) accepts any unsigned JWT; the test fixture
    crafts one with the role we need.
    """
    from jose import jwt

    return jwt.encode(
        {
            "sub": "test-admin",
            "realm_access": {"roles": ["awe-admin"]},
            "email": "admin@test",
        },
        "secret",
        algorithm="HS256",
    )


@pytest_asyncio.fixture
async def user_token() -> str:
    from jose import jwt

    return jwt.encode(
        {"sub": "u-alice", "realm_access": {"roles": []}, "email": "alice@test"},
        "secret",
        algorithm="HS256",
    )


@pytest_asyncio.fixture
async def service_token() -> str:
    """A token with no `email` claim — treated as a service account."""
    from jose import jwt

    return jwt.encode(
        {"sub": "svc-registry", "realm_access": {"roles": []}},
        "secret",
        algorithm="HS256",
    )


def auth_header(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}
