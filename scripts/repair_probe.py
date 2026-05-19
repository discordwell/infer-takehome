"""Auto-claude's eyes on a carrier site.

Two inspection modes:

  storage-state mode (fresh logged-in browser):
    uv run python -m scripts.repair_probe \\
        --storage-state storage/repair/<session>/auth_state.json \\
        --url https://carrier.example.com/docs

  CDP attach mode (live failed session):
    uv run python -m scripts.repair_probe \\
        --cdp-endpoint http://127.0.0.1:9222

Writes screenshot + full DOM to --out-dir and prints a JSON summary to stdout.

Used by the in-container auto-repair claude to inspect what changed when a
carrier adapter breaks, without needing real user credentials.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from playwright.async_api import async_playwright

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "Version/17.6 Safari/605.1.15"
)


async def _capture_page(page, out_dir: Path, console_logs: list[str]) -> dict[str, Any]:
    try:
        await page.wait_for_load_state("networkidle", timeout=8000)
    except Exception:
        pass
    screenshot_path = out_dir / "probe_screenshot.png"
    try:
        await page.screenshot(path=str(screenshot_path), full_page=True)
    except Exception as e:
        screenshot_path = None  # type: ignore[assignment]
        console_logs.append(f"[probe] screenshot failed: {e}")
    dom = await page.content()
    dom_path = out_dir / "probe_dom.html"
    dom_path.write_text(dom)
    return {
        "url": page.url,
        "title": await page.title(),
        "dom_snippet": dom[:10_000],
        "dom_full_path": str(dom_path),
        "screenshot_path": str(screenshot_path) if screenshot_path else None,
        "console_logs": console_logs[-50:],
    }


async def probe_storage_state(state_path: Path, url: str, out_dir: Path) -> dict[str, Any]:
    state = json.loads(state_path.read_text())
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"],
        )
        try:
            context = await browser.new_context(
                storage_state=state,
                user_agent=USER_AGENT,
                viewport={"width": 1280, "height": 800},
            )
            page = await context.new_page()
            console_logs: list[str] = []
            page.on(
                "console", lambda m: console_logs.append(f"[{m.type}] {m.text}")
            )
            await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            captured = await _capture_page(page, out_dir, console_logs)
            return {"mode": "storage_state", **captured}
        finally:
            await browser.close()


async def probe_cdp(cdp_endpoint: str, out_dir: Path) -> dict[str, Any]:
    async with async_playwright() as pw:
        browser = await pw.chromium.connect_over_cdp(cdp_endpoint)
        try:
            if not browser.contexts:
                return {"mode": "cdp", "error": "no contexts on CDP browser"}
            ctx = browser.contexts[0]
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            console_logs: list[str] = []
            page.on(
                "console", lambda m: console_logs.append(f"[{m.type}] {m.text}")
            )
            captured = await _capture_page(page, out_dir, console_logs)
            return {"mode": "cdp", **captured}
        finally:
            await browser.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--storage-state", type=Path, help="Path to storage_state JSON")
    parser.add_argument("--url", help="URL to navigate to (storage-state mode)")
    parser.add_argument("--cdp-endpoint", help="CDP endpoint URL")
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=Path("storage/repair/probe_output"),
        help="Where to write screenshot + DOM",
    )
    args = parser.parse_args()

    args.out_dir.mkdir(parents=True, exist_ok=True)

    if args.cdp_endpoint:
        result = asyncio.run(probe_cdp(args.cdp_endpoint, args.out_dir))
    elif args.storage_state and args.url:
        result = asyncio.run(
            probe_storage_state(args.storage_state, args.url, args.out_dir)
        )
    else:
        parser.error(
            "must provide either --cdp-endpoint OR (--storage-state and --url)"
        )

    json.dump(result, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
