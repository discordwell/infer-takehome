"""Smoke test for the live Geico flow.

Requires RUN_LIVE_SMOKE=1 and real credentials in .env or env:
  GEICO_USERNAME=...
  GEICO_PASSWORD=...

This test prompts you for the MFA code on stdin (Geico's portal SMS/email
arrives a few seconds after login). It asserts at least one document is
returned. Skipped automatically when creds aren't set.

Run explicitly:
  RUN_LIVE_SMOKE=1 uv run pytest tests/test_geico_smoke.py -v -s
"""

import os
import sys

import pytest
from dotenv import load_dotenv

from backend.carriers.geico import GeicoFlow
from backend.models import Document
from backend.playwright_runner import http_from_context, runner

load_dotenv()


pytestmark = pytest.mark.skipif(
    not (
        os.getenv("RUN_LIVE_SMOKE") == "1"
        and os.getenv("GEICO_USERNAME")
        and os.getenv("GEICO_PASSWORD")
    ),
    reason="RUN_LIVE_SMOKE=1, GEICO_USERNAME, and GEICO_PASSWORD must be set",
)


async def _prompt_for_mfa() -> str:
    print("\n>> Enter the MFA code Geico sent you: ", end="", flush=True)
    return sys.stdin.readline().strip()


async def test_geico_login_and_fetch_docs():
    """Walk through the real Geico flow against ecams.geico.com.

    Not an isolated unit test — it hits the real carrier. Run manually.
    """
    flow = GeicoFlow()
    username = os.environ["GEICO_USERNAME"]
    password = os.environ["GEICO_PASSWORD"]

    await runner.start()
    try:
        async with runner.new_context() as ctx:
            page = await ctx.new_page()
            await flow.login(page, username, password)

            if await flow.mfa_required(page):
                code = await _prompt_for_mfa()
                await flow.submit_mfa(page, code)

            http = await http_from_context(ctx)
            try:
                docs, doc_bytes = await flow.fetch_documents(page, http, ctx)
            finally:
                await http.aclose()

            print(f"\nGot {len(docs)} documents:")
            for d in docs:
                print(f"  - {d.name} ({d.size_bytes} bytes, {d.content_type})")

            assert len(docs) >= 1, "expected at least one document from Geico"
            for d in docs:
                assert isinstance(d, Document)
                body = doc_bytes[d.id]
                assert len(body) > 0
                # most are PDFs; allow other content types but log
                if d.content_type == "application/pdf":
                    assert body.startswith(b"%PDF"), f"{d.name} not a valid PDF"
    finally:
        await runner.shutdown()
