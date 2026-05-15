"""Inspect the real Geico login page to capture actual form selectors.

Run with: uv run python scripts/inspect_geico.py
Outputs to /tmp/geico_inspect.json and a screenshot at /tmp/geico_login.png.
"""

import asyncio
import json
from pathlib import Path

from playwright.async_api import async_playwright


async def main() -> None:
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) "
                "AppleWebKit/605.1.15 (KHTML, like Gecko) "
                "Version/17.6 Safari/605.1.15"
            )
        )
        page = await ctx.new_page()
        result: dict = {"urls_tried": []}

        for url in (
            "https://ecams.geico.com/ecams/login",
            "https://ecams.geico.com/",
            "https://www.geico.com/login/",
        ):
            try:
                resp = await page.goto(url, wait_until="domcontentloaded", timeout=15000)
                await page.wait_for_load_state("networkidle", timeout=10000)
                result["urls_tried"].append(
                    {
                        "url": url,
                        "status": resp.status if resp else None,
                        "final_url": page.url,
                        "title": await page.title(),
                    }
                )
                if resp and resp.status == 200:
                    break
            except Exception as e:
                result["urls_tried"].append({"url": url, "error": str(e)})

        result["final_url"] = page.url
        result["inputs"] = await page.eval_on_selector_all(
            "input",
            """els => els.map(e => ({
                type: e.type,
                name: e.name,
                id: e.id,
                placeholder: e.placeholder,
                aria_label: e.getAttribute('aria-label'),
                visible: e.offsetParent !== null,
            }))""",
        )
        result["buttons"] = await page.eval_on_selector_all(
            "button",
            """els => els.map(e => ({
                type: e.type,
                text: (e.innerText || '').trim().slice(0, 80),
                id: e.id,
                name: e.name,
                aria_label: e.getAttribute('aria-label'),
                visible: e.offsetParent !== null,
            }))""",
        )
        result["forms"] = await page.eval_on_selector_all(
            "form",
            """els => els.map(e => ({ action: e.action, method: e.method, id: e.id, name: e.name }))""",
        )
        result["labels"] = await page.eval_on_selector_all(
            "label",
            """els => els.map(e => ({ text: (e.innerText || '').trim().slice(0, 80), for: e.htmlFor }))""",
        )

        Path("/tmp/geico_inspect.json").write_text(json.dumps(result, indent=2))
        await page.screenshot(path="/tmp/geico_login.png", full_page=True)
        await browser.close()

        print(json.dumps(result, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
