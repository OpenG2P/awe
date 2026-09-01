"""Reproduce postgres-init search_path and ensure create_schema succeeds."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import create_async_engine


ADMIN_URL = os.environ.get(
    "AWE_PG_ADMIN_URL",
    "postgresql+asyncpg://postgres:postgres@127.0.0.1:55432/postgres",
)
APP_URL = os.environ.get(
    "AWE_PG_APP_URL",
    "postgresql+asyncpg://awe_user:awe_user@127.0.0.1:55432/awe_test",
)


async def _exec(engine, sql: str) -> None:
    async with engine.begin() as conn:
        await conn.exec_driver_sql(sql)


async def setup_restricted_role() -> None:
    admin = create_async_engine(ADMIN_URL, isolation_level="AUTOCOMMIT")
    try:
        await _exec(admin, "DROP DATABASE IF EXISTS awe_test")
        await _exec(admin, "DROP ROLE IF EXISTS awe_user")
        await _exec(admin, "CREATE ROLE awe_user LOGIN PASSWORD 'awe_user'")
        await _exec(admin, "CREATE DATABASE awe_test OWNER postgres")
        await _exec(admin, "GRANT CONNECT, CREATE, TEMP ON DATABASE awe_test TO awe_user")
    finally:
        await admin.dispose()

    db_admin = create_async_engine(
        "postgresql+asyncpg://postgres:postgres@127.0.0.1:55432/awe_test",
        isolation_level="AUTOCOMMIT",
    )
    try:
        await _exec(db_admin, "REVOKE ALL ON SCHEMA public FROM PUBLIC")
        await _exec(db_admin, "REVOKE ALL ON SCHEMA public FROM awe_user")
        await _exec(db_admin, "ALTER ROLE awe_user IN DATABASE awe_test SET search_path TO ''")
    finally:
        await db_admin.dispose()


async def prove_unfixed_fails() -> None:
    """Unqualified CREATE TABLE must fail with empty search_path."""
    engine = create_async_engine(APP_URL)
    try:
        async with engine.begin() as conn:
            await conn.exec_driver_sql("CREATE TABLE broken_probe (id int)")
        raise SystemExit("expected empty search_path to reject CREATE TABLE")
    except Exception as exc:
        msg = str(exc).lower()
        if "no schema has been selected" not in msg and "invalidschemaname" not in msg:
            # permission denied is also a locked-down public schema
            if "permission denied" not in msg:
                raise
        print("reproduced locked schema:", exc.__class__.__name__)
    finally:
        await engine.dispose()


async def prove_create_schema_works() -> None:
    os.environ["DATABASE_URL"] = APP_URL
    from awe.db import dispose_engine, init_engine
    from awe.models import create_schema
    from awe.models.base import Base

    await dispose_engine()
    engine = init_engine()
    try:
        await create_schema(engine)
        async with engine.connect() as conn:
            tables = (
                await conn.execute(
                    sa.text(
                        "SELECT count(*) FROM information_schema.tables "
                        "WHERE table_name = 'approval_request'"
                    )
                )
            ).scalar()
        if not tables:
            raise SystemExit("approval_request was not created")
        print("create_schema succeeded; approval_request exists")
    finally:
        await dispose_engine()
        Base.metadata.clear()


async def main() -> None:
    await setup_restricted_role()
    await prove_unfixed_fails()
    await prove_create_schema_works()


if __name__ == "__main__":
    asyncio.run(main())
