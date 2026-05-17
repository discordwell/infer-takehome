"""Dump the USAA page immediately after credential submission.

Run:
  uv run python -m scripts.inspect_usaa_after_login

Writes /tmp/usaa_after_login.json and /tmp/usaa_after_login.png.
"""

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
            login_error = None
            try:
                await flow.login(page, username, password)
            except Exception as e:  # noqa: BLE001 - inspection should still dump page state
                login_error = str(e)
            result = {
                "url": page.url,
                "title": await page.title(),
                "login_error": login_error,
                "mfa_required": await flow.mfa_required(page),
                "body": (await flow._body_text(page))[:4000],
                "inputs": await page.eval_on_selector_all(
                    "input",
                    """els => els.map(e => ({
                        type: e.type,
                        name: e.name,
                        id: e.id,
                        aria: e.getAttribute('aria-label'),
                        autocomplete: e.autocomplete,
                        visible: !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length),
                    }))""",
                ),
                "buttons": await page.eval_on_selector_all(
                    "button, [role='button'], input[type=submit]",
                    """els => els.map(e => ({
                        tag: e.tagName,
                        type: e.type || e.getAttribute('type'),
                        text: (e.innerText || e.value || e.textContent || '').trim().slice(0, 120),
                        id: e.id,
                        aria: e.getAttribute('aria-label'),
                        visible: !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length),
                    }))""",
                ),
                "links": await page.eval_on_selector_all(
                    "a[href]",
                    """els => els.map(e => ({
                        text: (e.innerText || e.textContent || '').trim().slice(0, 120),
                        href: e.href,
                        visible: !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length),
                    }))""",
                ),
            }
            await page.screenshot(path="/tmp/usaa_after_login.png", full_page=True)
            Path("/tmp/usaa_after_login.json").write_text(json.dumps(result, indent=2))
            print("Wrote /tmp/usaa_after_login.json and /tmp/usaa_after_login.png")
    finally:
        await runner.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
