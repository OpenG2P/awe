"""Lifespan startup/shutdown — background worker paths in main.py."""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest


async def _hanging_worker(_engine) -> None:
    try:
        await asyncio.Event().wait()
    except asyncio.CancelledError:
        raise


@pytest.mark.asyncio
async def test_lifespan_starts_and_cancels_workers(client) -> None:
    from awe.main import app, lifespan

    with patch("awe.main._test_mode_enabled", return_value=False):
        with patch("awe.main.webhook_dispatcher_loop", side_effect=_hanging_worker):
            with patch("awe.main.sla_monitor_loop", side_effect=_hanging_worker):
                async with lifespan(app):
                    import awe.main as main_mod

                    assert main_mod._webhook_task is not None
                    assert main_mod._sla_task is not None
                    assert main_mod.is_startup_complete() is True

                assert main_mod._startup_complete is False


@pytest.mark.asyncio
async def test_test_mode_skips_workers(client) -> None:
    from awe.main import app, lifespan

    with patch("awe.main._test_mode_enabled", return_value=True):
        async with lifespan(app):
            import awe.main as main_mod

            assert main_mod._webhook_task is None
            assert main_mod._sla_task is None
            assert main_mod.is_startup_complete() is True
