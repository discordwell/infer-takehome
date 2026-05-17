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

from backend.carriers.usaa import UsaaFlow
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
        async with runner.new_context(**flow.context_options()) as ctx:
            page = await ctx.new_page()
            await flow.login(page, username, password)

            if await flow.mfa_required(page):
                code = os.environ.get("USAA_MFA_CODE")
                if not code:
                    print("\n>> Enter the USAA MFA code: ", end="", flush=True)
                    code = sys.stdin.readline().strip()
                await flow.submit_mfa(page, code)

            http = await http_from_context(
                ctx, user_agent=flow.context_options().get("user_agent")
            )
            try:
                docs, doc_bytes = await flow.fetch_documents(page, http, ctx)
            finally:
                await http.aclose()

            out_dir = Path("/tmp/usaa_docs")
            out_dir.mkdir(exist_ok=True)
            print(f"Got {len(docs)} documents")
            for doc in docs:
                suffix = ".pdf" if "pdf" in doc.content_type.lower() else ".bin"
                path = out_dir / f"{doc.id}{suffix}"
                path.write_bytes(doc_bytes[doc.id])
                print(f"{doc.id}: {doc.size_bytes} bytes {doc.content_type} -> {path}")
    finally:
        await runner.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
