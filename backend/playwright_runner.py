from __future__ import annotations

import asyncio
import logging
import socket
import subprocess
from pathlib import Path
from contextlib import asynccontextmanager

import httpx
from playwright.async_api import (
    Browser,
    BrowserContext,
    Playwright,
    async_playwright,
)

from .config import settings

log = logging.getLogger(__name__)

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

    async def _ensure_default_browser(self) -> None:
        await self.start()
        if self._browser is None:
            assert self._pw is not None
            self._browser = await self._pw.chromium.launch(
                headless=settings.playwright_headless,
                slow_mo=settings.playwright_slowmo_ms,
                args=["--disable-blink-features=AutomationControlled"],
            )

    async def shutdown(self) -> None:
        if self._browser:
            try:
                await self._browser.close()
            except Exception as e:  # noqa: BLE001
                log.warning("Playwright browser close failed: %s", e)
            finally:
                self._browser = None
        if self._pw:
            try:
                await self._pw.stop()
            except Exception as e:  # noqa: BLE001
                log.warning("Playwright shutdown failed: %s", e)
            finally:
                self._pw = None

    @asynccontextmanager
    async def new_context(self, storage_state: dict | None = None, **overrides):
        launch_chrome_cdp = overrides.pop("_launch_chrome_cdp", False)
        chrome_profile_dir = overrides.pop("_chrome_profile_dir", None)
        if launch_chrome_cdp:
            async with self._new_chrome_cdp_context(
                chrome_profile_dir=chrome_profile_dir,
                storage_state=storage_state,
            ) as ctx:
                yield ctx
            return

        await self._ensure_default_browser()
        assert self._browser is not None
        options = {
            "storage_state": storage_state,
            "user_agent": USER_AGENT,
            "viewport": {"width": 1280, "height": 800},
        }
        options.update(overrides)
        ctx = await self._browser.new_context(**options)  # type: ignore[arg-type]
        try:
            yield ctx
        finally:
            await ctx.close()

    @asynccontextmanager
    async def _new_chrome_cdp_context(
        self, chrome_profile_dir: str | None, storage_state: dict | None
    ):
        await self.start()
        assert self._pw is not None

        chrome = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
        profile_dir = Path(chrome_profile_dir or "storage/browser-profiles/chrome")
        profile_dir.mkdir(parents=True, exist_ok=True)
        port = _free_port()
        proc = subprocess.Popen(
            [
                chrome,
                f"--remote-debugging-port={port}",
                f"--user-data-dir={profile_dir}",
                "--no-first-run",
                "--no-default-browser-check",
                "about:blank",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        browser = None
        try:
            await _wait_for_port(port)
            browser = await self._pw.chromium.connect_over_cdp(
                f"http://127.0.0.1:{port}"
            )
            ctx = browser.contexts[0]
            if storage_state and storage_state.get("cookies"):
                await ctx.add_cookies(storage_state["cookies"])
            yield ctx
        finally:
            if browser is not None:
                try:
                    await browser.close()
                except Exception as e:  # noqa: BLE001
                    log.warning("Chrome CDP close failed: %s", e)
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()


runner = PlaywrightRunner()


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


async def _wait_for_port(port: int) -> None:
    deadline = asyncio.get_running_loop().time() + 10
    while True:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return
        except OSError:
            if asyncio.get_running_loop().time() > deadline:
                raise RuntimeError(f"Chrome CDP port {port} did not open")
            await asyncio.sleep(0.1)


async def http_from_context(
    ctx: BrowserContext, user_agent: str | None = USER_AGENT
) -> httpx.AsyncClient:
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
        headers={"User-Agent": user_agent or USER_AGENT},
        follow_redirects=True,
        timeout=15.0,
    )
