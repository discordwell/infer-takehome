"""Try USAA login with installed Chrome and a persistent profile."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

from dotenv import load_dotenv
from playwright.async_api import async_playwright

from backend.carriers.usaa import STEALTH_INIT_SCRIPT, USAA_USER_AGENT

load_dotenv()

OUT = Path("/tmp/usaa_chrome_inspect.json")
PROFILE = Path("/tmp/usaa_chrome_profile")


async def main() -> None:
    username = os.environ.get("USAA_USERNAME")
    if not username:
        raise SystemExit("Set USAA_USERNAME in .env first")

    async with async_playwright() as p:
        ctx = await p.chromium.launch_persistent_context(
            str(PROFILE),
            channel="chrome",
            headless=False,
            slow_mo=80,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-features=IsolateOrigins,site-per-process",
            ],
            user_agent=USAA_USER_AGENT,
            viewport={"width": 1280, "height": 800},
            locale="en-US",
            timezone_id="America/New_York",
            extra_http_headers={
                "Accept-Language": "en-US,en;q=0.9",
                "Accept": (
                    "text/html,application/xhtml+xml,application/xml;q=0.9,"
                    "image/avif,image/webp,*/*;q=0.8"
                ),
            },
        )
        await ctx.add_init_script(STEALTH_INIT_SCRIPT)
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        result: dict = {}
        try:
            resp = await page.goto(
                "https://www.usaa.com/my/logon",
                wait_until="domcontentloaded",
                timeout=30000,
            )
            try:
                await page.wait_for_load_state("networkidle", timeout=10000)
            except Exception:
                pass
            await page.wait_for_timeout(1000)
            await page.locator("input[name='memberId']:visible").fill(username)
            await page.locator("#next-button:visible").click()
            await page.wait_for_timeout(6000)
            result = {
                "status": resp.status if resp else None,
                "url": page.url,
                "title": await page.title(),
                "body": (await page.locator("body").inner_text(timeout=3000))[:4000],
                "inputs": await page.eval_on_selector_all(
                    "input",
                    """els => els.map(e => ({
                        type: e.type,
                        name: e.name,
                        autocomplete: e.autocomplete,
                        visible: !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length),
                    }))""",
                ),
                "buttons": await page.eval_on_selector_all(
                    "button",
                    """els => els.map(e => ({
                        text: (e.innerText || e.textContent || '').trim().slice(0, 120),
                        type: e.type,
                        id: e.id,
                        visible: !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length),
                    }))""",
                ),
            }
            await page.screenshot(path="/tmp/usaa_chrome_inspect.png", full_page=True)
        finally:
            OUT.write_text(json.dumps(result, indent=2))
            await ctx.close()
    print(f"Wrote {OUT}")


if __name__ == "__main__":
    asyncio.run(main())
