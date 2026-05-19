"""Spawn long-lived chromium processes for auto-repair to attach to via CDP.

When the orchestrator hits ERROR, the failed Playwright context has already
been closed by `async with` cleanup before `auto_repair` runs. To give claude
a live-attachable browser, we spawn a fresh chromium subprocess with
`--remote-debugging-port`, pre-populate cookies from the saved storage_state,
and hand the CDP endpoint URL to claude via `cdp_endpoint.txt`.

The chromium subprocess stays alive until the repair completes (STATUS file
appears) or the wall timeout fires, at which point `cleanup(carrier)` kills
the process. `cleanup_all()` is called from the FastAPI lifespan teardown
so app shutdown doesn't leak chromium.
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from playwright.async_api import async_playwright

from .playwright_runner import _chrome_binary

log = logging.getLogger(__name__)


@dataclass
class RepairBrowser:
    session_id: str
    carrier: str
    proc: subprocess.Popen
    profile_dir: Path
    cdp_endpoint: str
    started_at: float


_repair_browsers: dict[str, RepairBrowser] = {}
_lock = asyncio.Lock()


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


async def _wait_for_port(port: int, timeout: float = 10.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return
        except OSError:
            if asyncio.get_running_loop().time() > deadline:
                raise RuntimeError(f"chrome CDP port {port} did not open")
            await asyncio.sleep(0.1)


def _terminate(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    try:
        if os.name == "posix":
            os.killpg(proc.pid, 15)
            try:
                proc.wait(timeout=5)
                return
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, 9)
                proc.wait(timeout=5)
                return
    except Exception as e:  # noqa: BLE001
        log.warning("error in group-terminate, falling back: %s", e)
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


async def spawn(
    session_id: str,
    carrier: str,
    storage_state: dict,
    initial_url: str | None = None,
) -> str:
    """Spawn a repair chromium with cookies pre-loaded.

    Returns the CDP endpoint URL. Reuses an existing browser for the same
    carrier if one is already alive.
    """
    async with _lock:
        existing = _repair_browsers.get(carrier)
        if existing and existing.proc.poll() is None:
            log.info(
                "reusing existing repair browser for %s at %s",
                carrier,
                existing.cdp_endpoint,
            )
            return existing.cdp_endpoint

    profile_dir = Path("storage/repair") / session_id / "chrome-profile"
    profile_dir.mkdir(parents=True, exist_ok=True)
    port = _free_port()
    cdp_endpoint = f"http://127.0.0.1:{port}"
    chrome = _chrome_binary()

    args = [
        chrome,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile_dir}",
        "--headless=new",
        "--no-sandbox",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-dev-shm-usage",
        "--disable-blink-features=AutomationControlled",
        initial_url or "about:blank",
    ]
    proc = subprocess.Popen(
        args,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=(os.name == "posix"),
    )

    try:
        await _wait_for_port(port, timeout=10.0)

        async with async_playwright() as pw:
            browser = await pw.chromium.connect_over_cdp(cdp_endpoint)
            try:
                if not browser.contexts:
                    raise RuntimeError("no context on CDP browser")
                ctx = browser.contexts[0]
                cookies = storage_state.get("cookies") or []
                if cookies:
                    await ctx.add_cookies(cookies)
                if initial_url:
                    page = ctx.pages[0] if ctx.pages else await ctx.new_page()
                    try:
                        await page.goto(
                            initial_url,
                            wait_until="domcontentloaded",
                            timeout=15_000,
                        )
                    except Exception as e:  # noqa: BLE001
                        log.warning(
                            "repair browser nav to %s failed: %s",
                            initial_url,
                            e,
                        )
            finally:
                await browser.close()

        rb = RepairBrowser(
            session_id=session_id,
            carrier=carrier,
            proc=proc,
            profile_dir=profile_dir,
            cdp_endpoint=cdp_endpoint,
            started_at=time.time(),
        )
        async with _lock:
            _repair_browsers[carrier] = rb
        log.info(
            "spawned repair browser for %s session %s at %s",
            carrier,
            session_id,
            cdp_endpoint,
        )
        return cdp_endpoint
    except Exception:
        _terminate(proc)
        raise


async def cleanup(carrier: str) -> None:
    async with _lock:
        rb = _repair_browsers.pop(carrier, None)
    if rb is None:
        return
    log.info(
        "cleaning up repair browser for %s session %s",
        carrier,
        rb.session_id,
    )
    _terminate(rb.proc)


async def cleanup_all() -> None:
    async with _lock:
        carriers = list(_repair_browsers.keys())
    for carrier in carriers:
        await cleanup(carrier)


def active_browsers_snapshot() -> dict[str, dict]:
    return {
        carrier: {
            "session_id": rb.session_id,
            "cdp_endpoint": rb.cdp_endpoint,
            "started_at": rb.started_at,
            "alive": rb.proc.poll() is None,
        }
        for carrier, rb in _repair_browsers.items()
    }
