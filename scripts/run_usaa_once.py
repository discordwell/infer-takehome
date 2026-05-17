"""Run the USAA flow once from the command line.

Useful while iterating on USAA's bot-sensitive login. Reads credentials from
.env and accepts the MFA code from USAA_MFA_CODE or stdin. Writes downloaded
documents to /tmp/usaa_docs for inspection.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

from backend import storage
from backend.carriers.usaa import UsaaFlow
from backend.models import Carrier
from backend.playwright_runner import http_from_context, runner

load_dotenv(".env")


async def main() -> None:
    username = os.environ.get("USAA_USERNAME")
    password = os.environ.get("USAA_PASSWORD")
    if not username or not password:
        raise SystemExit("Set USAA_USERNAME and USAA_PASSWORD in .env first")

    flow = UsaaFlow()
    await runner.start()
    try:
        stored_state = storage.load(Carrier.USAA.value, username)
        context_options = flow.context_options()
        async with runner.new_context(
            storage_state=stored_state, **context_options
        ) as ctx:
            page = await ctx.new_page()

            if stored_state is not None:
                flow.reset_timings()
                flow.mark_timing("quick_path_start")
                http = await http_from_context(
                    ctx, user_agent=context_options.get("user_agent")
                )
                try:
                    docs, doc_bytes = await flow.fetch_documents(page, http, ctx)
                    storage.save(
                        Carrier.USAA.value, username, await ctx.storage_state()
                    )
                    flow.mark_timing("docs_ready")
                    _write_docs(docs, doc_bytes)
                    _print_timings(flow)
                    return
                except Exception as e:
                    print(f"Stored session did not fetch docs; falling back to login: {e}")
                finally:
                    await http.aclose()

            await flow.login(page, username, password)

            if await flow.mfa_required(page):
                code = os.environ.get("USAA_MFA_CODE")
                if not code:
                    print("\n>> Enter the USAA MFA code: ", end="", flush=True)
                    code = sys.stdin.readline().strip()
                flow.reset_timings()
                flow.mark_timing("mfa_code_received")
                await flow.submit_mfa(page, code)
                flow.mark_timing("mfa_submit_returned")
            else:
                flow.reset_timings()
                flow.mark_timing("no_mfa_fetch_start")

            http = await http_from_context(
                ctx, user_agent=context_options.get("user_agent")
            )
            try:
                docs, doc_bytes = await flow.fetch_documents(page, http, ctx)
            finally:
                await http.aclose()

            storage.save(Carrier.USAA.value, username, await ctx.storage_state())
            flow.mark_timing("docs_ready")
            _write_docs(docs, doc_bytes)
            _print_timings(flow)
    finally:
        await runner.shutdown()


def _write_docs(docs, doc_bytes) -> None:
    out_dir = Path("/tmp/usaa_docs")
    out_dir.mkdir(exist_ok=True)
    print(f"Got {len(docs)} documents")
    for doc in docs:
        suffix = ".pdf" if "pdf" in doc.content_type.lower() else ".bin"
        path = out_dir / f"{doc.id}{suffix}"
        path.write_bytes(doc_bytes[doc.id])
        print(f"{doc.id}: {doc.size_bytes} bytes {doc.content_type} -> {path}")


def _print_timings(flow: UsaaFlow) -> None:
    report = flow.timing_report()
    if report:
        print(f"Timing: {report}")


if __name__ == "__main__":
    asyncio.run(main())
