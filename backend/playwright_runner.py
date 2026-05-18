from __future__ import annotations

import asyncio
import logging
import os
import signal
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from contextlib import asynccontextmanager
from collections.abc import Awaitable, Callable

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
            headless = _default_browser_headless()
            self._browser = await self._pw.chromium.launch(
                headless=headless,
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
                context_options=overrides,
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
        self,
        chrome_profile_dir: str | None,
        storage_state: dict | None,
        context_options: dict | None = None,
        before_connect: Callable[[int, Path], Awaitable[None]] | None = None,
    ):
        await self.start()
        assert self._pw is not None

        context_options = context_options or {}
        initial_url = context_options.pop("_initial_url", "about:blank")
        chrome = _chrome_binary()
        profile_dir = Path(chrome_profile_dir or "storage/browser-profiles/chrome")
        profile_dir.mkdir(parents=True, exist_ok=True)
        _clear_stale_profile_locks(profile_dir)
        port = _free_port()
        viewport = context_options.get("viewport") or {}
        proxy = context_options.get("proxy") or {}
        proxy_server = proxy.get("server") or os.environ.get("CHROME_PROXY_SERVER")
        chrome_args = [
            f"--remote-debugging-port={port}",
            f"--user-data-dir={profile_dir}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-dev-shm-usage",
            "--disable-blink-features=AutomationControlled",
        ]
        if user_agent := context_options.get("user_agent"):
            chrome_args.append(f"--user-agent={user_agent}")
        if locale := context_options.get("locale"):
            chrome_args.append(f"--lang={locale}")
        if viewport.get("width") and viewport.get("height"):
            chrome_args.append(f"--window-size={viewport['width']},{viewport['height']}")
        if proxy_server:
            chrome_args.append(f"--proxy-server={proxy_server}")
        if os.name == "posix" and hasattr(os, "geteuid") and os.geteuid() == 0:
            chrome_args.append("--no-sandbox")
        command = [chrome, *chrome_args, initial_url]
        if not os.environ.get("DISPLAY") and os.name == "posix":
            xvfb = shutil.which("xvfb-run")
            if xvfb:
                command = [xvfb, "-a", *command]
        log_path = Path("/tmp") / f"chrome-cdp-{port}.log"
        log_file = log_path.open("wb")
        proc = subprocess.Popen(
            command,
            stdout=subprocess.DEVNULL,
            stderr=log_file,
            start_new_session=(os.name == "posix"),
            env=_chrome_env(context_options),
        )
        browser = None
        try:
            try:
                await _wait_for_port(port)
            except RuntimeError as e:
                try:
                    log_file.flush()
                    chrome_log = log_path.read_text(errors="ignore").strip()
                except Exception:
                    chrome_log = ""
                detail = f"{e}. Chrome stderr: {chrome_log[-1000:]}"
                raise RuntimeError(detail) from e
            if before_connect is not None:
                await before_connect(port, profile_dir)
            browser = await self._pw.chromium.connect_over_cdp(
                f"http://127.0.0.1:{port}"
            )
            ctx = browser.contexts[0]
            if headers := context_options.get("extra_http_headers"):
                await ctx.set_extra_http_headers(headers)
            if init_script := context_options.get("_init_script"):
                await ctx.add_init_script(init_script)
            if storage_state and storage_state.get("cookies"):
                await ctx.add_cookies(storage_state["cookies"])
            yield ctx
        finally:
            try:
                log_file.close()
            except Exception:
                pass
            if browser is not None:
                try:
                    await browser.close()
                except Exception as e:  # noqa: BLE001
                    log.warning("Chrome CDP close failed: %s", e)
            _terminate_process_group(proc)
            _clear_stale_profile_locks(profile_dir, wait_for_processes=True)

    @asynccontextmanager
    async def new_chrome_cdp_context_after(
        self,
        *,
        chrome_profile_dir: str | None,
        storage_state: dict | None,
        context_options: dict | None,
        before_connect: Callable[[int, Path], Awaitable[None]],
    ):
        async with self._new_chrome_cdp_context(
            chrome_profile_dir=chrome_profile_dir,
            storage_state=storage_state,
            context_options=context_options,
            before_connect=before_connect,
        ) as ctx:
            yield ctx


runner = PlaywrightRunner()


def _default_browser_headless() -> bool:
    if settings.playwright_headless:
        return True
    if os.name == "posix" and sys.platform != "darwin" and not os.environ.get("DISPLAY"):
        log.warning(
            "PLAYWRIGHT_HEADLESS=false but DISPLAY is unset; "
            "forcing default browser to headless"
        )
        return True
    return False


def _chrome_binary() -> str:
    configured = os.environ.get("CHROME_BINARY")
    if configured:
        return configured

    candidates = [
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/usr/bin/google-chrome-stable",
        "/usr/bin/google-chrome",
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
    ]
    for candidate in candidates:
        if Path(candidate).exists():
            return candidate

    for name in ("google-chrome-stable", "google-chrome", "chromium", "chromium-browser"):
        found = shutil.which(name)
        if found:
            return found

    raise RuntimeError("Google Chrome or Chromium binary not found")


def _chrome_env(context_options: dict) -> dict[str, str]:
    env = os.environ.copy()
    if timezone_id := context_options.get("timezone_id"):
        env["TZ"] = timezone_id
    return env


def _terminate_process_group(proc: subprocess.Popen) -> None:
    """Stop Chrome launched for CDP, including xvfb-run child processes."""
    if proc.poll() is not None:
        return
    if os.name == "posix":
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            proc.wait(timeout=5)
            return
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                return
            proc.wait(timeout=5)
            return

    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


def _clear_stale_profile_locks(
    profile_dir: Path, wait_for_processes: bool = False
) -> None:
    """Remove Chrome profile lock files left by a previous crashed container."""
    deadline = time.monotonic() + 2.0
    while _profile_has_live_chrome(profile_dir):
        if not wait_for_processes or time.monotonic() >= deadline:
            return
        time.sleep(0.1)
    for name in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
        try:
            (profile_dir / name).unlink()
        except FileNotFoundError:
            pass
        except OSError as e:
            log.warning("Could not remove stale Chrome profile lock %s: %s", name, e)


def _profile_has_live_chrome(profile_dir: Path) -> bool:
    if os.name != "posix" or not Path("/proc").exists():
        return False
    candidates = {str(profile_dir), str(profile_dir.resolve())}
    try:
        proc_dirs = list(Path("/proc").iterdir())
    except OSError:
        return False
    for proc_dir in proc_dirs:
        if not proc_dir.name.isdigit():
            continue
        try:
            cmdline = (proc_dir / "cmdline").read_bytes()
        except OSError:
            continue
        if not cmdline:
            continue
        command = cmdline.replace(b"\x00", b" ").decode(errors="ignore")
        if "chrome" not in command.lower():
            continue
        if any(f"--user-data-dir={candidate}" in command for candidate in candidates):
            return True
    return False


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
    if user_agent is None:
        user_agent = await _navigator_user_agent(ctx)
    return httpx.AsyncClient(
        cookies=jar,
        headers={"User-Agent": user_agent or USER_AGENT},
        follow_redirects=True,
        timeout=15.0,
    )


async def _navigator_user_agent(ctx: BrowserContext) -> str | None:
    page = ctx.pages[0] if ctx.pages else None
    owned_page = False
    if page is None:
        page = await ctx.new_page()
        owned_page = True
    try:
        return await page.evaluate("navigator.userAgent")
    except Exception:
        return None
    finally:
        if owned_page and page is not None and not page.is_closed():
            await page.close()
