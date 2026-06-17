"""Step past the USAA username page to capture the password-page selectors.

Fills memberId with the env-saved username, clicks Next, then dumps the next
page's form. Does NOT submit the password.

Run: uv run python -m scripts.inspect_usaa_password
"""

import asyncio
import json
import os
from pathlib import Path

from dotenv import load_dotenv
from playwright.async_api import async_playwright

load_dotenv()

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/130.0.0.0 Safari/537.36"
)

STEALTH_INIT_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
Object.defineProperty(navigator, 'plugins', { get: () => [1,2,3,4,5].map(()=>({}))});
Object.defineProperty(navigator, 'languages', { get: () => ['en-US','en'] });
Object.defineProperty(navigator, 'platform', { get: () => 'MacIntel' });
window.chrome = { runtime: {} };
"""


async def main() -> None:
    username = os.environ.get("USAA_USERNAME")
    if not username:
        raise SystemExit("Set USAA_USERNAME in .env first")

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=False,
            args=["--disable-blink-features=AutomationControlled"],
        )
        ctx = await browser.new_context(
            user_agent=USER_AGENT,
            viewport={"width": 1280, "height": 800},
            locale="en-US",
            timezone_id="America/New_York",
        )
        await ctx.add_init_script(STEALTH_INIT_SCRIPT)
        page = await ctx.new_page()

        await page.goto("https://www.usaa.com/my/logon", wait_until="domcontentloaded", timeout=20000)
        try:
            await page.wait_for_load_state("networkidle", timeout=10000)
        except Exception:
            pass
        print(f"USERNAME PAGE: {page.url}")
        try:
            await page.screenshot(path="/tmp/usaa_step1_username.png", full_page=True, timeout=5000)
        except Exception:
            pass

        # Fill memberId and click Next
        await page.locator("input[name='memberId']").fill(username)
        await page.locator("#next-button").click()

        # Wait for the password page to render
        try:
            await page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            pass
        # Give React a moment if the URL didn't change
        await page.wait_for_timeout(2500)

        print(f"PASSWORD PAGE: {page.url}")
        try:
            await page.screenshot(path="/tmp/usaa_step2_password.png", full_page=True, timeout=5000)
        except Exception:
            pass

        inputs = await page.eval_on_selector_all(
            "input",
            """els => els.map(e => ({
                type: e.type, name: e.name, id: e.id,
                placeholder: e.placeholder,
                aria_label: e.getAttribute('aria-label'),
                autocomplete: e.autocomplete,
                visible: e.offsetParent !== null,
            }))""",
        )
        buttons = await page.eval_on_selector_all(
            "button, input[type=submit]",
            """els => els.map(e => ({
                tag: e.tagName, type: e.type || null,
                text: (e.innerText || e.value || '').trim().slice(0, 80),
                id: e.id, name: e.name,
                aria_label: e.getAttribute('aria-label'),
                visible: e.offsetParent !== null,
            }))""",
        )
        labels = await page.eval_on_selector_all(
            "label",
            """els => els.map(e => ({ text: (e.innerText || '').trim().slice(0, 80), for: e.htmlFor }))""",
        )
        forms = await page.eval_on_selector_all(
            "form",
            """els => els.map(e => ({ action: e.action, method: e.method, id: e.id }))""",
        )
        result = {
            "url": page.url,
            "visible_inputs": [i for i in inputs if i["visible"]],
            "visible_buttons": [b for b in buttons if b["visible"]],
            "labels": [lbl for lbl in labels if lbl["text"]],
            "forms": forms,
        }
        Path("/tmp/usaa_password_page.json").write_text(json.dumps(result, indent=2))
        print(json.dumps(result, indent=2))

        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
