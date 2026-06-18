"""Mock carrier flow for testing the full stack without real credentials.

Enable by setting `CARRIER_MOCK=true` (or `1`/`yes`/`on`) in your environment.
The registry substitutes this for the live carriers. Useful for:

- End-to-end testing of FastAPI + SSE + frontend without a network roundtrip
- Latency measurement of the framework code (Playwright is ~1.5s of the budget;
  the mock collapses that to ~0.2s so you can see the rest of the overhead)
- Reproducing edge cases: wrong password (MOCK_BAD_PASSWORD=1), MFA timeout,
  no-MFA path (MOCK_SKIP_MFA=1)

These flags are read at call time via `env_flags.env_truthy` so the test suite
and demo can flip them per process; any of `1/true/yes/on` count as set.
"""

from __future__ import annotations

import asyncio

import httpx
from playwright.async_api import BrowserContext, Page

from ..env_flags import env_truthy
from ..models import Carrier, Document
from .base import CarrierFlow

# A 1-page valid PDF, smallest legal form.
MOCK_PDF = (
    b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
    b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
    b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 200 200]/Contents 4 0 R"
    b"/Resources<</Font<</F1<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>>>>>>>endobj\n"
    b"4 0 obj<</Length 44>>stream\nBT /F1 12 Tf 50 100 Td (Mock policy doc) Tj ET\nendstream endobj\n"
    b"xref\n0 5\n0000000000 65535 f \n0000000009 00000 n \n0000000054 00000 n \n"
    b"0000000099 00000 n \n0000000201 00000 n \ntrailer<</Size 5/Root 1 0 R>>\n"
    b"startxref\n290\n%%EOF\n"
)


class MockFlow(CarrierFlow):
    def __init__(self, carrier: Carrier = Carrier.GEICO) -> None:
        self.carrier = carrier

    async def login(self, page: Page, username: str, password: str) -> None:
        await asyncio.sleep(0.4)
        if env_truthy("MOCK_BAD_PASSWORD"):
            raise RuntimeError("Invalid username or password")
        self._username = username

    async def mfa_required(self, page: Page) -> bool:
        return not env_truthy("MOCK_SKIP_MFA")

    async def submit_mfa(self, page: Page, code: str) -> None:
        await asyncio.sleep(0.2)
        if env_truthy("MOCK_BAD_MFA"):
            raise RuntimeError("Invalid verification code")

    async def is_authenticated(self, page: Page) -> bool:
        # Force quick-path to succeed on second runs (default on)
        return env_truthy("MOCK_QUICK_PATH_OK", default=True)

    async def fetch_documents(
        self,
        page: Page,
        http: httpx.AsyncClient,
        ctx: BrowserContext,
    ) -> tuple[list[Document], dict[str, bytes]]:
        await asyncio.sleep(0.6)
        docs = [
            Document(
                id="dec",
                name="Declarations Page.pdf",
                size_bytes=len(MOCK_PDF),
            ),
            Document(
                id="id-card",
                name="Auto ID Card.pdf",
                size_bytes=len(MOCK_PDF),
            ),
            Document(
                id="policy",
                name="Policy Booklet.pdf",
                size_bytes=len(MOCK_PDF),
            ),
        ]
        return docs, {d.id: MOCK_PDF for d in docs}
