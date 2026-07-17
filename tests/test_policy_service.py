"""Direct unit tests for awe.services.policy."""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from awe.schemas.policy import ApproverRuleIn, PolicyCreate, StageIn
from awe.services import policy as policy_svc


def _payload(*, policy_key: str = "svc.policy") -> PolicyCreate:
    return PolicyCreate(
        policy_key=policy_key,
        name="Service test",
        artifact_type="test",
        stages=[
            StageIn(
                name="S1",
                stage_order=1,
                mode="all",
                rules=[ApproverRuleIn(rule_type="user", rule_value={"user_id": "u1"})],
            )
        ],
    )


@pytest.mark.asyncio
async def test_create_draft_and_conflict(client) -> None:
    from awe.db import get_engine

    sm = async_sessionmaker(get_engine(), expire_on_commit=False)
    async with sm() as session:
        p = await policy_svc.create_draft(session, _payload(policy_key="svc.create"), actor="admin")
        await session.commit()
        assert p.version == 1
        assert p.status == "draft"

    async with sm() as session:
        with pytest.raises(policy_svc.PolicyError, match="already exists"):
            await policy_svc.create_draft(session, _payload(policy_key="svc.create"))


@pytest.mark.asyncio
async def test_add_update_activate_deactivate(client) -> None:
    from awe.db import get_engine

    sm = async_sessionmaker(get_engine(), expire_on_commit=False)
    async with sm() as session:
        await policy_svc.create_draft(session, _payload(policy_key="svc.ver"), actor="a")
        v2 = await policy_svc.add_draft_version(
            session, "svc.ver", _payload(policy_key="svc.ver"), actor="b"
        )
        assert v2.version == 2
        await session.commit()

    async with sm() as session:
        with pytest.raises(policy_svc.PolicyNotFound):
            await policy_svc.add_draft_version(
                session, "missing", _payload(policy_key="missing")
            )

    async with sm() as session:
        active = await policy_svc.activate_version(session, "svc.ver", 1)
        assert active.status == "active"
        await session.commit()

    async with sm() as session:
        again = await policy_svc.activate_version(session, "svc.ver", 1)
        assert again.status == "active"

    async with sm() as session:
        updated = await policy_svc.update_draft(
            session, "svc.ver", 2, _payload(policy_key="svc.ver"), actor="c"
        )
        assert updated.created_by == "c"
        await session.commit()

    async with sm() as session:
        with pytest.raises(policy_svc.PolicyError, match="immutable"):
            await policy_svc.update_draft(
                session, "svc.ver", 1, _payload(policy_key="svc.ver")
            )

    async with sm() as session:
        archived = await policy_svc.deactivate_version(session, "svc.ver", 1)
        assert archived.status == "archived"
        await session.commit()

    async with sm() as session:
        with pytest.raises(policy_svc.PolicyError, match="not 'active'"):
            await policy_svc.deactivate_version(session, "svc.ver", 1)


@pytest.mark.asyncio
async def test_get_list_helpers(client) -> None:
    from awe.db import get_engine

    sm = async_sessionmaker(get_engine(), expire_on_commit=False)
    async with sm() as session:
        await policy_svc.create_draft(session, _payload(policy_key="svc.list.a"))
        await policy_svc.create_draft(session, _payload(policy_key="svc.list.b"))
        await session.commit()

    async with sm() as session:
        assert await policy_svc.get_version(session, "svc.list.a", 99) is None
        assert await policy_svc.get_active(session, "svc.list.a") is None
        policies = await policy_svc.list_policies(session)
        keys = {p.policy_key for p in policies}
        assert "svc.list.a" in keys
        versions = await policy_svc.list_versions(session, "svc.list.a")
        assert len(versions) == 1
        assert await policy_svc.list_versions(session, "no.key") == []

    async with sm() as session:
        with pytest.raises(policy_svc.PolicyNotFound):
            await policy_svc.activate_version(session, "ghost", 1)
        with pytest.raises(policy_svc.PolicyNotFound):
            await policy_svc.deactivate_version(session, "ghost", 1)
        with pytest.raises(policy_svc.PolicyNotFound):
            await policy_svc.update_draft(session, "ghost", 1, _payload(policy_key="ghost"))
