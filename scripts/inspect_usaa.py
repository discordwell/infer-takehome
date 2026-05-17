"""Inspect the real USAA login page, trying stealth flags since USAA blocks
plain headless Chromium at the TLS layer.

Run: uv run python -m scripts.inspect_usaa
Outputs /tmp/usaa_inspect.json and a screenshot.
"""

import asyncio
import json
import sys
from pathlib import Path

from playwright.async_api import async_playwright

HEADLESS = "--headed" not in sys.argv

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/130.0.0.0 Safari/537.36"
)

STEALTH_INIT_SCRIPT = """
// Hide webdriver flag
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });

// Make plugins look real
Object.defineProperty(navigator, 'plugins', {
  get: () => [1, 2, 3, 4, 5].map(() => ({}))
});

// Languages
Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });

// Spoof platform
Object.defineProperty(navigator, 'platform', { get: () => 'MacIntel' });

// chrome runtime
window.chrome = { runtime: {} };

// permissions
const origQuery = navigator.permissions ? navigator.permissions.query : null;
if (origQuery) {
  navigator.permissions.query = (params) =>
    params.name === 'notifications'
      ? Promise.resolve({ state: Notification.permission })
      : origQuery(params);
}
"""


async def try_with_settings(
    *,
    headless: bool,
    extra_args: list[str] | None = None,
    label: str,
) -> dict:
    extra_args = extra_args or []
    print(f"\n=== Attempt: {label} (headless={headless}, args={extra_args}) ===")
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=headless,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-features=IsolateOrigins,site-per-process",
                *extra_args,
            ],
        )
        ctx = await browser.new_context(
            user_agent=USER_AGENT,
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
        page = await ctx.new_page()

        urls = [
            "https://www.usaa.com/my/logon",
            "https://www.usaa.com/inet/ent_logon/Logon",
            "https://www.usaa.com/",
        ]
        result: dict = {"label": label, "urls_tried": []}
        for url in urls:
            try:
                resp = await page.goto(url, wait_until="domcontentloaded", timeout=20000)
                try:
                    await page.wait_for_load_state("networkidle", timeout=10000)
                except Exception:
                    pass
                result["urls_tried"].append(
                    {
                        "url": url,
                        "status": resp.status if resp else None,
                        "final_url": page.url,
                        "title": await page.title(),
                    }
                )
                print(f"  {url} → status={resp.status if resp else 'noresp'} final={page.url}")
                if resp and resp.status == 200 and "usaa.com" in page.url:
                    break
            except Exception as e:
                result["urls_tried"].append({"url": url, "error": str(e)[:200]})
                print(f"  {url} → ERROR: {str(e)[:120]}")

        result["final_url"] = page.url
        if "chrome-error" not in page.url:
            try:
                result["inputs"] = await page.eval_on_selector_all(
                    "input",
                    """els => els.map(e => ({
                        type: e.type, name: e.name, id: e.id,
                        placeholder: e.placeholder,
                        aria_label: e.getAttribute('aria-label'),
                        visible: e.offsetParent !== null,
                    }))""",
                )
                result["buttons"] = await page.eval_on_selector_all(
                    "button, input[type=submit], [role=button]",
                    """els => els.map(e => ({
                        tag: e.tagName, type: e.type || null,
                        text: (e.innerText || e.value || '').trim().slice(0, 80),
                        id: e.id, name: e.name,
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
                await page.screenshot(path=f"/tmp/usaa_{label}.png", full_page=True)
            except Exception as e:
                result["dom_extract_error"] = str(e)[:200]
        await browser.close()
        return result


async def main() -> None:
    attempts = [
        ("headless_stealth", {"headless": True, "extra_args": []}),
    ]
    if not HEADLESS:
        attempts.append(("headed_stealth", {"headless": False, "extra_args": []}))

    results = []
    for label, kwargs in attempts:
        r = await try_with_settings(label=label, **kwargs)
        results.append(r)

    Path("/tmp/usaa_inspect.json").write_text(json.dumps(results, indent=2))

    # summarize the first successful attempt
    for r in results:
        if r.get("final_url") and "chrome-error" not in r["final_url"]:
            print(f"\n=== SUCCESS: {r['label']} ===")
            print(f"Final URL: {r['final_url']}")
            print("Visible inputs:")
            for i in r.get("inputs", []):
                if i["visible"]:
                    print(f"  {i}")
            print("Visible buttons:")
            for b in r.get("buttons", []):
                if b["visible"]:
                    print(f"  {b}")
            return

    print("\n=== All attempts blocked. Try --headed.")


if __name__ == "__main__":
    asyncio.run(main())
