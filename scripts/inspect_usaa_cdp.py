"""Try USAA with a Chrome process launched outside Playwright.

This starts Google Chrome with a remote debugging port, then uses Playwright
over CDP. The browser is not launched through Playwright's normal automation
path, which helps separate Akamai/browser-fingerprint failures from selector
failures.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
from pathlib import Path

from dotenv import load_dotenv
from playwright.async_api import async_playwright

load_dotenv()

CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
PROFILE = Path("/tmp/usaa_cdp_chrome_profile")
OUT = Path("/tmp/usaa_cdp_inspect.json")


async def main() -> None:
    username = os.environ.get("USAA_USERNAME")
    if not username:
        raise SystemExit("Set USAA_USERNAME in .env first")

    shutil.rmtree(PROFILE, ignore_errors=True)
    proc = subprocess.Popen(
        [
            CHROME,
            "--remote-debugging-port=9222",
            f"--user-data-dir={PROFILE}",
            "--no-first-run",
            "--no-default-browser-check",
            "about:blank",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    result: dict = {}
    try:
        await asyncio.sleep(3)
        async with async_playwright() as p:
            browser = await p.chromium.connect_over_cdp("http://127.0.0.1:9222")
            ctx = browser.contexts[0]
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            resp = await page.goto(
                "https://www.usaa.com/my/logon",
                wait_until="domcontentloaded",
                timeout=30000,
            )
            try:
                await page.wait_for_load_state("networkidle", timeout=10000)
            except Exception:
                pass
            await page.wait_for_timeout(1500)
            await page.locator("input[name='memberId']:visible").click()
            await page.keyboard.type(username, delay=70)
            await page.locator("#next-button:visible").click()
            await page.wait_for_timeout(25000)
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
            try:
                await page.screenshot(
                    path="/tmp/usaa_cdp_inspect.png", full_page=True, timeout=5000
                )
            except Exception:
                pass
            await browser.close()
    finally:
        OUT.write_text(json.dumps(result, indent=2))
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()

    print(f"Wrote {OUT}")


if __name__ == "__main__":
    asyncio.run(main())
