"""Quick check that our Geico selectors find the username/password/submit elements.

Does NOT actually submit anything — just fills the form and reports back.
Run: uv run python scripts/verify_geico_form.py
"""

import asyncio

from playwright.async_api import async_playwright

from backend.carriers.geico import LOGIN_URL, GeicoFlow


async def main() -> None:
    flow = GeicoFlow()
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context()
        page = await ctx.new_page()
        try:
            await page.goto(LOGIN_URL, wait_until="domcontentloaded")
            await flow._dismiss_cookie_banner(page)

            # Try the same selectors login() uses, but don't click submit
            import re

            user_field = await flow._first_present(
                page.get_by_label(re.compile(r"Email|User\s*ID|Policy", re.I)),
                page.locator("input[type='text']:visible").first,
            )
            await user_field.fill("placeholder-user")
            print("  ✓ filled username field")

            pw_field = await flow._first_present(
                page.get_by_label("Password", exact=True),
                page.locator("input[type='password']:visible").first,
            )
            await pw_field.fill("placeholder-password")
            print("  ✓ filled password field")

            submit = await flow._first_present(
                page.get_by_role("button", name=re.compile(r"^\s*Log ?In\s*$", re.I)),
                page.locator("button:has-text('Log In'):visible").first,
            )
            assert await submit.is_visible()
            print("  ✓ Log In button found and visible")

            print("\n✅ All selectors resolve. Geico flow is ready for live creds.")
        except Exception as e:
            print(f"\n❌ Selector failed: {e}")
            await page.screenshot(path="/tmp/geico-verify-fail.png", full_page=True)
            raise
        finally:
            await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
