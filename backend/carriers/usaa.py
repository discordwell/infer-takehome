from __future__ import annotations

import asyncio
import logging
import re
import time
from pathlib import Path

import httpx
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
    "https://www.usaa.com/inet/wc/document_center",
    "https://www.usaa.com/my/insurance",
    "https://www.usaa.com/inet/wc/insurance_auto_main",
)
DEBUG_DIR = Path("/tmp")
USAA_CHROME_PROFILE_DIR = (
    Path(__file__).resolve().parent.parent.parent
    / "storage"
    / "browser-profiles"
    / "usaa-chrome"
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

    def context_options(self) -> dict:
        return {
            "_launch_chrome_cdp": True,
            "_chrome_profile_dir": str(USAA_CHROME_PROFILE_DIR),
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
        docs, doc_bytes = await self._fetch_from_document_surface(page, http, ctx)
        if docs:
            self.mark_timing("docs_first_document_ready")
            return docs, doc_bytes

        candidates: list[tuple[str, str]] = []
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
                                return self._single_document(
                                    body,
                                    content_type,
                                    self._name_from_headers(
                                        resp.headers, resp.url, "USAA document 1"
                                    ),
                                )
                        except Exception:
                            pass
                title = (await page.title()).lower()
                if "logon" in page.url.lower() or "login" in page.url.lower():
                    continue
                if "page not found" in title:
                    continue
                try:
                    await page.locator("button[data-testid^='readDocument-']").first.wait_for(
                        state="visible", timeout=8000
                    )
                    self.mark_timing("docs_button_visible")
                except Exception:
                    await page.wait_for_timeout(500)
            except Exception as e:
                log.warning("usaa: docs URL %s failed: %s", url, e)
                continue
            docs, doc_bytes = await self._fetch_from_document_surface(page, http, ctx)
            if docs:
                self.mark_timing("docs_first_document_ready")
                return docs, doc_bytes

            candidates = await self._collect_document_links(page)
            if candidates:
                break

        if not candidates:
            await self._dump_debug(page, "docs-no-links")
            raise RuntimeError("USAA: authenticated, but no document links found yet")

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
        if not docs:
            await self._dump_debug(page, "docs-fetch-failed")
            raise RuntimeError("USAA: found document links, but downloads failed")
        return docs, doc_bytes

    async def _fetch_from_document_surface(
        self,
        page: Page,
        http: httpx.AsyncClient,
        ctx: BrowserContext,
    ) -> tuple[list[Document], dict[str, bytes]]:
        docs, doc_bytes = await self._fetch_document_buttons(page, http, ctx)
        if docs:
            return docs, doc_bytes

        docs, doc_bytes = await self._fetch_named_document_actions(page, http, ctx)
        if docs:
            return docs, doc_bytes

        if await self._open_policy_documents_from_summary(page):
            self.mark_timing("policy_documents_opened")
            docs, doc_bytes = await self._fetch_document_buttons(page, http, ctx)
            if docs:
                return docs, doc_bytes
            return await self._fetch_named_document_actions(page, http, ctx)

        return [], {}

    async def _fetch_document_buttons(
        self,
        page: Page,
        http: httpx.AsyncClient,
        ctx: BrowserContext,
    ) -> tuple[list[Document], dict[str, bytes]]:
        buttons = page.locator("button[data-testid^='readDocument-']")
        count = await buttons.count()
        if count == 0:
            return [], {}

        button = buttons.first
        try:
            name = (await button.inner_text(timeout=2000)).strip() or "USAA document 1"
        except Exception:
            name = "USAA document 1"

        try:
            href = await self._direct_document_href(button)
            if href:
                direct = await self._fetch_direct_document(http, href, name)
                if direct is not None:
                    body, content_type, display_name = direct
                    return self._single_document(body, content_type, display_name)

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
            download_task = asyncio.create_task(
                page.wait_for_event("download", timeout=7000)
            )
            popup_task = asyncio.create_task(ctx.wait_for_event("page", timeout=7000))
            try:
                await button.click(timeout=5000)
                self.mark_timing("doc_button_clicked")
                payload = await self._first_document_payload(
                    page, http, response_queue, download_task, popup_task, name
                )
            finally:
                page.remove_listener("response", on_response)
                for task in (download_task, popup_task):
                    if not task.done():
                        task.cancel()

            if payload is not None:
                body, content_type, display_name = payload
                return self._single_document(body, content_type, display_name)
        except Exception as e:
            log.warning("usaa: first document button failed: %s", e)
        return [], {}

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
                    return self._single_document(body, content_type, display_name)
            except Exception as e:
                log.warning("usaa: document action %s failed: %s", pattern.pattern, e)
        return [], {}

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
            return await self._first_document_payload(
                page, http, response_queue, download_task, popup_task, name
            )
        finally:
            page.remove_listener("response", on_response)
            for task in (download_task, popup_task):
                if not task.done():
                    task.cancel()

    async def _open_policy_documents_from_summary(self, page: Page) -> bool:
        rows = page.locator("li", has_text=re.compile(r"Policy documents", re.I))
        if await rows.count() == 0:
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
                const href = e.href || e.getAttribute('data-href') || '';
                const text = (e.innerText || e.textContent || '').trim().slice(0, 120);
                return [text || 'USAA document', href];
            })""",
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

    async def _dump_debug(self, page: Page, label: str) -> None:
        try:
            DEBUG_DIR.mkdir(exist_ok=True)
            png = DEBUG_DIR / f"usaa-{label}.png"
            html = DEBUG_DIR / f"usaa-{label}.html"
            html.write_text(await page.content())
            try:
                await page.screenshot(path=str(png), full_page=True, timeout=5000)
            except Exception as e:
                log.warning("usaa: failed to capture screenshot %s: %s", png, e)
            log.warning("usaa debug dump -> %s, %s", png, html)
        except Exception as e:
            log.warning("usaa: failed to dump debug artifacts: %s", e)

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
