from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path

import httpx
from playwright.async_api import BrowserContext, Locator, Page

from ..models import Carrier, Document
from .base import CarrierFlow

log = logging.getLogger(__name__)

LOGIN_URL = "https://ecams.geico.com/ecams/login"
DASHBOARD_URL = "https://ecams.geico.com/"
DOCS_URL_CANDIDATES = (
    "https://ecams.geico.com/policy/documents",
    "https://ecams.geico.com/documents",
    "https://ecams.geico.com/policy/manage-documents",
    "https://ecams.geico.com/policy",
)
DEBUG_DIR = Path("/tmp")


class GeicoFlow(CarrierFlow):
    """Geico portal flow against ecams.geico.com.

    Selectors are best-effort given that Geico's React form renders inputs
    without name/id/aria. We prefer label-based locators, fall back to role,
    then to position-of-visible-input as a last resort. Each step dumps a
    screenshot + HTML to /tmp on failure for easy debugging with real creds.
    """

    carrier = Carrier.GEICO

    async def login(self, page: Page, username: str, password: str) -> None:
        log.info("geico: navigating to login URL")
        await page.goto(LOGIN_URL, wait_until="domcontentloaded")
        await self._dismiss_cookie_banner(page)

        try:
            user_field = await self._first_present(
                page.get_by_label(re.compile(r"Email|User\s*ID|Policy", re.I)),
                page.locator("input[type='text']:visible").first,
            )
            await user_field.fill(username)

            pw_field = await self._first_present(
                page.get_by_label("Password", exact=True),
                page.locator("input[type='password']:visible").first,
            )
            await pw_field.fill(password)

            # Click "Log In" — fall back to pressing Enter on password
            try:
                submit = await self._first_present(
                    page.get_by_role(
                        "button", name=re.compile(r"^\s*Log ?In\s*$", re.I)
                    ),
                    page.locator("button:has-text('Log In'):visible").first,
                )
                await submit.click()
            except Exception:
                log.warning("geico: Log In button click failed; pressing Enter")
                await pw_field.press("Enter")
        except Exception as e:
            await self._dump_debug(page, "login-failure")
            raise RuntimeError(f"Geico login form interaction failed: {e}") from e

        try:
            await page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            pass
        log.info("geico: post-login URL=%s", page.url)
        # detect explicit "invalid credentials" message
        body = (await page.locator("body").inner_text()).lower()
        if any(
            phrase in body
            for phrase in (
                "user id or password",
                "incorrect",
                "could not find an account",
                "invalid",
            )
        ) and "login" in page.url.lower():
            await self._dump_debug(page, "login-rejected")
            raise RuntimeError("Geico login rejected — check username/password")

    async def mfa_required(self, page: Page) -> bool:
        url = page.url.lower()
        if any(k in url for k in ("mfa", "otp", "verify", "two-step", "2step")):
            log.info("geico: MFA detected via URL=%s", url)
            return True
        otp = page.locator(
            "input[autocomplete='one-time-code'], input[inputmode='numeric']:visible"
        )
        if await otp.count() > 0:
            log.info("geico: MFA detected via OTP input")
            return True
        body_text = (await page.locator("body").inner_text()).lower()
        if any(
            phrase in body_text
            for phrase in (
                "verification code",
                "security code",
                "two-step verification",
                "we sent you a code",
                "enter the code",
                "one-time code",
            )
        ):
            log.info("geico: MFA detected via body text")
            return True
        return False

    async def submit_mfa(self, page: Page, code: str) -> None:
        try:
            otp_field = await self._first_present(
                page.locator("input[autocomplete='one-time-code']").first,
                page.locator("input[inputmode='numeric']:visible").first,
                page.locator("input[name*='code' i]:visible").first,
                page.locator("input[id*='code' i]:visible").first,
            )
            await otp_field.fill(code)
            try:
                submit = await self._first_present(
                    page.get_by_role(
                        "button",
                        name=re.compile(r"continue|submit|verify|log\s*in", re.I),
                    ).first,
                    page.locator("button[type='submit']:visible").first,
                )
                await submit.click()
            except Exception:
                await otp_field.press("Enter")
        except Exception as e:
            await self._dump_debug(page, "mfa-failure")
            raise RuntimeError(f"Geico MFA interaction failed: {e}") from e

        try:
            await page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            pass
        log.info("geico: post-MFA URL=%s", page.url)

    async def is_authenticated(self, page: Page) -> bool:
        # Use a tight timeout — a slow quick-path delays the fallback to full
        # login. The dashboard text scan is the real signal; we don't need
        # network-idle.
        try:
            await page.goto(
                DASHBOARD_URL, wait_until="domcontentloaded", timeout=8000
            )
        except Exception:
            return False
        url = page.url.lower()
        if "login" in url or "sign-in" in url:
            return False
        try:
            body = await page.locator("body").inner_text(timeout=3000)
        except Exception:
            return False
        body = body.lower()
        return any(s in body for s in ("log out", "sign out", "my policy", "policies"))

    async def fetch_documents(
        self,
        page: Page,
        http: httpx.AsyncClient,
        ctx: BrowserContext,
    ) -> tuple[list[Document], dict[str, bytes]]:
        landed_at = None
        for url in DOCS_URL_CANDIDATES:
            try:
                resp = await page.goto(url, wait_until="domcontentloaded", timeout=15000)
                if resp and resp.status == 200 and "login" not in page.url.lower():
                    landed_at = url
                    break
            except Exception as e:
                log.warning("geico: docs URL %s failed: %s", url, e)
                continue
        if landed_at is None:
            await self._dump_debug(page, "docs-no-url")
            raise RuntimeError("Geico: couldn't reach a documents page")
        try:
            await page.wait_for_load_state("networkidle", timeout=10000)
        except Exception:
            pass

        # Find PDF / document links on the page. We're permissive — anything
        # ending in .pdf or routed through /document(s)/ is a candidate.
        links: list[tuple[str, str]] = await page.eval_on_selector_all(
            "a[href$='.pdf'], a[href*='.pdf?'], a[href*='/document/'], "
            "a[href*='/documents/'], a[href*='dec-page'], a[href*='id-card']",
            "els => els.map(e => [e.textContent.trim().slice(0,120) || 'document', e.href])",
        )

        # Dedupe by URL, preserve order
        seen: set[str] = set()
        unique: list[tuple[str, str]] = []
        for name, href in links:
            if href and href not in seen:
                seen.add(href)
                unique.append((name, href))

        if not unique:
            await self._dump_debug(page, "docs-no-links")
            log.warning("geico: no document links found on %s", page.url)

        async def fetch(name: str, href: str, idx: int):
            try:
                r = await http.get(href)
                r.raise_for_status()
                body = r.content
                doc_id = f"doc-{idx}"
                content_type = r.headers.get("content-type", "application/pdf")
                if name.lower().endswith(".pdf"):
                    display_name = name
                elif "pdf" in content_type:
                    display_name = (name or f"document-{idx}") + ".pdf"
                else:
                    display_name = name or f"document-{idx}"
                doc = Document(
                    id=doc_id,
                    name=display_name,
                    content_type=content_type,
                    size_bytes=len(body),
                )
                return doc, body
            except Exception as e:
                log.warning("geico: failed to fetch %s: %s", href, e)
                return None

        results = await asyncio.gather(
            *[fetch(n, h, i) for i, (n, h) in enumerate(unique)]
        )
        docs: list[Document] = []
        doc_bytes: dict[str, bytes] = {}
        for r in results:
            if r is None:
                continue
            doc, body = r
            docs.append(doc)
            doc_bytes[doc.id] = body
        return docs, doc_bytes

    async def _dismiss_cookie_banner(self, page: Page) -> None:
        try:
            btn = page.locator(
                "#onetrust-reject-all-handler, #onetrust-accept-btn-handler"
            )
            if await btn.count() > 0:
                await btn.first.click(timeout=3000)
                await page.wait_for_timeout(300)
        except Exception:
            pass

    async def _dump_debug(self, page: Page, label: str) -> None:
        """Save a screenshot + page HTML to /tmp for debugging."""
        try:
            DEBUG_DIR.mkdir(exist_ok=True)
            png = DEBUG_DIR / f"geico-{label}.png"
            html = DEBUG_DIR / f"geico-{label}.html"
            await page.screenshot(path=str(png), full_page=True)
            (html).write_text(await page.content())
            log.warning("geico debug dump → %s, %s", png, html)
        except Exception as e:
            log.warning("geico: failed to dump debug artifacts: %s", e)

    @staticmethod
    async def _first_present(*locators: Locator, timeout_ms: int = 7000) -> Locator:
        """Return the first locator that resolves to a visible element within
        the total timeout. Raises Playwright TimeoutError if none do."""
        per = max(1000, timeout_ms // max(1, len(locators)))
        for loc in locators[:-1]:
            try:
                await loc.wait_for(state="visible", timeout=per)
                return loc
            except Exception:
                continue
        # last one — let it raise its own timeout if it doesn't show
        await locators[-1].wait_for(state="visible", timeout=per)
        return locators[-1]
