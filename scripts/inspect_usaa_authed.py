"""Inspect authenticated USAA pages and candidate document links.

Run:
  uv run python -m scripts.inspect_usaa_authed

This uses USAA_USERNAME / USAA_PASSWORD from .env, prompts for MFA if needed,
and writes portal/link metadata to /tmp/usaa_authed_inspect.json. It does not
print credentials or document contents.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from urllib.parse import urljoin

from dotenv import load_dotenv

from backend.carriers.usaa import DOCS_URL_CANDIDATES, UsaaFlow
from backend.playwright_runner import http_from_context, runner

load_dotenv()

OUT = Path("/tmp/usaa_authed_inspect.json")
SCREENSHOT_DIR = Path("/tmp/usaa_authed_pages")


async def _prompt_for_mfa() -> str:
    print("\n>> Enter the MFA code USAA sent you: ", end="", flush=True)
    return sys.stdin.readline().strip()


async def main() -> None:
    username = os.environ.get("USAA_USERNAME")
    password = os.environ.get("USAA_PASSWORD")
    if not username or not password:
        raise SystemExit("Set USAA_USERNAME and USAA_PASSWORD in .env first")

    flow = UsaaFlow()
    SCREENSHOT_DIR.mkdir(exist_ok=True)
    results: list[dict] = []

    await runner.start()
    try:
        async with runner.new_context(**flow.context_options()) as ctx:
            page = await ctx.new_page()
            await flow.login(page, username, password)
            if await flow.mfa_required(page):
                await flow.submit_mfa(page, await _prompt_for_mfa())

            http = await http_from_context(
                ctx, user_agent=flow.context_options().get("user_agent")
            )
            try:
                for idx, url in enumerate(DOCS_URL_CANDIDATES):
                    page_result: dict = {"candidate_url": url}
                    try:
                        resp = await page.goto(
                            url, wait_until="domcontentloaded", timeout=20000
                        )
                        try:
                            await page.wait_for_load_state("networkidle", timeout=10000)
                        except Exception:
                            pass
                        await page.wait_for_timeout(1500)
                        page_result.update(
                            {
                                "status": resp.status if resp else None,
                                "final_url": page.url,
                                "title": await page.title(),
                            }
                        )
                        shot = SCREENSHOT_DIR / f"page_{idx}.png"
                        await page.screenshot(path=str(shot), full_page=True)
                        page_result["screenshot"] = str(shot)

                        links = await page.eval_on_selector_all(
                            "a[href], button, [role='button'], [role='link']",
                            """els => els.map((e, i) => ({
                                index: i,
                                tag: e.tagName,
                                role: e.getAttribute('role'),
                                type: e.getAttribute('type'),
                                text: (e.innerText || e.textContent || '').trim().slice(0, 120),
                                href: e.href || e.getAttribute('href') || e.getAttribute('data-href') || '',
                                id: e.id,
                                name: e.getAttribute('name'),
                                aria: e.getAttribute('aria-label'),
                                visible: !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length),
                            }))""",
                        )
                        page_result["elements"] = [
                            item
                            for item in links
                            if item["visible"] and (item["text"] or item["href"] or item["aria"])
                        ]

                        hrefs = []
                        seen: set[str] = set()
                        for item in page_result["elements"]:
                            href = item.get("href") or ""
                            if not href:
                                continue
                            absolute = urljoin(page.url, href)
                            if absolute in seen:
                                continue
                            seen.add(absolute)
                            if any(
                                token in (absolute + " " + item.get("text", "")).lower()
                                for token in (
                                    "pdf",
                                    "document",
                                    "policy",
                                    "declaration",
                                    "id-card",
                                    "id card",
                                    "insurance card",
                                )
                            ):
                                hrefs.append(
                                    {
                                        "text": item.get("text", ""),
                                        "url": absolute,
                                    }
                                )
                        for href in hrefs:
                            try:
                                r = await http.get(href["url"])
                                href["fetch_status"] = r.status_code
                                href["content_type"] = r.headers.get("content-type")
                                href["content_length"] = len(r.content)
                                href["starts_pdf"] = r.content.startswith(b"%PDF")
                            except Exception as e:
                                href["fetch_error"] = str(e)[:200]
                        page_result["href_candidates"] = hrefs
                    except Exception as e:
                        page_result["error"] = str(e)[:300]
                    results.append(page_result)
            finally:
                await http.aclose()
    finally:
        await runner.shutdown()

    OUT.write_text(json.dumps(results, indent=2))
    print(f"Wrote {OUT}")


if __name__ == "__main__":
    asyncio.run(main())
