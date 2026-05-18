from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urljoin, urlsplit

import httpx
from playwright.async_api import BrowserContext, Locator, Page

from ..models import Carrier, Document
from .base import CarrierFlow

log = logging.getLogger(__name__)

DEBUG_DIR = Path("/tmp")
MERCURY_MAX_DECLARATION_PDF_BYTES = 750_000


@dataclass(frozen=True)
class GenericPortalSpec:
    carrier: Carrier
    label: str
    login_url: str
    dashboard_urls: tuple[str, ...]
    document_urls: tuple[str, ...]
    auth_phrases: tuple[str, ...] = (
        "sign out",
        "log out",
        "my policy",
        "policies",
        "documents",
        "id cards",
    )
    invalid_phrases: tuple[str, ...] = (
        "invalid",
        "incorrect",
        "does not match",
        "could not find",
        "try again",
    )


class GenericPortalFlow(CarrierFlow):
    """Best-effort adapter for personal-lines portals with similar flows.

    This is intentionally generic and diagnostic-heavy. It gives us a running
    path for newly supplied credentials, then the debug artifacts tell us which
    carrier-specific selectors or document URLs need promotion into a bespoke
    adapter.
    """

    def __init__(self, spec: GenericPortalSpec) -> None:
        self.spec = spec
        self.carrier = spec.carrier

    async def login(self, page: Page, username: str, password: str) -> None:
        log.info("%s: navigating to login URL", self.spec.label)
        await page.goto(self.spec.login_url, wait_until="domcontentloaded", timeout=30000)
        await self._settle(page, delay_ms=700, networkidle_timeout_ms=5000)

        try:
            user_field = await self._first_present(
                page.get_by_label(
                    re.compile(r"email|username|user\s*id|online\s*id|policy", re.I)
                ).first,
                page.locator("input[autocomplete='username']:visible").first,
                page.locator("input[type='email']:visible").first,
                page.locator("input[name*='user' i]:visible").first,
                page.locator("input[name*='email' i]:visible").first,
                page.locator("input[id*='user' i]:visible").first,
                page.locator("input[id*='email' i]:visible").first,
                page.locator("input[type='text']:visible").first,
            )
            await self._slow_fill(user_field, username)

            try:
                pw_field = await self._password_field(page, timeout_ms=2500)
            except Exception:
                await self._advance_after_username(page, user_field)
                await self._settle(page, delay_ms=400, networkidle_timeout_ms=5000)
                pw_field = await self._password_field(page)
            await self._slow_fill(pw_field, password)

            try:
                submit = await self._first_present(
                    page.get_by_role(
                        "button",
                        name=re.compile(r"log\s*in|sign\s*in|continue|next", re.I),
                    ).first,
                    page.locator("button[type='submit']:visible").first,
                    page.locator("input[type='submit']:visible").first,
                )
                await submit.click(timeout=5000)
            except Exception:
                await pw_field.press("Enter")
        except Exception as e:
            await self._dump_debug(page, "login-failure")
            raise RuntimeError(
                f"{self.spec.label} login form interaction failed: {e}"
            ) from e

        await self._settle(page, delay_ms=800, networkidle_timeout_ms=12000)
        if await self._looks_login_rejected(page):
            await self._dump_debug(page, "login-rejected")
            raise RuntimeError(f"{self.spec.label} login rejected")

    async def mfa_required(self, page: Page) -> bool:
        url = page.url.lower()
        if any(k in url for k in ("mfa", "otp", "verify", "challenge", "two-step")):
            return True
        if await page.locator(self._otp_selector()).count() > 0:
            return True
        body = (await self._body_text(page)).lower()
        return any(
            phrase in body
            for phrase in (
                "verification code",
                "security code",
                "one-time code",
                "two-step",
                "verify your identity",
                "we sent you a code",
            )
        )

    async def submit_mfa(self, page: Page, code: str) -> None:
        try:
            otp_field = await self._first_present(
                page.locator(self._otp_selector()).first,
                page.locator("input[name*='code' i]:visible").first,
                page.locator("input[id*='code' i]:visible").first,
            )
            await self._slow_fill(otp_field, code)
            try:
                submit = await self._first_present(
                    page.get_by_role(
                        "button",
                        name=re.compile(r"continue|submit|verify|next", re.I),
                    ).first,
                    page.locator("button[type='submit']:visible").first,
                    page.locator("input[type='submit']:visible").first,
                )
                await submit.click(timeout=5000)
            except Exception:
                await otp_field.press("Enter")
        except Exception as e:
            await self._dump_debug(page, "mfa-failure")
            raise RuntimeError(f"{self.spec.label} MFA interaction failed: {e}") from e

        await self._settle(page, delay_ms=800, networkidle_timeout_ms=12000)

    async def is_authenticated(self, page: Page) -> bool:
        for url in self.spec.dashboard_urls:
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=10000)
                await self._settle(page, delay_ms=400, networkidle_timeout_ms=2500)
            except Exception:
                continue
            if self._is_login_url(page.url):
                continue
            body = (await self._body_text(page, timeout_ms=3000)).lower()
            if any(phrase in body for phrase in self.spec.auth_phrases):
                return True
        return False

    async def fetch_documents(
        self,
        page: Page,
        http: httpx.AsyncClient,
        ctx: BrowserContext,
    ) -> tuple[list[Document], dict[str, bytes]]:
        for url in self.spec.document_urls + self.spec.dashboard_urls:
            try:
                resp = await page.goto(url, wait_until="domcontentloaded", timeout=15000)
                await self._settle(page, delay_ms=700, networkidle_timeout_ms=7000)
            except Exception as e:
                log.warning("%s: document URL %s failed: %s", self.spec.label, url, e)
                continue
            if resp is not None:
                direct = await self._document_from_response(resp)
                if direct is not None:
                    return self._single_document(*direct)
            docs, doc_bytes = await self._fetch_links(page, http)
            if docs:
                return docs, doc_bytes
            docs, doc_bytes = await self._click_document_actions(page, http, ctx)
            if docs:
                return docs, doc_bytes

        await self._dump_debug(page, "docs-not-found")
        raise RuntimeError(f"{self.spec.label}: no policy documents found")

    async def _fetch_links(
        self, page: Page, http: httpx.AsyncClient
    ) -> tuple[list[Document], dict[str, bytes]]:
        links = await self._collect_document_links(page)

        async def fetch(name: str, href: str, idx: int):
            try:
                r = await http.get(href)
                r.raise_for_status()
                content_type = r.headers.get("content-type", "application/pdf")
                if not self._is_document_body(r.content, content_type):
                    return None
                return self._document(f"doc-{idx}", name, r.content, content_type)
            except Exception as e:
                log.warning("%s: failed to fetch %s: %s", self.spec.label, href, e)
                return None

        results = await asyncio.gather(
            *[fetch(name, href, idx) for idx, (name, href) in enumerate(links)]
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

    async def _click_document_actions(
        self,
        page: Page,
        http: httpx.AsyncClient,
        ctx: BrowserContext,
    ) -> tuple[list[Document], dict[str, bytes]]:
        candidates = page.locator(
            "button:visible, [role='button']:visible, a:visible",
            has_text=re.compile(
                r"documents?|declarations?|policy|id cards?|proof|download|view|print",
                re.I,
            ),
        )
        count = min(await candidates.count(), 8)
        for idx in range(count):
            target = candidates.nth(idx)
            try:
                name = (await target.inner_text(timeout=1000)).strip()
            except Exception:
                name = f"{self.spec.label} document {idx + 1}"
            payload = await self._click_for_document(page, http, ctx, target, name)
            if payload is None:
                continue
            return self._single_document(*payload)
        return [], {}

    async def _click_for_document(
        self,
        page: Page,
        http: httpx.AsyncClient,
        ctx: BrowserContext,
        target: Locator,
        name: str,
        *,
        include_current_page: bool = True,
    ) -> tuple[bytes, str, str] | None:
        response_queue: asyncio.Queue = asyncio.Queue()

        def on_response(resp):
            if self._looks_like_document_response(resp):
                response_queue.put_nowait(resp)

        page.on("response", on_response)
        download_task = asyncio.create_task(page.wait_for_event("download", timeout=6000))
        popup_task = asyncio.create_task(ctx.wait_for_event("page", timeout=6000))
        try:
            try:
                await target.scroll_into_view_if_needed(timeout=1500)
            except Exception:
                pass
            try:
                await target.click(timeout=5000)
            except Exception:
                await target.click(timeout=5000, force=True)
            tasks = [
                asyncio.create_task(
                    self._document_from_response_queue(response_queue, name)
                ),
                asyncio.create_task(self._document_from_download(download_task, name)),
                asyncio.create_task(self._document_from_popup(popup_task, http, name)),
            ]
            if include_current_page:
                tasks.append(
                    asyncio.create_task(
                        self._document_from_current_page(page, http, name)
                    )
                )
            try:
                for completed in asyncio.as_completed(tasks, timeout=7.0):
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
        except Exception:
            return None
        finally:
            page.remove_listener("response", on_response)
            for task in (download_task, popup_task):
                if not task.done():
                    task.cancel()
        return None

    async def _document_from_response_queue(
        self, queue: asyncio.Queue, name: str
    ) -> tuple[bytes, str, str]:
        deadline = asyncio.get_running_loop().time() + 6.5
        while asyncio.get_running_loop().time() < deadline:
            remaining = deadline - asyncio.get_running_loop().time()
            try:
                resp = await asyncio.wait_for(queue.get(), timeout=remaining)
            except asyncio.TimeoutError:
                break
            content_type = resp.headers.get("content-type", "application/pdf")
            try:
                body = await resp.body()
            except Exception:
                continue
            if not self._is_document_body(body, content_type):
                continue
            return body, content_type, self._name_from_headers(
                resp.headers, resp.url, name
            )
        raise RuntimeError("no valid document response observed")

    async def _document_from_download(
        self, download_task: asyncio.Task, name: str
    ) -> tuple[bytes, str, str]:
        download = await download_task
        path = await download.path()
        if path is None:
            raise RuntimeError("download path missing")
        body = Path(path).read_bytes()
        if not self._is_document_body(body, "application/pdf"):
            raise RuntimeError("download is not a document")
        return body, "application/pdf", download.suggested_filename or name

    async def _document_from_popup(
        self, popup_task: asyncio.Task, http: httpx.AsyncClient, name: str
    ) -> tuple[bytes, str, str]:
        popup = await popup_task
        try:
            await self._settle(popup, delay_ms=400, networkidle_timeout_ms=3000)
            body, content_type = await self._extract_document_from_page(popup, http)
            if body is None:
                raise RuntimeError("popup did not expose a document")
            return body, content_type, name
        finally:
            if not popup.is_closed():
                await popup.close()

    async def _document_from_current_page(
        self, page: Page, http: httpx.AsyncClient, name: str
    ) -> tuple[bytes, str, str]:
        await page.wait_for_timeout(500)
        body, content_type = await self._extract_document_from_page(page, http)
        if body is None:
            raise RuntimeError("current page did not expose a document")
        return body, content_type, name

    async def _collect_document_links(self, page: Page) -> list[tuple[str, str]]:
        links: list[tuple[str, str]] = await page.eval_on_selector_all(
            "a[href], [role='link'][href], button[data-href], button[data-url]",
            """els => els.map(e => {
                const rects = e.getClientRects();
                const style = window.getComputedStyle(e);
                const visible = rects.length > 0
                    && style.visibility !== 'hidden'
                    && style.display !== 'none'
                    && Number(style.opacity || '1') > 0;
                const inChrome = !!e.closest('header, footer, nav');
                const href = e.href || e.getAttribute('href')
                    || e.getAttribute('data-href') || e.getAttribute('data-url') || '';
                const text = (e.innerText || e.textContent || '').trim().slice(0, 120);
                return { text: text || 'document', href, visible, inChrome };
            }).filter(e => e.visible && !e.inChrome).map(e => [e.text, e.href])""",
        )
        doc_pattern = re.compile(
            r"pdf|document|policy|declaration|id.?card|insurance.?card|proof",
            re.I,
        )
        seen: set[str] = set()
        candidates: list[tuple[str, str]] = []
        for name, href in links:
            href = urljoin(page.url, href or "")
            if not href or href in seen:
                continue
            if not self._is_http_url(href):
                continue
            if doc_pattern.search(name) or doc_pattern.search(href):
                seen.add(href)
                candidates.append((name, href))
        return candidates[:16]

    async def _extract_document_from_page(
        self, page: Page, http: httpx.AsyncClient
    ) -> tuple[bytes | None, str]:
        urls: list[str] = []
        if self._is_blob_url(page.url):
            urls.append(page.url)
        embedded_urls = await page.eval_on_selector_all(
            (
                "embed[src], iframe[src], object[data], "
                "a[href^='blob:'], a[href$='.pdf'], a[href*='.pdf?']"
            ),
            "els => els.map(e => e.src || e.data || e.href).filter(Boolean)",
        )
        urls.extend(embedded_urls)
        for url in urls:
            url = urljoin(page.url, url)
            if self._is_blob_url(url):
                body, content_type = await self._fetch_blob_document(page, url)
                if body is not None:
                    return body, content_type
                continue
            if not self._is_http_url(url):
                continue
            try:
                r = await http.get(url)
                content_type = r.headers.get("content-type", "application/pdf")
                if self._is_document_body(r.content, content_type):
                    return r.content, content_type
            except Exception:
                continue
        return None, "application/pdf"

    async def _fetch_blob_document(
        self, page: Page, url: str
    ) -> tuple[bytes | None, str]:
        try:
            result = await page.evaluate(
                """async (url) => {
                    const response = await fetch(url);
                    const buffer = await response.arrayBuffer();
                    const bytes = new Uint8Array(buffer);
                    let binary = '';
                    const chunkSize = 0x8000;
                    for (let i = 0; i < bytes.length; i += chunkSize) {
                        binary += String.fromCharCode(...bytes.subarray(i, i + chunkSize));
                    }
                    return {
                        contentType: response.headers.get('content-type') || 'application/pdf',
                        bodyBase64: btoa(binary)
                    };
                }""",
                url,
            )
        except Exception as e:
            log.warning("%s: failed to read blob document: %s", self.spec.label, e)
            return None, "application/pdf"

        body = base64.b64decode(result.get("bodyBase64", ""))
        content_type = result.get("contentType") or "application/pdf"
        if not self._is_document_body(body, content_type):
            return None, content_type
        return body, content_type

    async def _document_from_response(self, resp) -> tuple[bytes, str, str] | None:
        content_type = resp.headers.get("content-type", "application/pdf")
        if not self._looks_like_document_response(resp):
            return None
        try:
            body = await resp.body()
        except Exception:
            return None
        if not self._is_document_body(body, content_type):
            return None
        return body, content_type, self._name_from_headers(
            resp.headers, resp.url, f"{self.spec.label} document"
        )

    def _single_document(
        self, body: bytes, content_type: str, name: str
    ) -> tuple[list[Document], dict[str, bytes]]:
        doc, body = self._document("doc-0", name, body, content_type)
        return [doc], {doc.id: body}

    def _document(
        self, doc_id: str, name: str, body: bytes, content_type: str
    ) -> tuple[Document, bytes]:
        display_name = (name or f"{self.spec.label} document").strip()
        if (
            ("pdf" in content_type.lower() or body.startswith(b"%PDF"))
            and not display_name.lower().endswith(".pdf")
        ):
            display_name += ".pdf"
        return (
            Document(
                id=doc_id,
                name=display_name,
                content_type=content_type,
                size_bytes=len(body),
            ),
            body,
        )

    async def _looks_login_rejected(self, page: Page) -> bool:
        if not self._is_login_url(page.url):
            return False
        body = (await self._body_text(page)).lower()
        return any(phrase in body for phrase in self.spec.invalid_phrases)

    async def _password_field(self, page: Page, timeout_ms: int = 10000) -> Locator:
        return await self._first_present(
            page.get_by_label(re.compile(r"password", re.I)).first,
            page.locator("input[autocomplete='current-password']:visible").first,
            page.locator("input[type='password']:visible").first,
            timeout_ms=timeout_ms,
        )

    async def _advance_after_username(self, page: Page, user_field: Locator) -> None:
        try:
            submit = await self._first_present(
                page.get_by_role(
                    "button",
                    name=re.compile(r"continue|next|log\s*in|sign\s*in", re.I),
                ).first,
                page.locator("button[type='submit']:visible").first,
                page.locator("input[type='submit']:visible").first,
                timeout_ms=4000,
            )
            await submit.click(timeout=5000)
        except Exception:
            await user_field.press("Enter")

    def _is_login_url(self, url: str) -> bool:
        lowered = url.lower()
        return any(k in lowered for k in ("login", "signin", "sign-in", "logon"))

    @staticmethod
    def _is_http_url(url: str) -> bool:
        return urlsplit(url).scheme in {"http", "https"}

    @staticmethod
    def _is_blob_url(url: str) -> bool:
        return urlsplit(url).scheme == "blob"

    async def _body_text(self, page: Page, timeout_ms: int = 3000) -> str:
        try:
            return await page.locator("body").inner_text(timeout=timeout_ms)
        except Exception:
            return ""

    async def _settle(
        self, page: Page, delay_ms: int, networkidle_timeout_ms: int
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
            await loc.type(value, delay=25)
        except Exception:
            await loc.fill(value)

    async def _first_present(
        self, *locators: Locator, timeout_ms: int = 10000
    ) -> Locator:
        deadline = asyncio.get_running_loop().time() + (timeout_ms / 1000)
        last_error: Exception | None = None
        while asyncio.get_running_loop().time() < deadline:
            for loc in locators:
                try:
                    if await loc.count() > 0:
                        await loc.wait_for(state="visible", timeout=500)
                        return loc
                except Exception as e:
                    last_error = e
            await asyncio.sleep(0.2)
        raise RuntimeError(f"No matching locator became visible: {last_error}")

    async def _dump_debug(self, page: Page, label: str) -> None:
        safe_label = re.sub(r"[^a-z0-9-]+", "-", self.spec.carrier.value.lower())
        png = DEBUG_DIR / f"{safe_label}-{label}.png"
        html = DEBUG_DIR / f"{safe_label}-{label}.html"
        try:
            await page.screenshot(path=str(png), full_page=True, timeout=5000)
        except Exception:
            pass
        try:
            html.write_text(await page.content())
        except Exception:
            pass
        log.info("%s debug dump -> %s, %s", self.spec.label, png, html)

    @staticmethod
    def _otp_selector() -> str:
        return (
            "input[autocomplete='one-time-code']:visible, "
            "input[inputmode='numeric']:visible"
        )

    @staticmethod
    def _is_document_body(body: bytes, content_type: str) -> bool:
        if not body:
            return False
        if body.lstrip().lower().startswith((b"<!doctype html", b"<html")):
            return False
        lowered = content_type.lower()
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
            or "download" in url
        )

    @staticmethod
    def _name_from_headers(headers, url: str, fallback: str) -> str:
        disposition = headers.get("content-disposition", "")
        match = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)', disposition, re.I)
        if match:
            return unquote(match.group(1).strip())
        tail = url.split("?", 1)[0].rstrip("/").rsplit("/", 1)[-1]
        if tail and "." in tail:
            return tail
        return fallback


PROGRESSIVE_SPEC = GenericPortalSpec(
    carrier=Carrier.PROGRESSIVE,
    label="Progressive",
    login_url="https://account.apps.progressive.com/access/login",
    dashboard_urls=(
        "https://account.apps.progressive.com/",
        "https://www.progressive.com/manage-policy/",
    ),
    document_urls=(
        "https://account.apps.progressive.com/policy/documents",
        "https://account.apps.progressive.com/documents",
        "https://www.progressive.com/manage-policy/",
    ),
)

STATE_FARM_SPEC = GenericPortalSpec(
    carrier=Carrier.STATE_FARM,
    label="State Farm",
    login_url="https://auth.proofing.statefarm.com/login-ui/login",
    dashboard_urls=(
        "https://www.statefarm.com/customer-care/manage-your-accounts",
        "https://myaccounts.statefarm.com/",
        "https://www.statefarm.com/",
    ),
    document_urls=(
        "https://myaccounts.statefarm.com/",
        "https://www.statefarm.com/customer-care/manage-your-accounts",
    ),
)

ALLSTATE_SPEC = GenericPortalSpec(
    carrier=Carrier.ALLSTATE,
    label="Allstate",
    login_url="https://myaccountrwd.allstate.com/",
    dashboard_urls=(
        "https://myaccountrwd.allstate.com/",
        "https://www.allstate.com/help-support/account",
    ),
    document_urls=(
        "https://myaccountrwd.allstate.com/",
        "https://www.allstate.com/help-support/my-policy",
    ),
)


MERCURY_SPEC = GenericPortalSpec(
    carrier=Carrier.MERCURY,
    label="Mercury",
    login_url="https://cp.mercuryinsurance.com/",
    dashboard_urls=(
        "https://cp.mercuryinsurance.com/",
        "https://cp.mercuryinsurance.com/customer/",
        "https://www.mercuryinsurance.com/myaccount/",
    ),
    document_urls=(
        "https://cp.mercuryinsurance.com/",
        "https://cp.mercuryinsurance.com/customer/",
        "https://www.mercuryinsurance.com/myaccount/download-id-cards/",
    ),
    auth_phrases=(
        "sign out",
        "log out",
        "policy information",
        "policy documents",
        "id cards",
        "payment history",
        "claims",
    ),
)


class ProgressiveFlow(GenericPortalFlow):
    def __init__(self) -> None:
        super().__init__(PROGRESSIVE_SPEC)


class StateFarmFlow(GenericPortalFlow):
    def __init__(self) -> None:
        super().__init__(STATE_FARM_SPEC)


class AllstateFlow(GenericPortalFlow):
    def __init__(self) -> None:
        super().__init__(ALLSTATE_SPEC)


class MercuryFlow(GenericPortalFlow):
    def __init__(self) -> None:
        super().__init__(MERCURY_SPEC)

    async def fetch_documents(
        self,
        page: Page,
        http: httpx.AsyncClient,
        ctx: BrowserContext,
    ) -> tuple[list[Document], dict[str, bytes]]:
        await page.goto(
            "https://cp.mercuryinsurance.com/customer/dashboard",
            wait_until="domcontentloaded",
            timeout=15000,
        )
        await self._settle(page, delay_ms=700, networkidle_timeout_ms=7000)

        await self._dismiss_edelivery_overlay(page)
        if "/customer/mydocuments" not in page.url:
            if "/customer/policydetail" not in page.url:
                await self._open_first_policy(page)
            await self._open_document_history(page)

        await self._expand_document_groups(page)
        docs, doc_bytes = await self._open_declarations_document(page, http, ctx)
        if docs:
            return docs, doc_bytes

        await self._dump_debug(page, "mercury-docs-not-found")
        raise RuntimeError("Mercury: no policy declaration document found")

    async def _dismiss_edelivery_overlay(self, page: Page) -> None:
        button = page.get_by_text(re.compile(r"continue\s+to\s+account\s+page", re.I))
        if await button.count() == 0:
            return
        try:
            await button.first.click(timeout=2500)
            await self._settle(page, delay_ms=300, networkidle_timeout_ms=2500)
        except Exception:
            pass

    async def _open_first_policy(self, page: Page) -> None:
        target = await self._first_present(
            page.locator("a[href*='/customer/policydetail']:visible").first,
            page.locator("a[href*='policydetail']:visible").first,
            page.locator("a.pleaseWait:visible").first,
            timeout_ms=8000,
        )
        await target.click(timeout=5000)
        await self._wait_for_mercury_url(page, "/customer/policydetail")

    async def _open_document_history(self, page: Page) -> None:
        more_actions = await self._first_present(
            page.locator("div.moreActions:visible").first,
            page.locator("[role='button']:visible", has_text="More Actions").first,
            page.get_by_text(re.compile(r"more\s+actions", re.I)).first,
            timeout_ms=8000,
        )
        await more_actions.click(timeout=5000)
        history = await self._first_present(
            page.locator("a[href*='/customer/mydocuments']:visible").first,
            page.locator("a[href*='mydocuments']:visible").first,
            page.get_by_text(re.compile(r"document\s+history", re.I)).first,
            timeout_ms=8000,
        )
        await history.click(timeout=5000)
        await self._wait_for_mercury_url(page, "/customer/mydocuments")

    async def _expand_document_groups(self, page: Page) -> None:
        await self._first_present(
            page.locator("a[aria-label='View documents in group']").first,
            page.locator("a.document-drop-down").first,
            timeout_ms=10000,
        )
        expanders = page.locator(
            "a[aria-label='View documents in group'], a.document-drop-down"
        )
        for idx in range(await expanders.count()):
            if await self._visible_declarations_anchor_index(page) is not None:
                return
            expander = expanders.nth(idx)
            try:
                await expander.scroll_into_view_if_needed(timeout=1500)
            except Exception:
                pass
            try:
                await expander.click(timeout=2500)
            except Exception:
                try:
                    await expander.click(timeout=2500, force=True)
                except Exception:
                    continue
            await page.wait_for_timeout(350)
            if await self._visible_declarations_anchor_index(page) is not None:
                return

    async def _open_declarations_document(
        self,
        page: Page,
        http: httpx.AsyncClient,
        ctx: BrowserContext,
    ) -> tuple[list[Document], dict[str, bytes]]:
        candidate_indices = await self._declarations_anchor_indices(page)
        preferred_idx = await self._preferred_declarations_anchor_index(page)
        if preferred_idx is not None:
            candidate_indices = [
                preferred_idx,
                *[idx for idx in candidate_indices if idx != preferred_idx],
            ]
        if not candidate_indices:
            await self._dump_mercury_document_links(page)
            raise RuntimeError("Mercury: declaration link not found")

        oversized: list[tuple[int, int, str]] = []
        for target_idx in candidate_indices:
            target = page.locator("a").nth(target_idx)
            name = await self._anchor_text(page, target_idx)
            if not name:
                name = "Auto Insurance Policy Declarations"
            payload = await self._click_for_mercury_document(page, ctx, target, name)
            if payload is None:
                continue
            body, content_type, filename = payload
            if self._is_plausible_mercury_declarations_pdf(body):
                return self._single_document(body, content_type, filename)
            oversized.append((target_idx, len(body), filename))
            log.warning(
                "Mercury declaration candidate %s yielded large PDF (%d bytes); "
                "rejecting it and trying next declaration candidate",
                target_idx,
                len(body),
            )
            await self._return_to_documents_page(page)

        await self._dump_mercury_document_links(page)
        if oversized:
            detail = ", ".join(
                f"anchor {idx}: {size} bytes ({name})"
                for idx, size, name in oversized[:5]
            )
            raise RuntimeError(
                "Mercury: only oversized declaration-like PDFs were found; "
                f"refusing to return likely wrong document. {detail}"
            )
        return [], {}

    async def _click_for_mercury_document(
        self,
        page: Page,
        ctx: BrowserContext,
        target: Locator,
        name: str,
    ) -> tuple[bytes, str, str] | None:
        response_queue: asyncio.Queue = asyncio.Queue()

        def on_response(resp):
            if "/OAUTH/CONSUMER/Document/V1" in resp.url:
                response_queue.put_nowait(resp)

        page.on("response", on_response)
        popup_task = asyncio.create_task(ctx.wait_for_event("page", timeout=8000))
        try:
            try:
                await target.scroll_into_view_if_needed(timeout=1500)
            except Exception:
                pass
            try:
                await target.click(timeout=5000)
            except Exception:
                await target.click(timeout=5000, force=True)

            try:
                body, content_type, filename = await self._mercury_pdf_from_queue(
                    response_queue, name
                )
                return body, content_type, filename
            finally:
                if popup_task.done() and not popup_task.cancelled():
                    try:
                        popup = popup_task.result()
                        if not popup.is_closed():
                            await popup.close()
                    except Exception:
                        pass
        except Exception as e:
            log.warning("Mercury document click did not yield a PDF payload: %s", e)
            return None
        finally:
            page.remove_listener("response", on_response)
            if not popup_task.done():
                popup_task.cancel()

    async def _mercury_pdf_from_queue(
        self, queue: asyncio.Queue, name: str
    ) -> tuple[bytes, str, str]:
        deadline = asyncio.get_running_loop().time() + 8
        last_error: Exception | None = None
        while asyncio.get_running_loop().time() < deadline:
            remaining = deadline - asyncio.get_running_loop().time()
            try:
                resp = await asyncio.wait_for(queue.get(), timeout=remaining)
            except asyncio.TimeoutError:
                break
            try:
                data = json.loads((await resp.body()).decode("utf-8"))
                response = data.get("response") or {}
                if response.get("messageCode") != "200":
                    raise RuntimeError(f"Document/V1 messageCode={response.get('messageCode')}")
                encoded = response.get("pdfPayload")
                if not encoded:
                    raise RuntimeError("Document/V1 response missing pdfPayload")
                body = base64.b64decode(encoded)
                if not self._is_document_body(body, "application/pdf"):
                    raise RuntimeError("Document/V1 pdfPayload is not a PDF")
                filename = response.get("dName") or name
                return body, "application/pdf", filename
            except Exception as e:
                last_error = e
                continue
        raise RuntimeError(f"no valid Mercury Document/V1 PDF payload: {last_error}")

    async def _preferred_declarations_anchor_index(self, page: Page) -> int | None:
        matches = await page.eval_on_selector_all(
            "#doc-counter-version section div.documents-container "
            "p.group-document a.desktop",
            """els => els.map(el => {
                const allAnchors = Array.from(document.querySelectorAll('a'));
                const section = el.closest('section');
                const text = (el.innerText || el.textContent || '')
                    .replace(/\\s+/g, ' ')
                    .trim();
                const rects = el.getClientRects();
                const style = window.getComputedStyle(el);
                const visible = rects.length > 0
                    && style.visibility !== 'hidden'
                    && style.display !== 'none'
                    && Number(style.opacity || '1') > 0;
                return {
                    index: allAnchors.indexOf(el),
                    text,
                    visible,
                    sectionText: (section?.innerText || '')
                        .replace(/\\s+/g, ' ')
                        .trim(),
                    pathText: (el.closest('p.group-document')?.innerText || '')
                        .replace(/\\s+/g, ' ')
                        .trim()
                };
            }).filter(item =>
                item.index >= 0
                && item.visible
                && /auto\\s+insurance\\s+policy\\s+declarations/i.test(item.text)
            )""",
        )
        if not matches:
            return None

        def score(item: dict) -> tuple[int, int]:
            text = item["text"].lower()
            section_text = item.get("sectionText", "").lower()
            value = 0
            if text == "auto insurance policy declarations":
                value += 100
            if "renewal" in section_text:
                value += 20
            if "change" in section_text:
                value -= 10
            return value, -int(item["index"])

        ordered = sorted(matches, key=score, reverse=True)
        log.info("Mercury preferred declaration candidates: %s", ordered[:5])
        return int(ordered[0]["index"])

    async def _visible_declarations_anchor_index(self, page: Page) -> int | None:
        preferred_idx = await self._preferred_declarations_anchor_index(page)
        if preferred_idx is not None:
            return preferred_idx
        return await self._declarations_anchor_index(page)

    async def _declarations_anchor_index(self, page: Page) -> int | None:
        indices = await self._declarations_anchor_indices(page)
        return indices[0] if indices else None

    async def _declarations_anchor_indices(self, page: Page) -> list[int]:
        matches = await page.eval_on_selector_all(
            "a",
            """els => els.map((el, index) => {
                const text = (el.innerText || el.textContent || '')
                    .replace(/\\s+/g, ' ')
                    .trim();
                const rects = el.getClientRects();
                const style = window.getComputedStyle(el);
                const visible = rects.length > 0
                    && style.visibility !== 'hidden'
                    && style.display !== 'none'
                    && Number(style.opacity || '1') > 0;
                return { index, text, visible };
            }).filter(item => /declarations?/i.test(item.text))""",
        )
        if not matches:
            return []

        def score(item: dict) -> tuple[int, int]:
            text = item["text"].lower()
            value = 0
            if not item["visible"]:
                return 0, -int(item["index"])
            value += 100
            if "auto insurance policy declarations" in text:
                value += 80
            elif "policy declarations" in text:
                value += 60
            elif "declarations" in text:
                value += 40
            if "change" in text or "renewal" in text:
                value -= 20
            return value, -int(item["index"])

        ordered = sorted(matches, key=score, reverse=True)
        return [int(item["index"]) for item in ordered if score(item)[0] > 0]

    async def _return_to_documents_page(self, page: Page) -> None:
        try:
            if not page.is_closed() and "/customer/mydocuments" not in page.url:
                await page.go_back(wait_until="domcontentloaded", timeout=5000)
                await self._wait_for_mercury_url(page, "/customer/mydocuments")
        except Exception:
            pass

    async def _anchor_text(self, page: Page, index: int) -> str:
        try:
            return await page.locator("a").nth(index).evaluate(
                "el => (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim()"
            )
        except Exception:
            return ""

    async def _dump_mercury_document_links(self, page: Page) -> None:
        try:
            links = await page.eval_on_selector_all(
                "a",
                """els => {
                    function sanitize(href) {
                        return (href || '').replace(
                            /([?&](?:pnToken|systemToken|token|policyNumber|termIdentifier)=)[^&]+/ig,
                            '$1...'
                        );
                    }
                    function nthOfType(el) {
                        let n = 1;
                        let prev = el.previousElementSibling;
                        while (prev) {
                            if (prev.tagName === el.tagName) n++;
                            prev = prev.previousElementSibling;
                        }
                        return `${el.tagName.toLowerCase()}:nth-of-type(${n})`;
                    }
                    function path(el) {
                        const parts = [];
                        let cur = el;
                        while (cur && cur.nodeType === 1 && parts.length < 8) {
                            if (cur.id) {
                                parts.unshift(`${cur.tagName.toLowerCase()}#${cur.id}`);
                                break;
                            }
                            const classes = Array.from(cur.classList || []).slice(0, 2);
                            parts.unshift(`${nthOfType(cur)}${classes.map(c => `.${c}`).join('')}`);
                            cur = cur.parentElement;
                        }
                        return parts.join(' > ');
                    }
                    return els.map((el, index) => ({
                        index,
                        text: (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim(),
                        aria: el.getAttribute('aria-label') || '',
                        href: sanitize(el.href || el.getAttribute('href') || ''),
                        className: String(el.className || ''),
                        visible: el.getClientRects().length > 0,
                        cssPath: path(el),
                        groupText: (el.closest('p.group-document')?.innerText || '')
                            .replace(/\\s+/g, ' ')
                            .trim()
                    })).filter(item => item.text || item.aria || item.href);
                }""",
            )
            path = DEBUG_DIR / "mercury-document-links.json"
            path.write_text(json.dumps(links[:120], indent=2))
            log.info(
                "Mercury document links debug dump -> %s (%d anchors)",
                path,
                len(links),
            )
        except Exception:
            pass
        await self._dump_debug(page, "mercury-document-links")

    async def _wait_for_mercury_url(self, page: Page, fragment: str) -> None:
        try:
            await page.wait_for_url(f"**{fragment}**", timeout=10000)
        except Exception:
            pass
        await self._settle(page, delay_ms=500, networkidle_timeout_ms=6000)

    @staticmethod
    def _is_plausible_mercury_declarations_pdf(body: bytes) -> bool:
        return len(body) <= MERCURY_MAX_DECLARATION_PDF_BYTES
