"""Unit tests for schemas, helpers, config, db, and small utilities."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from awe.config import Settings, _find_config_path, get_settings
from awe.db import (
    _build_database_url,
    dispose_engine,
    get_engine,
    get_sessionmaker,
    init_engine,
    session_scope,
)
from awe.models import create_schema
from awe.schemas.delegation import DelegationCreate
from awe.schemas.policy import ApproverRuleIn, StageIn
from awe.schemas.request import DecisionIn
from awe.schemas.responses import ResponseForbiddenAdmin, auth_protected
from awe.services.assignee_id import (
    assignee_id_from_claims,
    assignee_id_from_keycloak_user,
    first_assignee_id,
)
from awe.services.task_search import _stringify, build_task_search_text


class _FakeRequest:
    def __init__(self, context, artifact_id="art-1"):
        self.context = context
        self.artifact_id = artifact_id


def test_first_assignee_id_empty():
    assert first_assignee_id({}) is None


def test_assignee_id_from_keycloak_user_maps_id_to_sub():
    assert assignee_id_from_keycloak_user({"id": "kc-uuid"}) == "kc-uuid"


def test_assignee_id_from_claims():
    assert assignee_id_from_claims({"preferred_username": "alice"}) == "alice"


def test_stringify_variants():
    assert _stringify(None) == ""
    assert _stringify({"a": 1}) == '{"a": 1}'
    assert _stringify([1, 2]) == "[1, 2]"
    assert _stringify("  x ") == "x"


def test_build_task_search_text_core_and_extra_keys():
    req = _FakeRequest(
        {
            "record_name": "Alice",
            "change_request_id": "cr-1",
            "district": "D1",
            "nested": {"x": 1},
            "tags": ["a"],
        },
        artifact_id="fallback-id",
    )
    text = build_task_search_text(req)
    assert "Alice" in text
    assert "cr-1" in text
    assert "D1" in text
    assert "fallback-id" not in text


def test_build_task_search_text_uses_artifact_id_when_no_cr():
    req = _FakeRequest({"record_name": "Bob"}, artifact_id="only-id")
    text = build_task_search_text(req)
    assert text is not None
    assert "only-id" in text


def test_build_task_search_text_empty():
    assert build_task_search_text(_FakeRequest({}, artifact_id="")) is None


def test_delegation_create_validators():
    now = datetime.now(timezone.utc)
    with pytest.raises(ValidationError):
        DelegationCreate(
            user_id="a",
            delegate_to="b",
            starts_at=now,
            ends_at=now,
        )
    with pytest.raises(ValidationError):
        DelegationCreate(
            user_id="same",
            delegate_to="same",
            starts_at=now,
            ends_at=now + timedelta(hours=1),
        )


def test_policy_schema_validators():
    with pytest.raises(ValidationError):
        ApproverRuleIn(rule_type="bad", rule_value={})
    with pytest.raises(ValidationError):
        ApproverRuleIn(rule_type="user", rule_value={}, kind="bad")
    with pytest.raises(ValidationError):
        StageIn(name="s", stage_order=1, mode="bad")
    with pytest.raises(ValidationError):
        StageIn(name="s", stage_order=1, on_empty="bad")
    with pytest.raises(ValidationError):
        StageIn(name="s", stage_order=1, on_breach="bad")
    assert StageIn(name="s", stage_order=1, mode="ANY-N").mode == "any-n"


def test_decision_in_invalid_action():
    with pytest.raises(ValidationError):
        DecisionIn(action="invalid", comment=None)


def test_auth_protected_merges_responses():
    merged = auth_protected(ResponseForbiddenAdmin)
    assert 401 in merged
    assert 403 in merged


def test_find_config_path_from_env(tmp_path, monkeypatch):
    cfg = tmp_path / "custom.yaml"
    cfg.write_text("awe:\n  service_id: test\n")
    monkeypatch.setenv("CONFIG_PATH", str(cfg))
    get_settings.cache_clear()
    assert _find_config_path() == cfg


def test_find_config_path_not_found(monkeypatch, tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.delenv("CONFIG_PATH", raising=False)
    monkeypatch.chdir(empty)
    get_settings.cache_clear()
    monkeypatch.setattr(Path, "exists", lambda self: False)
    with pytest.raises(FileNotFoundError):
        _find_config_path()


def test_find_config_path_cwd_fallback(tmp_path, monkeypatch):
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    cfg_file = cfg_dir / "default.yaml"
    cfg_file.write_text("awe:\n  service_id: cwd-test\n")
    monkeypatch.delenv("CONFIG_PATH", raising=False)
    monkeypatch.chdir(tmp_path)
    get_settings.cache_clear()
    assert _find_config_path() == cfg_file


def test_build_database_url_from_env(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@h/db")
    assert _build_database_url() == "postgresql+asyncpg://u:p@h/db"


def test_build_database_url_piecewise(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("DB_HOST", "dbhost")
    monkeypatch.setenv("DB_PORT", "5433")
    monkeypatch.setenv("DB_NAME", "mydb")
    monkeypatch.setenv("DB_USER", "user")
    monkeypatch.setenv("DB_PASSWORD", "pass")
    url = _build_database_url()
    assert "dbhost:5433/mydb" in url
    assert "user:pass" in url


@pytest.mark.asyncio
async def test_db_get_engine_before_init():
    await dispose_engine()
    with pytest.raises(RuntimeError, match="not initialized"):
        get_engine()
    with pytest.raises(RuntimeError, match="not initialized"):
        get_sessionmaker()


@pytest.mark.asyncio
async def test_session_scope_rollback():
    init_engine()
    try:
        async with session_scope() as session:
            assert isinstance(session, AsyncSession)
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    finally:
        await dispose_engine()


@pytest.mark.asyncio
async def test_create_schema_direct():
    engine = init_engine()
    try:
        await create_schema(engine)
    finally:
        await dispose_engine()



@pytest.mark.asyncio
async def test_init_engine_postgres_pool_kwargs(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@localhost/db")
    await dispose_engine()
    engine = init_engine()
    try:
        assert engine is not None
    finally:
        await dispose_engine()

