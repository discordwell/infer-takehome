"""Shared test fixtures.

We patch the Playwright runner so integration tests don't launch real Chromium —
the MockFlow ignores the page/context arguments, so an AsyncMock suffices.
"""

from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from backend import playwright_runner, result_store, storage


@pytest.fixture
def tmp_session_dir(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setattr(storage, "STORAGE_DIR", tmp_path)
    return tmp_path


@pytest.fixture(autouse=True)
def tmp_result_store_dir(tmp_path: Path, monkeypatch) -> Path:
    path = tmp_path / "results"
    monkeypatch.setattr(result_store, "RESULTS_DIR", path)
    return path


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
