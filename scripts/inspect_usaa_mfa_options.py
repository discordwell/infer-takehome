"""Inspect USAA MFA alternate delivery options after login."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

from dotenv import load_dotenv

from backend.carriers.usaa import UsaaFlow
from backend.playwright_runner import runner

load_dotenv()


async def main() -> None:
    username = os.environ.get("USAA_USERNAME")
    password = os.environ.get("USAA_PASSWORD")
    if not username or not password:
        raise SystemExit("Set USAA_USERNAME and USAA_PASSWORD in .env first")

    flow = UsaaFlow()
    await runner.start()
    try:
        async with runner.new_context(**flow.context_options()) as ctx:
            page = await ctx.new_page()
            await flow.login(page, username, password)
            try:
                await page.get_by_text("I need a different option", exact=False).click(
                    timeout=5000
                )
                await page.wait_for_timeout(2500)
            except Exception as e:
                print(f"alternate-option click failed: {e}")
            result = {
                "url": page.url,
                "title": await page.title(),
                "body": (await flow._body_text(page))[:5000],
                "inputs": await page.eval_on_selector_all(
                    "input",
                    """els => els.map(e => ({
                        type: e.type,
                        name: e.name,
                        id: e.id,
                        value: e.value,
                        checked: e.checked,
                        aria: e.getAttribute('aria-label'),
                        autocomplete: e.autocomplete,
                        visible: !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length),
                    }))""",
                ),
                "buttons": await page.eval_on_selector_all(
                    "button, [role='button'], a[href]",
                    """els => els.map(e => ({
                        tag: e.tagName,
                        role: e.getAttribute('role'),
                        type: e.type || e.getAttribute('type'),
                        text: (e.innerText || e.value || e.textContent || '').trim().slice(0, 140),
                        id: e.id,
                        aria: e.getAttribute('aria-label'),
                        href: e.href || e.getAttribute('href') || '',
                        visible: !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length),
                    }))""",
                ),
            }
            await page.screenshot(path="/tmp/usaa_mfa_options.png", full_page=True)
            Path("/tmp/usaa_mfa_options.json").write_text(json.dumps(result, indent=2))
            print("Wrote /tmp/usaa_mfa_options.json and /tmp/usaa_mfa_options.png")
    finally:
        await runner.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
