from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import shutil
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator, Awaitable, Callable

import httpx
from websockets.asyncio.client import connect as websocket_connect
from playwright.async_api import BrowserContext, Locator, Page

from ..config import settings
from ..models import Carrier, Document
from .base import CarrierFlow

log = logging.getLogger(__name__)

LOGIN_URL = "https://www.usaa.com/my/logon"
DASHBOARD_URL_CANDIDATES = (
    "https://www.usaa.com/my/usaa",
    "https://www.usaa.com/my/accounts",
    "https://www.usaa.com/",
)
DOCS_URL_CANDIDATES = (
    "https://www.usaa.com/my/auto-insurance/",
    "https://www.usaa.com/my/auto-insurance",
    "https://www.usaa.com/inet/ent_edde/ViewMyDocuments",
    "https://www.usaa.com/inet/gas_pc_pas/GyMemberAutoHistoryServlet",
    (
        "https://www.usaa.com/inet/gas_pc_pas/GyMemberAutoIdServlet"
        "?action=INIT&proofOfInsuranceType=IDCARD"
    ),
    "https://www.usaa.com/my/documents",
    "https://www.usaa.com/my/documents?akredirect=true",
    "https://www.usaa.com/inet/wc/document_center",
    "https://www.usaa.com/my/insurance",
    "https://www.usaa.com/inet/wc/insurance_auto_main",
)
DEBUG_DIR = Path("/tmp")
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
USAA_CHROME_PROFILE_DIR = (
    PROJECT_ROOT / "storage" / "browser-profiles" / "usaa-chrome"
)
MFA_CODE_INPUT_SELECTOR = (
    "input[autocomplete='one-time-code']:visible, "
    "input[inputmode='numeric']:visible, "
    "input[name*='code' i]:visible, "
    "input[id*='code' i]:visible"
)

STEALTH_INIT_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5].map(() => ({})) });
Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
Object.defineProperty(navigator, 'platform', { get: () => 'MacIntel' });
window.chrome = { runtime: {} };
const origQuery = navigator.permissions ? navigator.permissions.query : null;
if (origQuery) {
  navigator.permissions.query = (params) =>
    params.name === 'notifications'
      ? Promise.resolve({ state: Notification.permission })
      : origQuery(params);
}
"""


class UsaaFlow(CarrierFlow):
    """USAA portal flow.

    Local inspection showed USAA's Akamai edge fails plain headless Chromium
    with ERR_HTTP2_PROTOCOL_ERROR, while headed Chromium reaches the login
    form. This flow uses a Chrome-like context and dumps artifacts whenever
    the portal shape changes.
    """

    carrier = Carrier.USAA

    def __init__(self) -> None:
        self._timing_origin: float | None = None
        self._timings: list[tuple[str, float]] = []
        self._documents_progress_callback: (
            Callable[[list[Document], dict[str, bytes]], Awaitable[None]] | None
        ) = None

    def set_documents_progress_callback(
        self,
        callback: Callable[[list[Document], dict[str, bytes]], Awaitable[None]] | None,
    ) -> None:
        self._documents_progress_callback = callback

    def reset_timings(self) -> None:
        self._timing_origin = time.perf_counter()
        self._timings = []

    def mark_timing(self, label: str) -> None:
        if self._timing_origin is None:
            self.reset_timings()
        assert self._timing_origin is not None
        elapsed = time.perf_counter() - self._timing_origin
        self._timings.append((label, elapsed))
        log.info("usaa timing: %.3fs %s", elapsed, label)

    def timing_report(self) -> str:
        return ", ".join(
            f"{label}={elapsed:.3f}s" for label, elapsed in self._timings
        )

    def timing_snapshot(self) -> dict[str, int]:
        timings: dict[str, int] = {}
        for label, elapsed in self._timings:
            timings.setdefault(label, int(round(elapsed * 1000)))
        return timings

    def context_options(self) -> dict:
        return {
            "_launch_chrome_cdp": True,
            "_chrome_profile_dir": str(self._chrome_profile_dir()),
            "_init_script": STEALTH_INIT_SCRIPT,
            "viewport": {"width": 1280, "height": 800},
            "locale": "en-US",
            "timezone_id": "America/New_York",
            "extra_http_headers": {
                "Accept-Language": "en-US,en;q=0.9",
                "Accept": (
                    "text/html,application/xhtml+xml,application/xml;q=0.9,"
                    "image/avif,image/webp,*/*;q=0.8"
                ),
            },
        }

    def discard_stale_state(self, username: str) -> None:
        """Move the persistent Chrome profile aside before a fresh USAA login."""
        profile_dir = self._chrome_profile_dir()
        if not profile_dir.exists():
            return

        stale_dir = profile_dir.parent / "stale"
        stale_dir.mkdir(parents=True, exist_ok=True)
        destination = stale_dir / f"{profile_dir.name}-{int(time.time())}"
        try:
            shutil.move(str(profile_dir), str(destination))
            log.info(
                "usaa: moved stale Chrome profile for %s to %s",
                username,
                destination,
            )
        except Exception as e:  # noqa: BLE001
            log.warning("usaa: could not move stale Chrome profile: %s", e)

    @asynccontextmanager
    async def login_context(
        self,
        runner,
        username: str,
        password: str,
        context_options: dict,
    ) -> AsyncIterator[tuple[BrowserContext, Page]]:
        if self._login_driver() != "os_browser":
            async with runner.new_context(storage_state=None, **context_options) as ctx:
                page = await ctx.new_page()
                await self.login(page, username, password)
                yield ctx, page
            return

        options = dict(context_options)
        options.pop("_launch_chrome_cdp", None)
        profile_dir = options.pop("_chrome_profile_dir", str(self._chrome_profile_dir()))
        options["_initial_url"] = LOGIN_URL

        async def before_connect(port: int, launched_profile_dir: Path) -> None:
            await self._os_browser_login(username, password, launched_profile_dir, port)

        async with runner.new_chrome_cdp_context_after(
            chrome_profile_dir=profile_dir,
            storage_state=None,
            context_options=options,
            before_connect=before_connect,
        ) as ctx:
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            await page.wait_for_timeout(500)
            if await self._page_has_unavailable_block(page):
                await self._dump_debug(page, "os-login-blocked")
                raise RuntimeError("USAA login blocked after password submit")
            yield ctx, page

    def _login_driver(self) -> str:
        driver = settings.usaa_login_driver.strip().lower()
        if driver not in {"os_browser", "playwright"}:
            raise RuntimeError(
                "USAA_LOGIN_DRIVER must be either 'os_browser' or 'playwright'"
            )
        return driver

    def _chrome_profile_dir(self) -> Path:
        if self._login_driver() == "os_browser":
            configured = Path(settings.usaa_os_browser_profile_dir).expanduser()
            if configured.is_absolute():
                return configured
            return PROJECT_ROOT / configured
        return USAA_CHROME_PROFILE_DIR

    async def _os_browser_login(
        self, username: str, password: str, profile_dir: Path, port: int
    ) -> None:
        if sys.platform != "darwin":
            raise RuntimeError("USAA OS browser login requires macOS local worker")

        log.info(
            "usaa: OS-browser login starting profile=%s timeout=%ss",
            profile_dir,
            settings.usaa_os_login_timeout_seconds,
        )
        await self._activate_chrome(port)
        await self._wait_for_chrome_js(
            self._selector_exists_js("input[name='memberId'], input[type='text']"),
            timeout_ms=30000,
            port=port,
        )

        await self._focus_chrome_selector(
            "input[name='memberId'], input[type='text']", port
        )
        await self._replace_focused_text(username)
        await self._click_chrome_selector("#next-button, button[type='submit']", port)
        await self._dump_os_login_debug("after-username-submit", port)

        await self._wait_for_chrome_js(
            self._selector_exists_js("input[name='password'], input[type='password']"),
            timeout_ms=45000,
            port=port,
        )
        await self._focus_chrome_selector(
            "input[name='password'], input[type='password']", port
        )
        await self._replace_focused_text(password)
        await self._dump_os_login_debug("after-password-fill", port)

        await self._click_chrome_selector("#next-button, button[type='submit']", port)
        await self._wait_for_os_login_landing(port)
        await self._dump_os_login_debug("after-password-submit", port)

    async def _activate_chrome(self, port: int) -> None:
        try:
            await self._chrome_cdp_command(
                "Page.bringToFront", timeout_ms=3000, port=port
            )
        except Exception as e:
            log.debug("usaa: CDP Page.bringToFront failed before OS input: %s", e)

        last_error: Exception | None = None
        scripts = (
            'tell application "Google Chrome" to activate',
            """
            tell application "System Events"
                set frontmost of first process whose name is "Google Chrome" to true
            end tell
            """,
        )
        for _ in range(8):
            for script in scripts:
                try:
                    await self._osascript(script, timeout_ms=3000)
                    return
                except Exception as e:
                    last_error = e
            await asyncio.sleep(0.25)
        assert last_error is not None
        raise last_error

    async def _focus_chrome_selector(self, selector: str, port: int) -> None:
        selector_json = json.dumps(selector)
        focused = await self._chrome_js(
            f"""
            (() => {{
                const el = document.querySelector({selector_json});
                if (!el) return false;
                el.focus();
                if (el.select) el.select();
                return document.activeElement === el;
            }})()
            """,
            timeout_ms=5000,
            port=port,
        )
        if focused.strip().lower() != "true":
            raise RuntimeError(f"USAA OS browser could not focus selector: {selector}")

    async def _click_chrome_selector(self, selector: str, port: int) -> None:
        selector_json = json.dumps(selector)
        raw = await self._chrome_js(
            f"""
            (() => {{
                const el = document.querySelector({selector_json});
                if (!el) return null;
                el.scrollIntoView({{ block: 'center', inline: 'center' }});
                el.focus();
                const rect = el.getBoundingClientRect();
                const chromeLeft =
                    window.screenX + ((window.outerWidth - window.innerWidth) / 2);
                const chromeTop =
                    window.screenY + (window.outerHeight - window.innerHeight);
                return JSON.stringify({{
                    x: Math.round(chromeLeft + rect.left + rect.width / 2),
                    y: Math.round(chromeTop + rect.top + rect.height / 2),
                }});
            }})()
            """,
            timeout_ms=5000,
            port=port,
        )
        if not raw:
            raise RuntimeError(f"USAA OS browser could not locate selector: {selector}")
        point = json.loads(raw)
        await self._activate_chrome(port)
        if shutil.which("cliclick"):
            await self._cliclick(f"c:{int(point['x'])},{int(point['y'])}")
        else:
            await self._press_return()

    async def _replace_focused_text(self, value: str) -> None:
        await self._osascript(
            f"""
            tell application "System Events"
                keystroke "a" using command down
                keystroke {self._applescript_string(value)}
            end tell
            """,
            timeout_ms=10000,
        )

    async def _press_return(self) -> None:
        await self._osascript(
            """
            tell application "System Events"
                key code 36
            end tell
            """,
            timeout_ms=5000,
        )

    async def _wait_for_os_login_landing(self, port: int) -> None:
        deadline = time.perf_counter() + settings.usaa_os_login_timeout_seconds
        while time.perf_counter() < deadline:
            try:
                state = await self._chrome_js(
                    """
                    (() => {
                        const body = (document.body && document.body.innerText || '').toLowerCase();
                        const url = location.href.toLowerCase();
                        const hasCode = !!document.querySelector(
                            "input[autocomplete='one-time-code'], input[inputmode='numeric'], input[name*='code' i], input[id*='code' i]"
                        );
                        return JSON.stringify({ body, url, hasCode });
                    })()
                    """,
                    timeout_ms=3000,
                    port=port,
                )
                parsed = json.loads(state)
                body = parsed.get("body", "")
                url = parsed.get("url", "")
                if self._is_unavailable_block_text(body):
                    return
                if parsed.get("hasCode") or any(
                    phrase in body
                    for phrase in (
                        "verification code",
                        "security code",
                        "one-time code",
                        "verify your identity",
                    )
                ):
                    return
                if "logon" not in url and any(
                    phrase in body
                    for phrase in ("log off", "sign out", "accounts", "policies")
                ):
                    return
            except Exception as e:
                log.debug("usaa: OS-browser landing wait check failed: %s", e)
            await asyncio.sleep(0.5)
        try:
            state = await self._chrome_js(
                """
                (() => {
                    const body = (document.body && document.body.innerText || '').toLowerCase();
                    return JSON.stringify({
                        url: location.href,
                        hasPassword: !!document.querySelector("input[name='password'], input[type='password']"),
                        body: body.slice(0, 400),
                    });
                })()
                """,
                timeout_ms=3000,
                port=port,
            )
            parsed = json.loads(state)
            if parsed.get("hasPassword"):
                raise RuntimeError("USAA OS browser login did not leave password form")
            raise RuntimeError(
                "USAA OS browser login timed out after password submit: "
                f"{parsed.get('url', '')}"
            )
        except RuntimeError:
            raise
        except Exception as e:
            raise RuntimeError(
                "USAA OS browser login timed out after password submit"
            ) from e

    async def _dump_os_login_debug(self, label: str, port: int) -> None:
        png = DEBUG_DIR / f"usaa-os-login-{label}.png"
        html = DEBUG_DIR / f"usaa-os-login-{label}.html"
        try:
            proc = await asyncio.create_subprocess_exec(
                "screencapture",
                "-x",
                str(png),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            try:
                await asyncio.wait_for(proc.wait(), timeout=3)
            except asyncio.TimeoutError:
                proc.kill()
        except Exception:
            pass
        try:
            outer_html = await self._chrome_js(
                "document.documentElement.outerHTML", timeout_ms=3000, port=port
            )
            html.write_text(self._sanitize_debug_html(outer_html))
        except Exception:
            pass
        log.info("usaa: OS-browser login debug -> %s, %s", png, html)

    async def _wait_for_chrome_js(
        self, script: str, timeout_ms: int, port: int
    ) -> None:
        deadline = time.perf_counter() + (timeout_ms / 1000)
        last_error: Exception | None = None
        while time.perf_counter() < deadline:
            try:
                result = await self._chrome_js(script, timeout_ms=3000, port=port)
                if result.strip().lower() == "true":
                    return
            except Exception as e:
                last_error = e
            await asyncio.sleep(0.25)
        message = "USAA OS browser login timed out waiting for page readiness"
        if last_error is not None:
            message += f": {last_error}"
        raise RuntimeError(message)

    async def _chrome_cdp_command(
        self,
        method: str,
        *,
        timeout_ms: int,
        port: int,
        params: dict | None = None,
    ) -> dict:
        ws_url = await self._cdp_page_ws_url(port, timeout_ms)
        message = {
            "id": 1,
            "method": method,
            "params": params or {},
        }
        async with websocket_connect(
            ws_url,
            open_timeout=timeout_ms / 1000,
            close_timeout=1,
        ) as ws:
            await ws.send(json.dumps(message))
            deadline = time.perf_counter() + (timeout_ms / 1000)
            while time.perf_counter() < deadline:
                raw = await asyncio.wait_for(
                    ws.recv(), timeout=max(0.1, deadline - time.perf_counter())
                )
                payload = json.loads(raw)
                if payload.get("id") != 1:
                    continue
                if "error" in payload:
                    raise RuntimeError(f"Chrome CDP command failed: {payload['error']}")
                return payload.get("result", {})
        raise RuntimeError("Chrome CDP command did not return a response")

    async def _chrome_js(self, script: str, timeout_ms: int, port: int) -> str:
        result = await self._chrome_cdp_command(
            "Runtime.evaluate",
            timeout_ms=timeout_ms,
            port=port,
            params={
                "expression": script,
                "awaitPromise": True,
                "returnByValue": True,
            },
        )
        eval_result = result.get("result", {})
        if "exceptionDetails" in result:
            raise RuntimeError(f"Chrome JS failed: {result['exceptionDetails']}")
        value = eval_result.get("value")
        if value is None:
            return ""
        if isinstance(value, str):
            return value
        return json.dumps(value)

    async def _cliclick(self, command: str) -> None:
        proc = await asyncio.create_subprocess_exec(
            "cliclick",
            "-r",
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            detail = stderr.decode(errors="ignore").strip()
            if not detail:
                detail = stdout.decode(errors="ignore").strip()
            raise RuntimeError(f"cliclick failed: {detail}")

    async def _cdp_page_ws_url(self, port: int, timeout_ms: int) -> str:
        async with httpx.AsyncClient(timeout=timeout_ms / 1000) as client:
            resp = await client.get(f"http://127.0.0.1:{port}/json")
            resp.raise_for_status()
            targets = resp.json()
        for target in targets:
            if target.get("type") == "page" and target.get("webSocketDebuggerUrl"):
                return target["webSocketDebuggerUrl"]
        raise RuntimeError("Chrome CDP page target not found")

    async def _osascript(self, script: str, timeout_ms: int) -> str:
        proc = await asyncio.create_subprocess_exec(
            "osascript",
            "-",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(script.encode()), timeout=timeout_ms / 1000
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise RuntimeError("AppleScript timed out")
        if proc.returncode != 0:
            detail = stderr.decode(errors="ignore").strip()
            if "not authorized" in detail.lower() or "not allowed" in detail.lower():
                detail += (
                    " (grant Accessibility permission to the terminal/Codex app "
                    "running the local worker)"
                )
            raise RuntimeError(f"AppleScript failed: {detail}")
        return stdout.decode(errors="ignore").strip()

    @staticmethod
    def _selector_exists_js(selector: str) -> str:
        return f"!!document.querySelector({json.dumps(selector)})"

    @staticmethod
    def _applescript_string(value: str) -> str:
        return json.dumps(value)

    async def login(self, page: Page, username: str, password: str) -> None:
        await self._prepare_page(page)
        log.info("usaa: navigating to login URL")
        try:
            await page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=30000)
        except Exception as e:
            await self._dump_debug(page, "login-navigation-failure")
            raise RuntimeError(
                "USAA login page did not load. Use headed Chromium "
                "(PLAYWRIGHT_HEADLESS=false) for this carrier."
            ) from e

        await page.wait_for_timeout(750)
        if await self._looks_blocked(page):
            await self._dump_debug(page, "akamai-block")
            raise RuntimeError("USAA appears to be blocked by the bot manager")

        try:
            user_field = await self._first_present(
                page.locator("input[name='memberId']:visible").first,
                page.get_by_label(re.compile(r"Online ID|Member ID", re.I)).first,
                page.locator("input[type='text']:visible").first,
                timeout_ms=12000,
            )
            await self._slow_fill(user_field, username)

            next_button = await self._first_present(
                page.locator("#next-button:visible").first,
                page.get_by_role("button", name=re.compile(r"^\s*Next\s*$", re.I)).first,
                page.locator("button[type='submit']:visible").first,
            )
            await next_button.click()
            await self._settle(page, delay_ms=1000, networkidle_timeout_ms=3000)

            pw_field = await self._wait_for_password_field(page)
            await self._slow_fill(pw_field, password)

            submit = await self._first_present(
                page.locator("#next-button:visible").first,
                page.get_by_role(
                    "button", name=re.compile(r"^\s*(Next|Log On|Log In|Submit)\s*$", re.I)
                ).first,
                page.locator("button[type='submit']:visible").first,
            )
            await submit.click()
        except Exception as e:
            await self._dump_debug(page, "login-form-failure")
            raise RuntimeError(f"USAA login form interaction failed: {e}") from e

        await self._settle(page, delay_ms=500, networkidle_timeout_ms=3000)
        body = (await self._body_text(page)).lower()
        if any(
            phrase in body
            for phrase in (
                "password you entered doesn't match",
                "online id or password is incorrect",
                "credentials are incorrect",
                "cannot verify your information",
            )
        ) and "logon" in page.url.lower():
            await self._dump_debug(page, "login-rejected")
            raise RuntimeError("USAA login rejected - check username/password")
        if "logon" in page.url.lower() and await page.locator(
            "input[name='password']:visible, input[type='password']:visible"
        ).count():
            await self._dump_debug(page, "login-still-on-form")
            raise RuntimeError("USAA login did not leave the password form")

    async def mfa_required(self, page: Page) -> bool:
        await self._prefer_email_mfa(page)
        url = page.url.lower()
        if any(k in url for k in ("mfa", "otp", "verify", "security", "challenge")):
            log.info("usaa: MFA detected via URL=%s", url)
            return True
        if await page.locator(MFA_CODE_INPUT_SELECTOR).count() > 0:
            log.info("usaa: MFA detected via code input")
            return True
        body = (await self._body_text(page)).lower()
        if any(
            phrase in body
            for phrase in (
                "verification code",
                "security code",
                "one-time code",
                "enter the code",
                "we sent",
                "verify your identity",
            )
        ):
            log.info("usaa: MFA detected via body text")
            return True
        return False

    async def submit_mfa(self, page: Page, code: str) -> None:
        await self._prefer_email_mfa(page)
        try:
            otp_field = await self._first_present(
                page.locator("input[autocomplete='one-time-code']:visible").first,
                page.locator("input[inputmode='numeric']:visible").first,
                page.locator("input[name*='code' i]:visible").first,
                page.locator("input[id*='code' i]:visible").first,
                timeout_ms=20000,
            )
            await self._slow_fill(otp_field, code)
            self.mark_timing("mfa_code_filled")
            try:
                submit = await self._first_present(
                    page.get_by_role(
                        "button", name=re.compile(r"continue|next|submit|verify", re.I)
                    ).first,
                    page.locator("button[type='submit']:visible").first,
                    timeout_ms=6000,
                )
                await submit.click()
                self.mark_timing("mfa_submit_clicked")
            except Exception:
                await otp_field.press("Enter")
                self.mark_timing("mfa_submit_pressed_enter")
        except Exception as e:
            await self._dump_debug(page, "mfa-failure")
            raise RuntimeError(f"USAA MFA interaction failed: {e}") from e

        await self._wait_after_mfa_submit(page)

    async def is_authenticated(self, page: Page) -> bool:
        await self._prepare_page(page)
        for url in DASHBOARD_URL_CANDIDATES:
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=12000)
                await self._settle(page, delay_ms=500, networkidle_timeout_ms=2500)
            except Exception:
                continue
            current_url = page.url.lower()
            if "logon" in current_url or "login" in current_url:
                continue
            body = (await self._body_text(page)).lower()
            if any(s in body for s in ("log off", "sign out", "accounts", "policies")):
                return True
        return False

    async def fetch_documents(
        self,
        page: Page,
        http: httpx.AsyncClient,
        ctx: BrowserContext,
    ) -> tuple[list[Document], dict[str, bytes]]:
        self.mark_timing("docs_fetch_start")
        await self._prepare_page(page)
        all_docs: list[Document] = []
        all_doc_bytes: dict[str, bytes] = {}
        seen: set[str] = set()
        saw_document_candidates = False

        async def merge(
            docs: list[Document],
            doc_bytes: dict[str, bytes],
        ) -> None:
            had_docs = bool(all_docs)
            self._merge_documents(all_docs, all_doc_bytes, seen, docs, doc_bytes)
            if not had_docs and all_docs:
                self.mark_timing("docs_first_document_ready")

        docs, doc_bytes = await self._fetch_from_document_surface(page, http, ctx)
        await merge(docs, doc_bytes)
        if all_docs:
            return all_docs, all_doc_bytes

        for url in DOCS_URL_CANDIDATES:
            try:
                resp = await page.goto(url, wait_until="domcontentloaded", timeout=15000)
                self.mark_timing("docs_url_loaded")
                if resp:
                    content_type = resp.headers.get("content-type", "application/pdf")
                    if self._looks_like_document_response(resp):
                        try:
                            body = await resp.body()
                            if self._is_document_body(body, content_type):
                                self.mark_timing("doc_pdf_bytes")
                                docs, doc_bytes = self._single_document(
                                    body,
                                    content_type,
                                    self._name_from_headers(
                                        resp.headers, resp.url, "USAA document 1"
                                    ),
                                )
                                await merge(docs, doc_bytes)
                                return all_docs, all_doc_bytes
                        except Exception:
                            pass
                title = (await page.title()).lower()
                if "logon" in page.url.lower() or "login" in page.url.lower():
                    continue
                if "page not found" in title:
                    continue
            except Exception as e:
                log.warning("usaa: docs URL %s failed: %s", url, e)
                continue
            docs, doc_bytes = await self._fetch_from_document_surface(page, http, ctx)
            await merge(docs, doc_bytes)
            if all_docs:
                return all_docs, all_doc_bytes

            candidates = await self._collect_document_links(page)
            if candidates:
                saw_document_candidates = True
                docs, doc_bytes = await self._fetch_document_link_candidates(
                    http, candidates
                )
                await merge(docs, doc_bytes)
                if all_docs:
                    return all_docs, all_doc_bytes

        if all_docs:
            return all_docs, all_doc_bytes

        if not saw_document_candidates:
            await self._dump_debug(page, "docs-no-links")
            raise RuntimeError("USAA: authenticated, but no document links found yet")

        await self._dump_debug(page, "docs-fetch-failed")
        raise RuntimeError("USAA: found document links, but downloads failed")

    async def _fetch_document_link_candidates(
        self,
        http: httpx.AsyncClient,
        candidates: list[tuple[str, str]],
    ) -> tuple[list[Document], dict[str, bytes]]:
        async def fetch(name: str, href: str, idx: int):
            try:
                r = await http.get(href)
                r.raise_for_status()
                body = r.content
                content_type = r.headers.get("content-type", "application/pdf")
                if "text/html" in content_type.lower() or body.lstrip().startswith(
                    b"<!doctype html"
                ):
                    return None
                if "pdf" not in content_type.lower() and not body.startswith(b"%PDF"):
                    return None
                display_name = name.strip() or f"usaa-document-{idx}"
                if (
                    "pdf" in content_type.lower() or body.startswith(b"%PDF")
                ) and not display_name.lower().endswith(".pdf"):
                    display_name += ".pdf"
                doc = Document(
                    id=f"usaa-doc-{idx}",
                    name=display_name,
                    content_type=content_type,
                    size_bytes=len(body),
                )
                return doc, body
            except Exception as e:
                log.warning("usaa: failed to fetch %s: %s", href, e)
                return None

        results = await asyncio.gather(
            *[fetch(name, href, idx) for idx, (name, href) in enumerate(candidates)]
        )
        docs: list[Document] = []
        doc_bytes: dict[str, bytes] = {}
        for result in results:
            if result is None:
                continue
            doc, body = result
            docs.append(doc)
            doc_bytes[doc.id] = body
        return docs, doc_bytes

    async def _fetch_from_document_surface(
        self,
        page: Page,
        http: httpx.AsyncClient,
        ctx: BrowserContext,
    ) -> tuple[list[Document], dict[str, bytes]]:
        all_docs: list[Document] = []
        all_doc_bytes: dict[str, bytes] = {}
        seen: set[str] = set()

        docs, doc_bytes = await self._fetch_document_buttons(page, http, ctx)
        self._merge_documents(all_docs, all_doc_bytes, seen, docs, doc_bytes)

        if await self._open_policy_documents_from_summary(page):
            self.mark_timing("policy_documents_opened")
            docs, doc_bytes = await self._fetch_document_buttons(page, http, ctx)
            self._merge_documents(all_docs, all_doc_bytes, seen, docs, doc_bytes)

        docs, doc_bytes = await self._fetch_named_document_actions(page, http, ctx)
        self._merge_documents(all_docs, all_doc_bytes, seen, docs, doc_bytes)

        if all_docs:
            return all_docs, all_doc_bytes

        return all_docs, all_doc_bytes

    async def _fetch_document_buttons(
        self,
        page: Page,
        http: httpx.AsyncClient,
        ctx: BrowserContext,
    ) -> tuple[list[Document], dict[str, bytes]]:
        buttons = page.locator("button[data-testid^='readDocument-']")
        count = await buttons.count()
        if count == 0:
            try:
                await buttons.first.wait_for(state="visible", timeout=2500)
                self.mark_timing("docs_button_visible")
                count = await buttons.count()
            except Exception:
                return [], {}

        all_docs: list[Document] = []
        all_doc_bytes: dict[str, bytes] = {}
        seen: set[str] = set()
        for idx in range(count):
            await self._close_document_viewer(page)
            button = buttons.nth(idx)
            try:
                name = (
                    await button.inner_text(timeout=2000)
                ).strip() or f"USAA document {idx + 1}"
            except Exception:
                name = f"USAA document {idx + 1}"

            try:
                href = await self._direct_document_href(button)
                if href:
                    direct = await self._fetch_direct_document(http, href, name)
                    if direct is not None:
                        body, content_type, display_name = direct
                        docs, doc_bytes = self._single_document(
                            body, content_type, display_name
                        )
                        self._merge_documents(
                            all_docs, all_doc_bytes, seen, docs, doc_bytes
                        )
                        await self._emit_documents_progress(all_docs, all_doc_bytes)
                        continue

                payload = await self._click_for_first_document(
                    page, http, ctx, button, name
                )
                if payload is not None:
                    body, content_type, display_name = payload
                    docs, doc_bytes = self._single_document(
                        body, content_type, display_name
                    )
                    self._merge_documents(
                        all_docs, all_doc_bytes, seen, docs, doc_bytes
                    )
                    await self._emit_documents_progress(all_docs, all_doc_bytes)
            except Exception as e:
                log.warning("usaa: document button %s failed: %s", idx, e)
        return all_docs, all_doc_bytes

    async def _fetch_named_document_actions(
        self,
        page: Page,
        http: httpx.AsyncClient,
        ctx: BrowserContext,
    ) -> tuple[list[Document], dict[str, bytes]]:
        action_patterns = (
            re.compile(r"Proof of insurance", re.I),
            re.compile(r"^(Auto )?ID card$", re.I),
        )
        all_docs: list[Document] = []
        all_doc_bytes: dict[str, bytes] = {}
        seen: set[str] = set()
        for pattern in action_patterns:
            button = page.get_by_role("button", name=pattern).first
            if await button.count() == 0:
                continue
            try:
                name = (await button.inner_text(timeout=1000)).strip()
            except Exception:
                name = pattern.pattern.strip("^$") or "USAA document 1"
            try:
                payload = await self._click_for_first_document(
                    page, http, ctx, button, name
                )
                if payload is not None:
                    body, content_type, display_name = payload
                    docs, doc_bytes = self._single_document(
                        body, content_type, display_name
                    )
                    self._merge_documents(
                        all_docs, all_doc_bytes, seen, docs, doc_bytes
                    )
                    await self._emit_documents_progress(all_docs, all_doc_bytes)
            except Exception as e:
                log.warning("usaa: document action %s failed: %s", pattern.pattern, e)
        return all_docs, all_doc_bytes

    async def _emit_documents_progress(
        self,
        docs: list[Document],
        doc_bytes: dict[str, bytes],
    ) -> None:
        if not docs or self._documents_progress_callback is None:
            return
        await self._documents_progress_callback(list(docs), dict(doc_bytes))

    async def _click_for_first_document(
        self,
        page: Page,
        http: httpx.AsyncClient,
        ctx: BrowserContext,
        target: Locator,
        name: str,
    ) -> tuple[bytes, str, str] | None:
        response_queue: asyncio.Queue = asyncio.Queue()
        saw_response_headers = False

        def on_response(resp):
            nonlocal saw_response_headers
            if self._looks_like_document_response(resp):
                response_queue.put_nowait(resp)
                if not saw_response_headers:
                    self.mark_timing("doc_pdf_response_headers")
                    saw_response_headers = True

        page.on("response", on_response)
        download_task = asyncio.create_task(page.wait_for_event("download", timeout=7000))
        popup_task = asyncio.create_task(ctx.wait_for_event("page", timeout=7000))
        try:
            await target.click(timeout=5000)
            self.mark_timing("doc_action_clicked")
            payload = await self._first_document_payload(
                page, http, response_queue, download_task, popup_task, name
            )
            if payload is not None:
                await self._close_document_viewer(page)
            return payload
        finally:
            page.remove_listener("response", on_response)
            for task in (download_task, popup_task):
                if not task.done():
                    task.cancel()

    async def _close_document_viewer(self, page: Page) -> None:
        modal = page.locator(
            "[data-testid='document-view-modal'], "
            "iframe[data-testid='document-view-iframe']"
        )
        try:
            if await modal.count() == 0:
                return
        except Exception:
            return

        close_targets = (
            page.locator(
                "[data-testid='document-view-modal'] "
                "button[aria-label*='close' i]"
            ).first,
            page.locator(
                "[data-testid='document-view-modal'] "
                "button[data-testid*='close' i]"
            ).first,
            page.locator(
                "[data-testid='document-view-modal'] "
                "[role='button'][aria-label*='close' i]"
            ).first,
            page.get_by_role(
                "button", name=re.compile(r"close|dismiss|done", re.I)
            ).first,
        )
        for target in close_targets:
            try:
                if await target.count() == 0:
                    continue
                await target.click(timeout=1200)
                break
            except Exception:
                continue
        else:
            try:
                await page.keyboard.press("Escape")
            except Exception:
                return

        try:
            await page.locator("[data-testid='document-view-modal']").first.wait_for(
                state="hidden", timeout=2500
            )
        except Exception:
            try:
                await page.wait_for_timeout(300)
            except Exception:
                pass

    async def _open_policy_documents_from_summary(self, page: Page) -> bool:
        rows = page.locator("li", has_text=re.compile(r"Policy documents", re.I))
        if await rows.count() == 0:
            if not self._is_auto_policy_surface(page):
                return False
            try:
                await rows.first.wait_for(state="visible", timeout=4500)
            except Exception:
                return False
        row = rows.first
        try:
            button = row.get_by_role("button", name=re.compile(r"View", re.I)).first
            await button.click(timeout=4000)
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=5000)
            except Exception:
                pass
            try:
                await page.locator("button[data-testid^='readDocument-']").first.wait_for(
                    state="visible", timeout=5000
                )
            except Exception:
                await page.wait_for_timeout(700)
            return True
        except Exception as e:
            log.warning("usaa: policy documents action failed: %s", e)
            return False

    @staticmethod
    def _is_auto_policy_surface(page: Page) -> bool:
        url = page.url.lower()
        return "auto-insurance" in url or "insurance_auto" in url

    async def _first_document_payload(
        self,
        page: Page,
        http: httpx.AsyncClient,
        response_queue: asyncio.Queue,
        download_task: asyncio.Task,
        popup_task: asyncio.Task,
        name: str,
    ) -> tuple[bytes, str, str] | None:
        async def from_response():
            deadline = time.perf_counter() + 7.0
            while True:
                remaining = deadline - time.perf_counter()
                if remaining <= 0:
                    raise TimeoutError("no document response")
                resp = await asyncio.wait_for(response_queue.get(), timeout=remaining)
                content_type = resp.headers.get("content-type", "application/pdf")
                body = await resp.body()
                if self._is_document_body(body, content_type):
                    self.mark_timing("doc_pdf_bytes")
                    return body, content_type, self._name_from_response(resp, name)

        async def from_download():
            download = await download_task
            path = await download.path()
            if not path:
                raise RuntimeError("download had no local path")
            body = Path(path).read_bytes()
            content_type = "application/pdf"
            if not self._is_document_body(body, content_type):
                raise RuntimeError("download was not a PDF")
            self.mark_timing("doc_pdf_bytes")
            return body, content_type, download.suggested_filename or name

        async def from_popup():
            popup = await popup_task
            try:
                try:
                    await popup.wait_for_load_state("domcontentloaded", timeout=3500)
                except Exception:
                    pass
                body, content_type = await self._extract_document_from_page(popup, http)
                if body is None or not self._is_document_body(body, content_type):
                    raise RuntimeError("popup did not expose a PDF")
                self.mark_timing("doc_pdf_bytes")
                return body, content_type, name
            finally:
                if not popup.is_closed():
                    await popup.close()

        async def from_current_page():
            await page.wait_for_timeout(400)
            body, content_type = await self._extract_document_from_page(page, http)
            if body is None or not self._is_document_body(body, content_type):
                raise RuntimeError("current page did not expose a PDF")
            self.mark_timing("doc_pdf_bytes")
            return body, content_type, name

        tasks = [
            asyncio.create_task(from_response()),
            asyncio.create_task(from_download()),
            asyncio.create_task(from_popup()),
            asyncio.create_task(from_current_page()),
        ]
        try:
            for completed in asyncio.as_completed(tasks, timeout=7.5):
                try:
                    return await completed
                except Exception:
                    continue
        except TimeoutError:
            return None
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
        return None

    async def _direct_document_href(self, button: Locator) -> str | None:
        try:
            return await button.evaluate(
                """el => {
                    const attrs = ['href', 'data-href', 'data-url', 'data-document-url'];
                    const candidates = [el, el.closest('a[href], [data-href], [data-url], [data-document-url]')];
                    for (const node of candidates) {
                        if (!node) continue;
                        for (const attr of attrs) {
                            const value = node.getAttribute(attr);
                            if (value) return new URL(value, window.location.href).href;
                        }
                    }
                    return null;
                }"""
            )
        except Exception:
            return None

    async def _fetch_direct_document(
        self, http: httpx.AsyncClient, href: str, name: str
    ) -> tuple[bytes, str, str] | None:
        try:
            r = await http.get(href)
            content_type = r.headers.get("content-type", "application/pdf")
            if not self._is_document_body(r.content, content_type):
                return None
            self.mark_timing("doc_pdf_bytes")
            return r.content, content_type, self._name_from_headers(
                r.headers, href, name
            )
        except Exception as e:
            log.warning("usaa: direct document fetch failed for %s: %s", href, e)
            return None

    def _single_document(
        self, body: bytes, content_type: str, name: str
    ) -> tuple[list[Document], dict[str, bytes]]:
        display_name = name.strip() or "USAA document 1"
        if (
            ("pdf" in content_type.lower() or body.startswith(b"%PDF"))
            and not display_name.lower().endswith(".pdf")
        ):
            display_name += ".pdf"
        doc = Document(
            id="usaa-doc-0",
            name=display_name,
            content_type=content_type,
            size_bytes=len(body),
        )
        return [doc], {doc.id: body}

    def _merge_documents(
        self,
        target_docs: list[Document],
        target_bytes: dict[str, bytes],
        seen: set[str],
        source_docs: list[Document],
        source_bytes: dict[str, bytes],
    ) -> None:
        for source in source_docs:
            body = source_bytes.get(source.id)
            if not body:
                continue
            key = hashlib.sha256(body).hexdigest()
            if key in seen:
                continue
            seen.add(key)
            doc_id = f"usaa-doc-{len(target_docs)}"
            doc = Document(
                id=doc_id,
                name=source.name,
                content_type=source.content_type,
                size_bytes=len(body),
            )
            target_docs.append(doc)
            target_bytes[doc_id] = body

    async def _extract_document_from_page(
        self, page: Page, http: httpx.AsyncClient
    ) -> tuple[bytes | None, str]:
        urls = await page.eval_on_selector_all(
            "embed[src], iframe[src], object[data], a[href$='.pdf'], a[href*='.pdf?']",
            """els => els.map(e => e.src || e.data || e.href).filter(Boolean)""",
        )
        for url in urls:
            try:
                r = await http.get(url)
                content_type = r.headers.get("content-type", "application/pdf")
                if self._is_document_body(r.content, content_type):
                    return r.content, content_type
            except Exception:
                continue
        return None, "application/pdf"

    @staticmethod
    def _is_document_body(body: bytes, content_type: str) -> bool:
        if not body:
            return False
        lowered = content_type.lower()
        if body.lstrip().lower().startswith((b"<!doctype html", b"<html")):
            return False
        return body.startswith(b"%PDF") or "pdf" in lowered or "octet-stream" in lowered

    @staticmethod
    def _looks_like_document_response(resp) -> bool:
        content_type = resp.headers.get("content-type", "").lower()
        url = resp.url.lower()
        return (
            "pdf" in content_type
            or "octet-stream" in content_type
            or ".pdf" in url
            or "document" in url
            or "content" in url
        )

    @staticmethod
    def _name_from_response(resp, fallback: str) -> str:
        return UsaaFlow._name_from_headers(resp.headers, resp.url, fallback)

    @staticmethod
    def _name_from_headers(headers, url: str, fallback: str) -> str:
        disposition = headers.get("content-disposition", "")
        match = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)', disposition, re.I)
        if match:
            return match.group(1).strip()
        tail = url.split("?", 1)[0].rstrip("/").rsplit("/", 1)[-1]
        if tail and "." in tail:
            return tail
        return fallback

    async def _prepare_page(self, page: Page) -> None:
        return None

    async def _wait_after_mfa_submit(self, page: Page) -> None:
        deadline = time.perf_counter() + 2.0
        while time.perf_counter() < deadline:
            code_inputs = await page.locator(MFA_CODE_INPUT_SELECTOR).count()
            body = (await self._body_text(page, timeout_ms=500)).lower()
            if any(
                phrase in body
                for phrase in (
                    "invalid code",
                    "incorrect code",
                    "code you entered",
                    "expired",
                )
            ):
                raise RuntimeError("USAA MFA code was rejected")
            if code_inputs == 0:
                self.mark_timing("mfa_code_input_gone")
                return
            url = page.url.lower()
            challenge_tokens = (
                "mfa",
                "otp",
                "verify",
                "security",
                "challenge",
                "logon",
            )
            if not any(k in url for k in challenge_tokens):
                self.mark_timing("mfa_url_left_challenge")
                return
            await page.wait_for_timeout(150)
        self.mark_timing("mfa_short_wait_capped")

    async def _prefer_email_mfa(self, page: Page) -> None:
        if await page.locator(MFA_CODE_INPUT_SELECTOR).count() > 0:
            return

        target_email = (settings.usaa_mfa_email or "").lower()
        body = (await self._body_text(page)).lower()
        if not target_email and "email" not in body:
            return

        if await page.locator(
            "button[aria-busy='true']",
            has_text=re.compile(r"email security code|email", re.I),
        ).count():
            if await self._wait_for_mfa_code_input(page, timeout_ms=15000):
                return

        if "check your phone" in body and "different option" in body:
            try:
                await page.get_by_text(
                    re.compile(r"i need a different option|different option", re.I)
                ).click(timeout=5000)
                await page.wait_for_timeout(750)
                body = (await self._body_text(page)).lower()
            except Exception:
                return

        if target_email and target_email not in body and "email" not in body:
            return

        email_pattern = re.compile(
            re.escape(target_email) if target_email and target_email in body else r"email",
            re.I,
        )
        try:
            email_choice = await self._first_present(
                page.locator("button", has_text=re.compile(r"email security code", re.I)).first,
                page.get_by_label(email_pattern).first,
                page.locator("label", has_text=email_pattern).first,
                page.get_by_text(email_pattern).first,
                timeout_ms=5000,
            )
            await email_choice.click()
            if await self._wait_for_mfa_code_input(page, timeout_ms=15000):
                return
            await page.wait_for_timeout(500)
        except Exception:
            pass

        body = (await self._body_text(page)).lower()
        if not any(
            phrase in body
            for phrase in ("send code", "email me", "verify your identity", "continue")
        ):
            return
        try:
            button = await self._first_present(
                page.get_by_role(
                    "button",
                    name=re.compile(r"send|email|continue|next", re.I),
                ).first,
                page.locator("button[type='submit']:visible").first,
                timeout_ms=3000,
            )
            await button.click()
            await self._wait_for_mfa_code_input(page, timeout_ms=15000)
        except Exception:
            pass

    async def _wait_for_mfa_code_input(self, page: Page, timeout_ms: int) -> bool:
        try:
            await page.locator(MFA_CODE_INPUT_SELECTOR).first.wait_for(
                state="visible", timeout=timeout_ms
            )
            return True
        except Exception:
            return False

    async def _collect_document_links(self, page: Page) -> list[tuple[str, str]]:
        links: list[tuple[str, str]] = await page.eval_on_selector_all(
            "a[href], button[data-href], [role='link'][href]",
            """els => els.map(e => {
                const rects = e.getClientRects();
                const style = window.getComputedStyle(e);
                const visible = rects.length > 0
                    && style.visibility !== 'hidden'
                    && style.display !== 'none'
                    && Number(style.opacity || '1') > 0;
                const inChrome = !!e.closest(
                    'header, footer, nav, .usaa-globalHeader, .usaa-globalFooterNav, .headerDropMenu'
                );
                const href = e.href || e.getAttribute('data-href') || '';
                const text = (e.innerText || e.textContent || '').trim().slice(0, 120);
                return { text: text || 'USAA document', href, visible, inChrome };
            }).filter(e => e.visible && !e.inChrome).map(e => [e.text, e.href])""",
        )
        doc_pattern = re.compile(
            r"pdf|document|policy|declaration|id.?card|insurance.?card|proof",
            re.I,
        )
        seen: set[str] = set()
        candidates: list[tuple[str, str]] = []
        for name, href in links:
            if not href or href in seen:
                continue
            if doc_pattern.search(name) or doc_pattern.search(href):
                seen.add(href)
                candidates.append((name, href))
        return candidates

    async def _settle(
        self,
        page: Page,
        delay_ms: int = 1000,
        networkidle_timeout_ms: int = 3000,
    ) -> None:
        try:
            await page.wait_for_load_state("networkidle", timeout=networkidle_timeout_ms)
        except Exception:
            pass
        await page.wait_for_timeout(delay_ms)

    async def _slow_fill(self, loc: Locator, value: str) -> None:
        try:
            await loc.click()
            await loc.fill("")
            await loc.type(value, delay=35)
        except Exception:
            await loc.fill(value)

    async def _wait_for_password_field(
        self, page: Page, timeout_ms: int = 45000
    ) -> Locator:
        deadline = time.perf_counter() + (timeout_ms / 1000)
        password_locators = (
            page.locator("input[name='password']:visible").first,
            page.get_by_label(re.compile(r"Password", re.I)).first,
            page.locator("input[type='password']:visible").first,
        )
        while time.perf_counter() < deadline:
            for locator in password_locators:
                try:
                    await locator.wait_for(state="visible", timeout=300)
                    return locator
                except Exception:
                    pass
            if await self._looks_blocked(page):
                raise RuntimeError(
                    "USAA blocked or returned unavailable after the Online ID step"
                )
            await page.wait_for_timeout(300)
        raise RuntimeError("USAA password field did not appear after Online ID step")

    async def _body_text(self, page: Page, timeout_ms: int = 3000) -> str:
        try:
            return await page.locator("body").inner_text(timeout=timeout_ms)
        except Exception:
            return ""

    async def _looks_blocked(self, page: Page) -> bool:
        url = page.url.lower()
        if "chrome-error" in url:
            return True
        body = (await self._body_text(page)).lower()
        return any(
            phrase in body
            for phrase in (
                "access denied",
                "unable to complete your request",
                "system is currently unavailable",
                "request unsuccessful",
                "reference #",
                "bot manager",
                "akamai",
            )
        )

    async def _page_has_unavailable_block(self, page: Page) -> bool:
        body = (await self._body_text(page)).lower()
        return self._is_unavailable_block_text(body)

    @staticmethod
    def _is_unavailable_block_text(body: str) -> bool:
        return (
            "unable to complete your request" in body
            and "system is currently unavailable" in body
        )

    async def _dump_debug(self, page: Page, label: str) -> None:
        try:
            DEBUG_DIR.mkdir(exist_ok=True)
            png = DEBUG_DIR / f"usaa-{label}.png"
            html = DEBUG_DIR / f"usaa-{label}.html"
            html.write_text(self._sanitize_debug_html(await page.content()))
            try:
                await page.screenshot(path=str(png), full_page=True, timeout=5000)
            except Exception as e:
                log.warning("usaa: failed to capture screenshot %s: %s", png, e)
            log.warning("usaa debug dump -> %s, %s", png, html)
        except Exception as e:
            log.warning("usaa: failed to dump debug artifacts: %s", e)

    @staticmethod
    def _sanitize_debug_html(html: str) -> str:
        def redact_input(match: re.Match) -> str:
            tag = match.group(0)
            if "password" not in tag.lower():
                return tag
            return re.sub(
                r"value=(['\"])(.*?)\1",
                r"value=\1[redacted]\1",
                tag,
                flags=re.I,
            )

        return re.sub(r"<input\b[^>]*>", redact_input, html, flags=re.I)

    @staticmethod
    async def _first_present(*locators: Locator, timeout_ms: int = 7000) -> Locator:
        per = max(1000, timeout_ms // max(1, len(locators)))
        for loc in locators[:-1]:
            try:
                await loc.wait_for(state="visible", timeout=per)
                return loc
            except Exception:
                continue
        await locators[-1].wait_for(state="visible", timeout=per)
        return locators[-1]
