from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import httpx
from playwright.async_api import (
    Browser,
    BrowserContext,
    Playwright,
    async_playwright,
)

from .config import settings

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "Version/17.6 Safari/605.1.15"
)


class PlaywrightRunner:
    """Lazy, shared Playwright + Chromium browser. One per process."""

    def __init__(self) -> None:
        self._pw: Playwright | None = None
        self._browser: Browser | None = None
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        async with self._lock:
            if self._pw is None:
                self._pw = await async_playwright().start()
                self._browser = await self._pw.chromium.launch(
                    headless=settings.playwright_headless,
                    slow_mo=settings.playwright_slowmo_ms,
                )

    async def shutdown(self) -> None:
        if self._browser:
            await self._browser.close()
            self._browser = None
        if self._pw:
            await self._pw.stop()
            self._pw = None

    @asynccontextmanager
    async def new_context(self, storage_state: dict | None = None):
        await self.start()
        assert self._browser is not None
        ctx = await self._browser.new_context(
            storage_state=storage_state,  # type: ignore[arg-type]
            user_agent=USER_AGENT,
            viewport={"width": 1280, "height": 800},
        )
        try:
            yield ctx
        finally:
            await ctx.close()


runner = PlaywrightRunner()


async def http_from_context(ctx: BrowserContext) -> httpx.AsyncClient:
    """Build an httpx.AsyncClient that shares the BrowserContext's cookies.

    After Playwright finishes auth, lift cookies into httpx for fast parallel
    document fetches without DOM overhead.
    """
    cookies = await ctx.cookies()
    jar = httpx.Cookies()
    for c in cookies:
        jar.set(
            c["name"],
            c["value"],
            domain=c.get("domain", ""),
            path=c.get("path", "/"),
        )
    return httpx.AsyncClient(
        cookies=jar,
        headers={"User-Agent": USER_AGENT},
        follow_redirects=True,
        timeout=15.0,
    )
