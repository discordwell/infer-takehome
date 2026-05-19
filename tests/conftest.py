"""Shared test fixtures.

We patch the Playwright runner so integration tests don't launch real Chromium —
the MockFlow ignores the page/context arguments, so an AsyncMock suffices.
"""

from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from backend import playwright_runner, result_store, storage
from backend.slot_manager import slot_manager


@pytest.fixture
def tmp_session_dir(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setattr(storage, "STORAGE_DIR", tmp_path)
    return tmp_path


@pytest.fixture(autouse=True)
def tmp_result_store_dir(tmp_path: Path, monkeypatch) -> Path:
    path = tmp_path / "results"
    monkeypatch.setattr(result_store, "RESULTS_DIR", path)
    return path


@pytest.fixture(autouse=True)
def reset_slot_manager():
    """slot_manager is a process-wide singleton; reset between tests so
    a carrier-busy claim from one test doesn't 423 the next."""
    slot_manager._slots.clear()
    slot_manager._carrier_owners.clear()
    yield
    slot_manager._slots.clear()
    slot_manager._carrier_owners.clear()


@pytest.fixture(autouse=True)
def reset_session_manager():
    """session_manager is also a process-wide singleton; cancel any in-flight
    orchestrator tasks between tests so a pending request_mfa doesn't keep
    the event loop alive for the default 300s MFA timeout."""
    from backend.session_manager import manager

    def _cleanup():
        for sess in list(manager._sessions.values()):
            if sess.task and not sess.task.done():
                sess.task.cancel()
        manager._sessions.clear()

    _cleanup()
    yield
    _cleanup()


@pytest.fixture(autouse=True)
def short_mfa_timeout(monkeypatch):
    """Mock flows often don't submit MFA in tests; trim the wait so a
    fixture teardown doesn't sit on the default 300s timeout."""
    from backend.config import settings

    monkeypatch.setattr(settings, "mfa_timeout_seconds", 2)


@pytest.fixture
def fake_playwright(monkeypatch) -> None:
    """Replace runner.start/shutdown/new_context with no-Chromium stubs."""

    async def _start():
        return None

    async def _shutdown():
        return None

    @asynccontextmanager
    async def _new_context(storage_state=None):
        ctx = AsyncMock()
        ctx.cookies = AsyncMock(return_value=[])
        ctx.storage_state = AsyncMock(return_value={"cookies": [], "origins": []})

        # new_page returns an awaitable that resolves to a fake page
        fake_page = AsyncMock()
        fake_page.is_closed = lambda: False
        fake_page.close = AsyncMock()
        ctx.new_page = AsyncMock(return_value=fake_page)
        yield ctx

    monkeypatch.setattr(playwright_runner.runner, "start", _start)
    monkeypatch.setattr(playwright_runner.runner, "shutdown", _shutdown)
    monkeypatch.setattr(playwright_runner.runner, "new_context", _new_context)


@pytest.fixture
def mock_carrier(monkeypatch) -> None:
    """Force the registry to use MockFlow for the Geico slot."""
    monkeypatch.setenv("CARRIER_MOCK", "1")
    # rebuild the registry with the env applied
    from backend.carriers import registry

    registry._FLOWS = registry._build()
