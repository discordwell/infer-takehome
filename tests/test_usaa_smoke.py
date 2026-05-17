"""Smoke test for the live USAA flow.

Requires RUN_LIVE_SMOKE=1 and real credentials in .env or env:
  USAA_USERNAME=...
  USAA_PASSWORD=...

This test prompts for MFA on stdin if USAA asks for a code. It hits the real
carrier portal and is skipped automatically when credentials are missing.

Run explicitly:
  RUN_LIVE_SMOKE=1 uv run pytest tests/test_usaa_smoke.py -v -s
"""

import os
import sys

import pytest
from dotenv import load_dotenv

from backend.carriers.usaa import UsaaFlow
from backend.models import Document
from backend.playwright_runner import http_from_context, runner

load_dotenv()

pytestmark = pytest.mark.skipif(
    not (
        os.getenv("RUN_LIVE_SMOKE") == "1"
        and os.getenv("USAA_USERNAME")
        and os.getenv("USAA_PASSWORD")
    ),
    reason="RUN_LIVE_SMOKE=1, USAA_USERNAME, and USAA_PASSWORD must be set",
)


async def _prompt_for_mfa() -> str:
    print("\n>> Enter the MFA code USAA sent you: ", end="", flush=True)
    return sys.stdin.readline().strip()


async def test_usaa_login_and_fetch_docs():
    flow = UsaaFlow()
    username = os.environ["USAA_USERNAME"]
    password = os.environ["USAA_PASSWORD"]

    await runner.start()
    try:
        async with runner.new_context(**flow.context_options()) as ctx:
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

            print(f"\nGot {len(docs)} documents from USAA")
            for d in docs:
                print(f"  - {d.id} ({d.size_bytes} bytes, {d.content_type})")

            assert len(docs) >= 1, "expected at least one document from USAA"
            for d in docs:
                assert isinstance(d, Document)
                body = doc_bytes[d.id]
                assert len(body) > 0
                assert "html" not in d.content_type.lower()
                if "pdf" in d.content_type.lower():
                    assert body.startswith(b"%PDF"), f"{d.id} not a valid PDF"
    finally:
        await runner.shutdown()
